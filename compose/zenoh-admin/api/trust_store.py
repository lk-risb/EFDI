"""Persistent trust state. Only verified public material and password hashes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ObjectIdentifier
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Delegation, Revocation, TrustAuthority
from .trust_crypto import (
    canonical_grant,
    certificate_router_identity,
    certificate_sha256,
    verify_grant_envelope,
)
from .trust_types import DelegationGrant, grant_is_subset


_POLICY_SIGNER_EKU = ObjectIdentifier("1.3.6.1.4.1.55555.1.1")
_MAX_TRUST_CHAIN_DEPTH = 33
_MAX_CERTIFICATE_BYTES = 64 * 1024


class TrustStoreError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedAuthority:
    identity_uri: str
    namespace_scope: str
    ca_fingerprint: str
    ca_cert_pem: str
    policy_signer_fingerprint: str
    policy_signer_cert_pem: str
    max_delegation_depth: int
    not_after: datetime
    effective_grant: DelegationGrant | None


def _verify_issued_by(certificate: x509.Certificate, issuer: x509.Certificate) -> None:
    public_key = issuer.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise TrustStoreError("managed router issuers must use ECDSA keys")
    if certificate.issuer != issuer.subject:
        raise TrustStoreError("certificate issuer does not match the trusted authority")
    try:
        public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(certificate.signature_hash_algorithm),
        )
    except Exception as exc:
        raise TrustStoreError("certificate signature is not valid for the trusted authority") from exc


def _verify_ca_profile(certificate: x509.Certificate) -> None:
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as exc:
        raise TrustStoreError("managed router CA is missing required constraints") from exc
    if not constraints.ca or constraints.path_length is None or not usage.key_cert_sign:
        raise TrustStoreError("managed router CA profile is invalid")
    key = certificate.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise TrustStoreError("managed router CA must use ECDSA P-256")


def _verify_policy_profile(certificate: x509.Certificate, identity: str) -> None:
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        extended = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound as exc:
        raise TrustStoreError("policy signer is missing required certificate constraints") from exc
    if constraints.ca or not usage.digital_signature or _POLICY_SIGNER_EKU not in extended:
        raise TrustStoreError("policy signer certificate profile is invalid")
    if certificate_router_identity(certificate) != identity:
        raise TrustStoreError("policy signer identity does not match the delegated authority")
    key = certificate.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise TrustStoreError("policy signer must use ECDSA P-256")


def _authority_record(authority: TrustAuthority, grant: DelegationGrant | None) -> VerifiedAuthority:
    if not authority.policy_signer_fingerprint or not authority.policy_signer_cert_pem:
        raise TrustStoreError("trusted authority has no active policy signer")
    return VerifiedAuthority(
        identity_uri=authority.identity_uri,
        namespace_scope=authority.namespace_scope,
        ca_fingerprint=authority.ca_fingerprint,
        ca_cert_pem=authority.ca_cert_pem,
        policy_signer_fingerprint=authority.policy_signer_fingerprint,
        policy_signer_cert_pem=authority.policy_signer_cert_pem,
        max_delegation_depth=authority.max_delegation_depth,
        not_after=authority.not_after,
        effective_grant=grant,
    )


async def authority_by_identity(db: AsyncSession, identity_uri: str) -> TrustAuthority | None:
    result = await db.execute(
        select(TrustAuthority).where(TrustAuthority.identity_uri == identity_uri)
    )
    return result.scalar_one_or_none()


async def authority_is_revoked(db: AsyncSession, authority: TrustAuthority) -> bool:
    references = (authority.ca_fingerprint, authority.identity_uri)
    result = await db.execute(
        select(Revocation.id).where(
            Revocation.state == "active",
            Revocation.target_reference.in_(references),
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def export_trust_chain(db: AsyncSession, authority: TrustAuthority) -> dict:
    """Export the bounded public proof from a local trust anchor to authority."""
    lineage = [authority]
    current = authority
    while current.parent_id is not None:
        if len(lineage) >= _MAX_TRUST_CHAIN_DEPTH:
            raise TrustStoreError("trust chain exceeds the supported depth")
        parent = await db.get(TrustAuthority, current.parent_id)
        if parent is None:
            raise TrustStoreError("trust chain contains a missing parent authority")
        lineage.append(parent)
        current = parent
    lineage.reverse()
    steps = []
    for subject in lineage[1:]:
        delegation = (await db.execute(
            select(Delegation).where(
                Delegation.subject_authority_id == subject.id,
                Delegation.state == "active",
            ).order_by(Delegation.sequence.desc()).limit(1)
        )).scalar_one_or_none()
        if delegation is None:
            raise TrustStoreError("trust chain contains an authority without an active delegation")
        steps.append({
            "delegation_envelope": json.loads(delegation.envelope_json),
            "subject_ca_certificate": subject.ca_cert_pem,
            "subject_policy_signer_certificate": subject.policy_signer_cert_pem,
        })
    anchor = lineage[0]
    return {
        "schema": "efdi.trust-chain/v1",
        "anchor": {
            "identity_uri": anchor.identity_uri,
            "namespace_scope": anchor.namespace_scope,
            "ca_sha256": anchor.ca_fingerprint,
            "ca_certificate": anchor.ca_cert_pem,
            "policy_signer_sha256": anchor.policy_signer_fingerprint,
            "policy_signer_certificate": anchor.policy_signer_cert_pem,
            "max_delegation_depth": anchor.max_delegation_depth,
            "not_after": anchor.not_after.isoformat(),
        },
        "steps": steps,
    }


def verify_trust_chain(
    proof: object,
    trusted_anchor: TrustAuthority,
    *,
    revoked_references: set[str] | None = None,
    now: datetime | None = None,
) -> VerifiedAuthority:
    """Verify every CA, policy signer, grant, and narrowing step in a proof."""
    if not isinstance(proof, dict) or set(proof) != {"schema", "anchor", "steps"}:
        raise TrustStoreError("trust chain has an invalid shape")
    if proof.get("schema") != "efdi.trust-chain/v1":
        raise TrustStoreError("unsupported trust chain schema")
    anchor = proof.get("anchor")
    if not isinstance(anchor, dict) or set(anchor) != {
        "identity_uri", "namespace_scope", "ca_sha256", "ca_certificate",
        "policy_signer_sha256", "policy_signer_certificate",
        "max_delegation_depth", "not_after",
    }:
        raise TrustStoreError("trust chain anchor has an invalid shape")
    if anchor.get("identity_uri") != trusted_anchor.identity_uri:
        raise TrustStoreError("trust chain anchor identity is not trusted")
    if anchor.get("ca_sha256") != trusted_anchor.ca_fingerprint:
        raise TrustStoreError("trust chain anchor fingerprint is not trusted")
    if anchor.get("ca_certificate") != trusted_anchor.ca_cert_pem:
        raise TrustStoreError("trust chain anchor certificate is not trusted")
    if anchor.get("policy_signer_sha256") != trusted_anchor.policy_signer_fingerprint:
        raise TrustStoreError("trust chain anchor policy signer is not trusted")
    if anchor.get("policy_signer_certificate") != trusted_anchor.policy_signer_cert_pem:
        raise TrustStoreError("trust chain anchor policy certificate is not trusted")
    if anchor.get("namespace_scope") != trusted_anchor.namespace_scope:
        raise TrustStoreError("trust chain anchor namespace is not trusted")
    if anchor.get("max_delegation_depth") != trusted_anchor.max_delegation_depth:
        raise TrustStoreError("trust chain anchor depth is not trusted")
    if anchor.get("not_after") != trusted_anchor.not_after.isoformat():
        raise TrustStoreError("trust chain anchor lifetime is not trusted")
    steps = proof.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) < _MAX_TRUST_CHAIN_DEPTH:
        raise TrustStoreError("trust chain must contain a bounded delegation path")

    current_time = now or datetime.now(timezone.utc)
    revoked = revoked_references or set()
    if (
        trusted_anchor.state != "active"
        or trusted_anchor.identity_uri in revoked
        or trusted_anchor.ca_fingerprint in revoked
        or trusted_anchor.not_after <= current_time
    ):
        raise TrustStoreError("trust chain anchor is not active")
    issuer = _authority_record(trusted_anchor, None)
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {
            "delegation_envelope", "subject_ca_certificate", "subject_policy_signer_certificate"
        }:
            raise TrustStoreError(f"trust chain step {index} has an invalid shape")
        ca_pem = step["subject_ca_certificate"]
        policy_pem = step["subject_policy_signer_certificate"]
        if (
            not isinstance(ca_pem, str)
            or not isinstance(policy_pem, str)
            or len(ca_pem.encode()) > _MAX_CERTIFICATE_BYTES
            or len(policy_pem.encode()) > _MAX_CERTIFICATE_BYTES
        ):
            raise TrustStoreError(f"trust chain step {index} contains an invalid certificate")
        grant, _ = verify_grant_envelope(
            step["delegation_envelope"],
            expected_signer_sha256=issuer.policy_signer_fingerprint,
            now=current_time,
        )
        if grant.issuer_identity != issuer.identity_uri:
            raise TrustStoreError(f"trust chain step {index} has the wrong issuer")
        if issuer.effective_grant is not None:
            if not grant_is_subset(grant, issuer.effective_grant):
                raise TrustStoreError(f"trust chain step {index} widens its parent delegation")
        elif grant.max_delegation_depth >= issuer.max_delegation_depth:
            raise TrustStoreError(f"trust chain step {index} does not reduce delegation depth")

        try:
            ca = x509.load_pem_x509_certificate(ca_pem.encode())
            policy = x509.load_pem_x509_certificate(policy_pem.encode())
            issuer_ca = x509.load_pem_x509_certificate(issuer.ca_cert_pem.encode())
        except ValueError as exc:
            raise TrustStoreError(f"trust chain step {index} contains malformed certificates") from exc
        _verify_issued_by(ca, issuer_ca)
        _verify_issued_by(policy, issuer_ca)
        _verify_ca_profile(ca)
        _verify_policy_profile(policy, grant.subject_identity)
        ca_fingerprint = certificate_sha256(ca)
        policy_fingerprint = certificate_sha256(policy)
        if ca_fingerprint != grant.subject_ca_sha256:
            raise TrustStoreError(f"trust chain step {index} CA does not match its grant")
        if grant.subject_identity in revoked or ca_fingerprint in revoked or policy_fingerprint in revoked:
            raise TrustStoreError(f"trust chain step {index} is revoked")
        not_after = min(grant.not_after, ca.not_valid_after_utc, policy.not_valid_after_utc)
        if current_time < ca.not_valid_before_utc or current_time < policy.not_valid_before_utc:
            raise TrustStoreError(f"trust chain step {index} is not valid yet")
        if not_after <= current_time:
            raise TrustStoreError(f"trust chain step {index} is expired")
        issuer = VerifiedAuthority(
            identity_uri=grant.subject_identity,
            namespace_scope=grant.namespace,
            ca_fingerprint=ca_fingerprint,
            ca_cert_pem=ca_pem,
            policy_signer_fingerprint=policy_fingerprint,
            policy_signer_cert_pem=policy_pem,
            max_delegation_depth=grant.max_delegation_depth,
            not_after=not_after,
            effective_grant=grant,
        )
    return issuer


async def accept_delegation(
    db: AsyncSession,
    envelope: dict,
    *,
    subject_ca_pem: str,
    subject_policy_signer_pem: str | None,
    commit: bool = True,
) -> tuple[TrustAuthority, Delegation]:
    """Verify and atomically persist a strictly narrower child authority."""
    raw_signer = envelope.get("signer_certificate")
    if not isinstance(raw_signer, str):
        raise TrustStoreError("delegation envelope has no signer certificate")
    signer = x509.load_pem_x509_certificate(raw_signer.encode())
    signer_fp = certificate_sha256(signer)
    grant, _ = verify_grant_envelope(envelope, expected_signer_sha256=signer_fp)

    issuer = await authority_by_identity(db, grant.issuer_identity)
    if issuer is None or issuer.state != "active" or await authority_is_revoked(db, issuer):
        raise TrustStoreError("delegation issuer is not an active trusted authority")
    if issuer.policy_signer_fingerprint != signer_fp:
        raise TrustStoreError("delegation signer is not the issuer's active policy signer")

    parent_result = await db.execute(
        select(Delegation).where(
            Delegation.subject_authority_id == issuer.id,
            Delegation.state == "active",
        ).order_by(Delegation.sequence.desc()).limit(1)
    )
    parent_record = parent_result.scalar_one_or_none()
    if parent_record is not None:
        parent_envelope = json.loads(parent_record.envelope_json)
        parent_grant, _ = verify_grant_envelope(
            parent_envelope,
            expected_signer_sha256=parent_envelope["signer_sha256"],
        )
        if not grant_is_subset(grant, parent_grant):
            raise TrustStoreError("delegation exceeds the issuer's effective grant")
    elif grant.max_delegation_depth >= issuer.max_delegation_depth:
        raise TrustStoreError("delegation depth is not lower than the root authority")

    highest_sequence = await db.scalar(
        select(func.max(Delegation.sequence)).where(Delegation.issuer_authority_id == issuer.id)
    )
    if highest_sequence is not None and grant.sequence <= highest_sequence:
        raise TrustStoreError("delegation sequence is stale or replayed")

    ca_cert = x509.load_pem_x509_certificate(subject_ca_pem.encode())
    issuer_ca = x509.load_pem_x509_certificate(issuer.ca_cert_pem.encode())
    _verify_issued_by(ca_cert, issuer_ca)
    _verify_ca_profile(ca_cert)
    ca_fp = ca_cert.fingerprint(hashes.SHA256()).hex()
    current = datetime.now(timezone.utc)
    if current < ca_cert.not_valid_before_utc or current >= ca_cert.not_valid_after_utc:
        raise TrustStoreError("subject CA certificate is not currently valid")
    if ca_fp != grant.subject_ca_sha256:
        raise TrustStoreError("subject CA fingerprint does not match the signed grant")
    policy_fp = None
    if subject_policy_signer_pem:
        policy_cert = x509.load_pem_x509_certificate(subject_policy_signer_pem.encode())
        _verify_issued_by(policy_cert, issuer_ca)
        _verify_policy_profile(policy_cert, grant.subject_identity)
        if current < policy_cert.not_valid_before_utc or current >= policy_cert.not_valid_after_utc:
            raise TrustStoreError("subject policy signer certificate is not currently valid")
        policy_fp = policy_cert.fingerprint(hashes.SHA256()).hex()

    existing = await authority_by_identity(db, grant.subject_identity)
    if existing is not None:
        raise TrustStoreError("subject authority identity already exists")
    authority = TrustAuthority(
        identity_uri=grant.subject_identity,
        namespace_scope=grant.namespace,
        ca_fingerprint=ca_fp,
        ca_cert_pem=subject_ca_pem,
        policy_signer_fingerprint=policy_fp,
        policy_signer_cert_pem=subject_policy_signer_pem,
        parent_id=issuer.id,
        max_delegation_depth=grant.max_delegation_depth,
        state="active",
        not_after=min(grant.not_after, ca_cert.not_valid_after_utc),
    )
    db.add(authority)
    await db.flush()
    canonical_envelope = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    delegation = Delegation(
        grant_id=str(grant.id),
        issuer_authority_id=issuer.id,
        subject_authority_id=authority.id,
        sequence=grant.sequence,
        envelope_json=canonical_envelope,
        grant_sha256=hashlib.sha256(canonical_grant(grant)).hexdigest(),
        state="active",
        not_before=grant.not_before,
        not_after=grant.not_after,
    )
    db.add(delegation)
    if commit:
        await db.commit()
        await db.refresh(authority)
        await db.refresh(delegation)
    else:
        await db.flush()
    return authority, delegation


def authority_active_now(authority: TrustAuthority, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    return authority.state == "active" and authority.not_after > current
