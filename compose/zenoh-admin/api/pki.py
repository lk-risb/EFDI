"""Identity-bound managed-router enrollment and delegation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .control import _control
from .db import get_db
from .deps import pwd_ctx, require_role, write_audit
from .link_secrets import get_child_secret, put_child_secret, remove_child_secret
from .models import (
    Delegation,
    FederatedChild,
    IssuedIdentity,
    LinkCredential,
    PkiInvitation,
    TrustAuthority,
)
from .trust_crypto import (
    certificate_router_identity,
    certificate_sha256,
    sign_grant,
)
from .trust_identity import bounded_common_name, child_namespace, router_identity
from .trust_store import (
    TrustStoreError,
    accept_delegation,
    authority_by_identity,
    export_trust_chain,
    verify_trust_chain,
)
from .trust_types import ControlAction, DelegationGrant, RouterRole


router = APIRouter(prefix="/api/pki", tags=["pki"])
_CERT_DIR = Path(os.environ.get("EFDI_CERT_DIR", "/certs/efdi"))
_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "").strip("/")
_PREFIX_FILE = Path(os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix"))
_ROUTER_CA_CERT = Path(os.environ.get("EFDI_ROUTER_CA_PUBLIC_PATH", "/certs/router-ca-cert.pem"))
_POLICY_CERT = Path(os.environ.get("EFDI_POLICY_SIGNER_CERT_PATH", "/certs/policy-signer-cert.pem"))
_POLICY_KEY = Path(os.environ.get("EFDI_POLICY_SIGNER_KEY_PATH", "/certs/policy-signer-key.pem"))
_TRUST_BOOTSTRAP = Path(os.environ.get(
    "EFDI_TRUST_BOOTSTRAP_PATH", "/certs/efdi/trust/managed-bootstrap.json"
))


def _prefix() -> str:
    try:
        value = _PREFIX_FILE.read_text(encoding="utf-8").strip().strip("/")
        if value:
            return value
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "EFDI").strip("/")


def _full_namespace(partner_namespace: str | None = None) -> str:
    partner = (partner_namespace if partner_namespace is not None else _OWN_NAMESPACE).strip("/")
    return "/".join(item for item in (_prefix(), partner) if item)


class InvitationIn(BaseModel):
    child_name: str = Field(min_length=1, max_length=64)
    namespace: str = Field(min_length=1, max_length=512)
    max_delegation_depth: int = Field(default=0, ge=0, le=8)
    expires_in_hours: int = Field(default=24, ge=1, le=168)

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        try:
            child_namespace(_full_namespace(), value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value.strip("/")


class EnrollmentIn(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    router_ca_csr: str = Field(min_length=64, max_length=65536)
    transport_csr: str = Field(min_length=64, max_length=65536)
    policy_signer_csr: str = Field(min_length=64, max_length=65536)


def _invitation_out(invitation: PkiInvitation) -> dict:
    status = "used" if invitation.used_at else (
        "expired" if invitation.expires_at <= datetime.now(timezone.utc) else "pending"
    )
    return {
        "id": invitation.id,
        "child_name": invitation.child_name,
        "namespace": invitation.namespace,
        "max_delegation_depth": invitation.max_delegation_depth,
        "created_at": invitation.created_at.isoformat(),
        "expires_at": invitation.expires_at.isoformat(),
        "used_at": invitation.used_at.isoformat() if invitation.used_at else None,
        "status": status,
        "issued_serials": invitation.issued_serials,
    }


def _load_certificate(path: Path, label: str) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"local {label} is unavailable") from exc


def _verify_direct_signature(certificate: x509.Certificate, issuer: x509.Certificate) -> None:
    key = issuer.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or certificate.issuer != issuer.subject:
        raise HTTPException(status_code=503, detail="local policy signer is not issued by the router CA")
    try:
        key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(certificate.signature_hash_algorithm),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="local policy signer signature is invalid") from exc


async def ensure_local_authority(db: AsyncSession) -> TrustAuthority:
    """Bootstrap only public local trust metadata from mounted router identities."""
    identity = router_identity(_full_namespace())
    existing = await authority_by_identity(db, identity)
    if existing is not None:
        return existing
    ca = _load_certificate(_ROUTER_CA_CERT, "router CA certificate")
    policy = _load_certificate(_POLICY_CERT, "policy signer certificate")
    if certificate_router_identity(policy) != identity:
        raise HTTPException(status_code=503, detail="local policy signer has the wrong router identity")
    _verify_direct_signature(policy, ca)
    try:
        constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound as exc:
        raise HTTPException(status_code=503, detail="local router CA has no basic constraints") from exc
    if not constraints.ca or constraints.path_length is None:
        raise HTTPException(status_code=503, detail="local router CA cannot delegate managed children")
    ca_fingerprint = certificate_sha256(ca)
    if not _TRUST_BOOTSTRAP.is_file():
        previous_local = (
            await db.execute(
                select(TrustAuthority).where(
                    TrustAuthority.ca_fingerprint == ca_fingerprint,
                    TrustAuthority.parent_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if previous_local is not None:
            previous_local.identity_uri = identity
            previous_local.namespace_scope = _full_namespace() + "/**"
            previous_local.ca_cert_pem = _ROUTER_CA_CERT.read_text(encoding="utf-8")
            previous_local.policy_signer_fingerprint = certificate_sha256(policy)
            previous_local.policy_signer_cert_pem = _POLICY_CERT.read_text(encoding="utf-8")
            previous_local.max_delegation_depth = constraints.path_length
            previous_local.state = "active"
            previous_local.not_after = min(
                ca.not_valid_after_utc,
                policy.not_valid_after_utc,
            )
            await db.commit()
            await db.refresh(previous_local)
            return previous_local
    if _TRUST_BOOTSTRAP.is_file():
        try:
            bootstrap = json.loads(_TRUST_BOOTSTRAP.read_text(encoding="utf-8"))
            chain = bootstrap.get("trust_chain")
            if chain is None:
                parent_data = bootstrap["parent_authority"]
                chain = {
                    "schema": "efdi.trust-chain/v1",
                    "anchor": {
                        "identity_uri": parent_data["identity_uri"],
                        "namespace_scope": parent_data["namespace_scope"],
                        "ca_sha256": certificate_sha256(x509.load_pem_x509_certificate(
                            str(parent_data["ca_certificate"]).encode()
                        )),
                        "ca_certificate": parent_data["ca_certificate"],
                        "policy_signer_sha256": certificate_sha256(x509.load_pem_x509_certificate(
                            str(parent_data["policy_signer_certificate"]).encode()
                        )),
                        "policy_signer_certificate": parent_data["policy_signer_certificate"],
                        "max_delegation_depth": parent_data["max_delegation_depth"],
                        "not_after": min(
                            x509.load_pem_x509_certificate(str(parent_data["ca_certificate"]).encode()).not_valid_after_utc,
                            x509.load_pem_x509_certificate(str(parent_data["policy_signer_certificate"]).encode()).not_valid_after_utc,
                        ).isoformat(),
                    },
                    "steps": [{
                        "delegation_envelope": bootstrap["delegation_envelope"],
                        "subject_ca_certificate": _ROUTER_CA_CERT.read_text(encoding="utf-8"),
                        "subject_policy_signer_certificate": _POLICY_CERT.read_text(encoding="utf-8"),
                    }],
                }
            anchor_data = chain["anchor"]
            anchor_ca_pem = str(anchor_data["ca_certificate"])
            anchor_policy_pem = str(anchor_data["policy_signer_certificate"])
            anchor_ca = x509.load_pem_x509_certificate(anchor_ca_pem.encode())
            anchor_policy = x509.load_pem_x509_certificate(anchor_policy_pem.encode())
            anchor_identity = str(anchor_data["identity_uri"])
            if certificate_router_identity(anchor_policy) != anchor_identity:
                raise ValueError("trust anchor policy identity does not match bootstrap")
            _verify_direct_signature(anchor_policy, anchor_ca)
            anchor = await authority_by_identity(db, anchor_identity)
            if anchor is None:
                anchor = TrustAuthority(
                    identity_uri=anchor_identity,
                    namespace_scope=str(anchor_data["namespace_scope"]),
                    ca_fingerprint=certificate_sha256(anchor_ca),
                    ca_cert_pem=anchor_ca_pem,
                    policy_signer_fingerprint=certificate_sha256(anchor_policy),
                    policy_signer_cert_pem=anchor_policy_pem,
                    parent_id=None,
                    max_delegation_depth=int(anchor_data["max_delegation_depth"]),
                    state="active",
                    not_after=min(anchor_ca.not_valid_after_utc, anchor_policy.not_valid_after_utc),
                )
                db.add(anchor)
                await db.flush()
            verified = verify_trust_chain(chain, anchor)
            for step in chain["steps"]:
                grant = DelegationGrant.model_validate(step["delegation_envelope"]["payload"])
                authority = await authority_by_identity(db, grant.subject_identity)
                if authority is None:
                    authority, _ = await accept_delegation(
                        db,
                        step["delegation_envelope"],
                        subject_ca_pem=step["subject_ca_certificate"],
                        subject_policy_signer_pem=step["subject_policy_signer_certificate"],
                        commit=False,
                    )
            if verified.identity_uri != identity or authority.identity_uri != identity:
                raise ValueError("bootstrap delegation chain does not identify this router")
            await db.commit()
            await db.refresh(authority)
            return authority
        except (KeyError, TypeError, ValueError, OSError, TrustStoreError) as exc:
            await db.rollback()
            raise HTTPException(status_code=503, detail=f"managed trust bootstrap is invalid: {exc}") from exc
    authority = TrustAuthority(
        identity_uri=identity,
        namespace_scope=_full_namespace() + "/**",
        ca_fingerprint=ca_fingerprint,
        ca_cert_pem=_ROUTER_CA_CERT.read_text(encoding="utf-8"),
        policy_signer_fingerprint=certificate_sha256(policy),
        policy_signer_cert_pem=_POLICY_CERT.read_text(encoding="utf-8"),
        parent_id=None,
        max_delegation_depth=constraints.path_length,
        state="active",
        not_after=min(ca.not_valid_after_utc, policy.not_valid_after_utc),
    )
    db.add(authority)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        concurrent = await authority_by_identity(db, identity)
        if concurrent is None:
            raise
        return concurrent
    await db.refresh(authority)
    return authority


async def _bundle(invitation: PkiInvitation, db: AsyncSession) -> dict:
    if not invitation.authority_id:
        raise HTTPException(status_code=409, detail="enrollment has no managed authority record")
    secret = get_child_secret(invitation.authority_id)
    if not secret:
        raise HTTPException(
            status_code=409,
            detail="link secret is unavailable; rotate the child link credential instead of reissuing it",
        )
    parent_transport_path = _CERT_DIR / f"{_OWN_NAMESPACE}-cert.pem"
    try:
        parent_transport_cert = parent_transport_path.read_text(encoding="utf-8")
    except OSError:
        parent_transport_cert = None
    try:
        parent_policy_cert = _POLICY_CERT.read_text(encoding="utf-8")
    except OSError:
        parent_policy_cert = None
    child_authority = await db.get(TrustAuthority, invitation.authority_id)
    parent_authority = await db.get(TrustAuthority, child_authority.parent_id) if child_authority else None
    if parent_authority is None:
        raise HTTPException(status_code=409, detail="enrollment parent authority is unavailable")
    try:
        trust_chain = await export_trust_chain(db, child_authority)
    except TrustStoreError as exc:
        raise HTTPException(status_code=409, detail=f"enrollment trust chain is unavailable: {exc}") from exc
    return {
        "namespace": invitation.namespace,
        "parent_namespace": _OWN_NAMESPACE or None,
        "max_delegation_depth": invitation.max_delegation_depth,
        "router_ca_certificate": invitation.ca_cert_pem,
        "transport_certificate": invitation.transport_cert_pem,
        "policy_signer_certificate": invitation.policy_cert_pem,
        "issuer_chain": invitation.chain_pem,
        "transport_issuer_chain": invitation.transport_chain_pem or invitation.chain_pem,
        "parent_transport_certificate": parent_transport_cert,
        "parent_policy_signer_certificate": parent_policy_cert,
        "parent_authority": {
            "identity_uri": parent_authority.identity_uri,
            "namespace_scope": parent_authority.namespace_scope,
            "max_delegation_depth": parent_authority.max_delegation_depth,
            "ca_certificate": parent_authority.ca_cert_pem,
            "policy_signer_certificate": parent_authority.policy_signer_cert_pem,
        },
        "delegation_envelope": json.loads(invitation.grant_envelope_json or "null"),
        "trust_chain": trust_chain,
        "link_credential": secret,
        "issued_serials": invitation.issued_serials,
    }


@router.get("/status")
async def pki_status(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    issuer = await asyncio.to_thread(_control, "/v1/pki/status")
    try:
        authority = await ensure_local_authority(db)
        trust = {
            "ready": authority.state == "active" and authority.not_after > datetime.now(timezone.utc),
            "identity": authority.identity_uri,
            "namespace_scope": authority.namespace_scope,
            "max_delegation_depth": authority.max_delegation_depth,
            "expires_at": authority.not_after.isoformat(),
        }
    except HTTPException as exc:
        trust = {"ready": False, "error": exc.detail}
    return {**issuer, "managed_trust": trust}


@router.post("/invitations", status_code=201)
async def create_invitation(
    request: InvitationIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    issuer = await ensure_local_authority(db)
    if issuer.state != "active" or issuer.not_after <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="the local trust authority is not active")
    child_full = child_namespace(_full_namespace(), request.namespace)
    child_partner = child_full.removeprefix(_prefix() + "/")
    if request.max_delegation_depth >= issuer.max_delegation_depth:
        raise HTTPException(
            status_code=422,
            detail=f"delegation depth must be at most {issuer.max_delegation_depth - 1} for this issuer",
        )
    existing = await db.execute(
        select(FederatedChild.id).where(FederatedChild.namespace == child_partner)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="that namespace is already a direct child")
    raw_token = secrets.token_urlsafe(32)
    invitation = PkiInvitation(
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        child_name=request.child_name.strip(),
        namespace=child_partner,
        max_delegation_depth=request.max_delegation_depth,
        created_by=actor.id,
        expires_at=min(
            datetime.now(timezone.utc) + timedelta(hours=request.expires_in_hours),
            issuer.not_after,
        ),
    )
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)
    await write_audit(
        db,
        actor.id,
        "pki_invitation_created",
        f"namespace={child_partner}, max_depth={request.max_delegation_depth}",
    )
    return {**_invitation_out(invitation), "token": raw_token}


@router.get("/invitations")
async def list_invitations(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("superadmin")),
):
    result = await db.execute(
        select(PkiInvitation).order_by(PkiInvitation.created_at.desc()).limit(200)
    )
    return [_invitation_out(item) for item in result.scalars().all()]


def _certificate_metadata(certificate_pem: str) -> tuple[str, datetime]:
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
    return certificate_sha256(certificate), certificate.not_valid_after_utc


@router.post("/enroll")
async def enroll_router(request: EnrollmentIn, db: AsyncSession = Depends(get_db)):
    issuer = await ensure_local_authority(db)
    token_hash = hashlib.sha256(request.token.encode()).hexdigest()
    result = await db.execute(
        select(PkiInvitation).where(PkiInvitation.token_hash == token_hash).with_for_update()
    )
    invitation = result.scalar_one_or_none()
    if invitation is None:
        raise HTTPException(status_code=410, detail="invitation is invalid")

    ca_hash = hashlib.sha256(request.router_ca_csr.encode()).hexdigest()
    transport_hash = hashlib.sha256(request.transport_csr.encode()).hexdigest()
    policy_hash = hashlib.sha256(request.policy_signer_csr.encode()).hexdigest()
    if invitation.used_at:
        if (
            invitation.ca_csr_sha256 == ca_hash
            and invitation.transport_csr_sha256 == transport_hash
            and invitation.policy_csr_sha256 == policy_hash
        ):
            return await _bundle(invitation, db)
        raise HTTPException(status_code=410, detail="invitation was already used with different keys")
    now = datetime.now(timezone.utc)
    if invitation.expires_at <= now:
        raise HTTPException(status_code=410, detail="invitation expired")

    child_full = _full_namespace(invitation.namespace)
    identity = router_identity(child_full)
    common_names = {
        "ca": bounded_common_name("ca", identity),
        "transport": bounded_common_name("router", identity),
        "policy": bounded_common_name("policy", identity),
    }
    signing_requests = (
        (request.router_ca_csr, common_names["ca"], "router-ca", invitation.max_delegation_depth, 365),
        (request.transport_csr, common_names["transport"], "transport", 0, 3),
        (request.policy_signer_csr, common_names["policy"], "policy-signer", 0, 30),
    )
    ca_result, transport_result, policy_result = await asyncio.gather(*(
        asyncio.to_thread(
            _control,
            "/v1/pki/sign-csr",
            "POST",
            {
                "csr": csr,
                "common_name": common_name,
                "profile": profile,
                "path_length": depth,
                "days": days,
                "identity_uri": identity,
            },
        )
        for csr, common_name, profile, depth, days in signing_requests
    ))
    for item in (ca_result, transport_result, policy_result):
        if not item.get("ok"):
            raise HTTPException(status_code=502, detail=item.get("output") or "router signing failed")

    ca_cert = x509.load_pem_x509_certificate(ca_result["certificate"].encode())
    transport_fp, transport_not_after = _certificate_metadata(transport_result["certificate"])
    policy_fp, policy_not_after = _certificate_metadata(policy_result["certificate"])
    grant_not_after = min(ca_cert.not_valid_after_utc, policy_not_after, issuer.not_after)
    sequence = (await db.scalar(
        select(func.max(Delegation.sequence)).where(Delegation.issuer_authority_id == issuer.id)
    ) or 0) + 1
    namespace_scope = child_full + "/**"
    grant = DelegationGrant(
        id=uuid4(),
        issuer_identity=issuer.identity_uri,
        subject_ca_sha256=certificate_sha256(ca_cert),
        subject_identity=identity,
        namespace=namespace_scope,
        roles=[RouterRole.ROUTER_CA, RouterRole.ROUTER, RouterRole.WORKLOAD_ISSUER],
        publish=[namespace_scope],
        subscribe=[issuer.namespace_scope],
        control=[
            ControlAction.TOPOLOGY,
            ControlAction.STATUS,
            ControlAction.CONFIG_FROM_PARENT,
            ControlAction.MANAGE_CHILDREN,
        ],
        max_delegation_depth=invitation.max_delegation_depth,
        sequence=sequence,
        not_before=now - timedelta(minutes=2),
        not_after=grant_not_after,
    )
    try:
        envelope = sign_grant(grant, _POLICY_KEY.read_bytes(), _POLICY_CERT.read_bytes())
        authority, _delegation = await accept_delegation(
            db,
            envelope,
            subject_ca_pem=ca_result["certificate"],
            subject_policy_signer_pem=policy_result["certificate"],
            commit=False,
        )
    except (OSError, ValueError, TrustStoreError) as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail=f"managed delegation failed: {exc}") from exc

    username = "link-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    password = secrets.token_urlsafe(32)
    credential_expiry = min(transport_not_after, grant_not_after)
    db.add(LinkCredential(
        authority_id=authority.id,
        username=username,
        password_hash=pwd_ctx.hash(password),
        state="active",
        expires_at=credential_expiry,
    ))
    db.add_all([
        IssuedIdentity(
            authority_id=authority.id,
            identity_uri=identity,
            profile="transport",
            serial=str(transport_result["serial"]),
            cert_sha256=transport_fp,
            certificate_pem=transport_result["certificate"],
            state="active",
            not_after=transport_not_after,
        ),
        IssuedIdentity(
            authority_id=authority.id,
            identity_uri=identity,
            profile="policy-signer",
            serial=str(policy_result["serial"]),
            cert_sha256=policy_fp,
            certificate_pem=policy_result["certificate"],
            state="active",
            not_after=policy_not_after,
        ),
        FederatedChild(
            name=invitation.child_name,
            namespace=invitation.namespace,
            created_by=invitation.created_by,
            transport_cert_pem=transport_result["certificate"],
            cert_sha256=transport_fp,
            max_delegation_depth=invitation.max_delegation_depth,
        ),
    ])
    invitation.used_at = now
    invitation.authority_id = authority.id
    invitation.ca_csr_sha256 = ca_hash
    invitation.transport_csr_sha256 = transport_hash
    invitation.policy_csr_sha256 = policy_hash
    invitation.ca_cert_pem = ca_result["certificate"]
    invitation.transport_cert_pem = transport_result["certificate"]
    invitation.policy_cert_pem = policy_result["certificate"]
    invitation.chain_pem = ca_result["chain"]
    invitation.transport_chain_pem = transport_result.get("chain") or ca_result["chain"]
    invitation.grant_envelope_json = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    invitation.link_username = username
    invitation.link_password_hash = pwd_ctx.hash(password)
    invitation.issued_serials = (
        f"ca={ca_result['serial']},transport={transport_result['serial']},policy={policy_result['serial']}"
    )
    put_child_secret(authority.id, username, password)
    try:
        await db.commit()
    except Exception:
        remove_child_secret(authority.id)
        await db.rollback()
        raise
    await write_audit(
        db,
        invitation.created_by,
        "pki_router_enrolled",
        f"identity={identity}, transport_sha256={transport_fp}, grant={grant.id}",
    )
    return await _bundle(invitation, db)
