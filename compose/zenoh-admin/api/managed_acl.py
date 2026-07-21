"""Compile and safely activate identity-bound direct-link Zenoh ACLs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import json5
from cryptography import x509
from cryptography.x509.oid import NameOID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .acl_policy import PeerPolicy, compile_acl
from .config import (
    CONFIG_PATH,
    _extract_fields,
    apply_rendered_config,
    atomic_write,
    restart_router_container,
    wait_for_router_health,
)
from .db import get_db
from .deps import require_role, write_audit
from .link_secrets import all_secrets, ensure_local_secret
from .models import AclRevision, Delegation, IssuedIdentity, LinkCredential, TrustAuthority
from .pki import ensure_local_authority
from .trust_crypto import verify_grant_envelope


router = APIRouter(prefix="/api/trust/acl", tags=["trust"])
_DICTIONARY_PATH = Path(os.environ.get(
    "EFDI_ZENOH_USER_DICTIONARY_PATH", "/zenoh-config/tls/users.txt"
))
_ROUTER_DICTIONARY_PATH = os.environ.get(
    "EFDI_ZENOH_ROUTER_USER_DICTIONARY_PATH", "/etc/zenoh/tls/users.txt"
)
_PARENT_CERT_PATH = Path(os.environ.get(
    "ZENOH_ADMIN_TRUSTED_PARENT_CERT_PATH", "/certs/trusted-parent.pem"
))


def _common_name(pem: str) -> str:
    certificate = x509.load_pem_x509_certificate(pem.encode())
    values = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(values) != 1:
        raise ValueError("transport certificate must contain exactly one common name")
    return values[0].value


async def _latest_delegation(db: AsyncSession, subject_id: str) -> Delegation | None:
    result = await db.execute(
        select(Delegation).where(
            Delegation.subject_authority_id == subject_id,
            Delegation.state == "active",
        ).order_by(Delegation.sequence.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _transport_identity(db: AsyncSession, authority_id: str) -> IssuedIdentity | None:
    result = await db.execute(
        select(IssuedIdentity).where(
            IssuedIdentity.authority_id == authority_id,
            IssuedIdentity.profile == "transport",
            IssuedIdentity.state == "active",
        ).order_by(IssuedIdentity.issued_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def compile_current_policy(db: AsyncSession) -> dict:
    local = await ensure_local_authority(db)
    fields = _extract_fields(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    if fields.fabric_endpoints and local.parent_id is None:
        raise ValueError(
            "generated ACL activation is blocked while this root has an unmanaged fabric uplink; "
            "enroll or explicitly migrate that peer first"
        )
    secrets_document = all_secrets()
    peers: list[PeerPolicy] = []

    child_result = await db.execute(
        select(TrustAuthority).where(TrustAuthority.parent_id == local.id)
    )
    for child in child_result.scalars().all():
        delegation = await _latest_delegation(db, child.id)
        identity = await _transport_identity(db, child.id)
        credential_result = await db.execute(
            select(LinkCredential).where(
                LinkCredential.authority_id == child.id,
                LinkCredential.state == "active",
            ).order_by(LinkCredential.created_at.desc()).limit(1)
        )
        credential = credential_result.scalar_one_or_none()
        runtime_secret = secrets_document.get("children", {}).get(child.id)
        if delegation is None or identity is None or credential is None or not runtime_secret:
            raise ValueError(f"child {child.identity_uri} has incomplete trust or link state")
        grant, _ = verify_grant_envelope(delegation.envelope_json)
        peers.append(PeerPolicy(
            grant_id=delegation.grant_id,
            identity_uri=child.identity_uri,
            cert_common_name=_common_name(identity.certificate_pem),
            username=credential.username,
            relationship="child",
            namespace=grant.namespace,
            publish=tuple(grant.publish),
            subscribe=tuple(grant.subscribe),
            quarantined=child.state in {"quarantined", "decommissioned"},
        ))

    if local.parent_id:
        parent = await db.get(TrustAuthority, local.parent_id)
        delegation = await _latest_delegation(db, local.id)
        parent_secret = secrets_document.get("parent")
        try:
            parent_cert = _PARENT_CERT_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("trusted parent transport certificate is unavailable") from exc
        if parent is None or delegation is None or not isinstance(parent_secret, dict):
            raise ValueError("parent trust or link state is incomplete")
        grant, _ = verify_grant_envelope(delegation.envelope_json)
        peers.append(PeerPolicy(
            grant_id=delegation.grant_id,
            identity_uri=parent.identity_uri,
            cert_common_name=_common_name(parent_cert),
            username=str(parent_secret["username"]),
            relationship="parent",
            namespace=grant.namespace,
            publish=tuple(grant.publish),
            subscribe=tuple(grant.subscribe),
            quarantined=parent.state in {"quarantined", "decommissioned"},
        ))

    local_secret = ensure_local_secret()
    compiled = compile_acl(
        local_data_scope="/".join(
            part for part in (fields.publish_prefix, fields.partner_namespace, "**") if part
        ),
        inbound_scope=fields.inbound_namespace.rstrip("/") + "/**",
        federation_root=fields.namespace_prefix.split("/")[0],
        local_namespace=f"{fields.namespace_prefix}/{fields.partner_namespace}",
        peers=peers,
    )
    incoming = [local_secret, *secrets_document.get("children", {}).values()]
    dictionary = "".join(
        f"{item['username']}:{hashlib.sha256(item['password'].encode()).hexdigest()}\n"
        for item in sorted(incoming, key=lambda item: item["username"])
    )
    parent_secret = secrets_document.get("parent")
    return {
        **compiled,
        "peers": peers,
        "dictionary": dictionary,
        "outbound": {
            "user": parent_secret["username"],
            "password": hashlib.sha256(parent_secret["password"].encode()).hexdigest(),
        } if parent_secret else {"user": None, "password": None},
        "fields": fields,
    }


def _inject(rendered: str, policy: dict) -> str:
    document = json5.loads(rendered)
    transport = document.setdefault("transport", {})
    auth = transport.setdefault("auth", {})
    auth["usrpwd"] = {
        **policy["outbound"],
        "dictionary_file": _ROUTER_DICTIONARY_PATH,
    }
    document["access_control"] = policy["access_control"]
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


async def _preview(db: AsyncSession) -> tuple[dict, str]:
    policy = await compile_current_policy(db)
    current = Path(CONFIG_PATH).read_text(encoding="utf-8")
    return policy, _inject(current, policy)


@router.get("/preview")
async def preview_acl(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "superadmin")),
):
    try:
        policy, _ = await _preview(db)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "sha256": policy["sha256"],
        "peer_count": len(policy["peers"]),
        "peers": [{
            "identity": item.identity_uri,
            "relationship": item.relationship,
            "namespace": item.namespace,
            "quarantined": item.quarantined,
        } for item in policy["peers"]],
        "access_control": policy["access_control"],
    }


@router.post("/apply")
async def apply_acl(
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    try:
        policy, rendered = await _preview(db)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sequence = (await db.scalar(select(func.max(AclRevision.sequence))) or 0) + 1
    revision = AclRevision(
        sequence=sequence,
        policy_sha256=policy["sha256"],
        policy_json=policy["canonical_json"],
        state="staged",
        created_by=actor.id,
    )
    db.add(revision)
    await db.commit()
    previous_dictionary = _DICTIONARY_PATH.read_text(encoding="utf-8") if _DICTIONARY_PATH.exists() else None
    _DICTIONARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(_DICTIONARY_PATH), policy["dictionary"])
    os.chmod(_DICTIONARY_PATH, 0o600)
    result = await asyncio.to_thread(
        apply_rendered_config,
        rendered,
        policy["fields"],
        restart_native=True,
        preserve_management=bool(policy["outbound"]["user"]),
    )
    if result["status"] != "applied":
        if previous_dictionary is None:
            _DICTIONARY_PATH.unlink(missing_ok=True)
        else:
            atomic_write(str(_DICTIONARY_PATH), previous_dictionary)
            os.chmod(_DICTIONARY_PATH, 0o600)
        await asyncio.to_thread(restart_router_container)
        await asyncio.to_thread(wait_for_router_health)
        revision.state = "rolled_back"
        revision.detail = result.get("error")
    else:
        revision.state = "applied"
        revision.applied_at = datetime.now(timezone.utc)
    await db.commit()
    await write_audit(db, actor.id, "managed_acl_apply", f"sha256={policy['sha256']}, state={revision.state}")
    return {**result, "acl_sha256": policy["sha256"], "sequence": sequence}
