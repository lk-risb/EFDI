"""Managed trust inventory and explicit router lifecycle controls."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import pwd_ctx, require_role, write_audit
from .link_secrets import get_child_secret, put_child_secret
from .models import (
    AclRevision,
    Delegation,
    IssuedIdentity,
    LinkCredential,
    Revocation,
    TrustAuthority,
)
from .pki import ensure_local_authority


router = APIRouter(prefix="/api/trust", tags=["trust"])


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=512)


def _authority_out(item: TrustAuthority, local_id: str) -> dict:
    relationship = "local" if item.id == local_id else (
        "child" if item.parent_id == local_id else "ancestor"
    )
    return {
        "id": item.id,
        "identity_uri": item.identity_uri,
        "namespace_scope": item.namespace_scope,
        "ca_fingerprint": item.ca_fingerprint,
        "policy_signer_fingerprint": item.policy_signer_fingerprint,
        "parent_id": item.parent_id,
        "relationship": relationship,
        "max_delegation_depth": item.max_delegation_depth,
        "state": item.state,
        "created_at": item.created_at.isoformat(),
        "not_after": item.not_after.isoformat(),
    }


@router.get("")
async def trust_inventory(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    local = await ensure_local_authority(db)
    authorities = (await db.execute(
        select(TrustAuthority).order_by(TrustAuthority.created_at)
    )).scalars().all()
    identities = (await db.execute(
        select(IssuedIdentity).order_by(IssuedIdentity.issued_at.desc()).limit(500)
    )).scalars().all()
    revocations = (await db.execute(
        select(Revocation).order_by(Revocation.created_at.desc()).limit(500)
    )).scalars().all()
    acl = (await db.execute(
        select(AclRevision).order_by(AclRevision.sequence.desc()).limit(1)
    )).scalar_one_or_none()
    return {
        "local_authority_id": local.id,
        "authorities": [_authority_out(item, local.id) for item in authorities],
        "identities": [{
            "id": item.id,
            "authority_id": item.authority_id,
            "identity_uri": item.identity_uri,
            "profile": item.profile,
            "serial": item.serial,
            "cert_sha256": item.cert_sha256,
            "state": item.state,
            "issued_at": item.issued_at.isoformat(),
            "not_after": item.not_after.isoformat(),
            "replaced_by_id": item.replaced_by_id,
        } for item in identities],
        "revocations": [{
            "id": item.id,
            "target_type": item.target_type,
            "target_reference": item.target_reference,
            "reason": item.reason,
            "sequence": item.sequence,
            "state": item.state,
            "created_at": item.created_at.isoformat(),
        } for item in revocations],
        "acl": ({
            "sequence": acl.sequence,
            "sha256": acl.policy_sha256,
            "state": acl.state,
            "detail": acl.detail,
            "applied_at": acl.applied_at.isoformat() if acl.applied_at else None,
        } if acl else None),
    }


async def _direct_child(db: AsyncSession, authority_id: str) -> tuple[TrustAuthority, TrustAuthority]:
    local = await ensure_local_authority(db)
    authority = await db.get(TrustAuthority, authority_id)
    if authority is None:
        raise HTTPException(status_code=404, detail="managed authority not found")
    if authority.parent_id != local.id:
        raise HTTPException(status_code=403, detail="only a direct child can be changed here")
    return local, authority


@router.post("/authorities/{authority_id}/quarantine")
async def quarantine_authority(
    authority_id: str,
    body: ReasonIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    _, authority = await _direct_child(db, authority_id)
    if authority.state == "decommissioned":
        raise HTTPException(status_code=409, detail="decommissioning is irreversible")
    authority.state = "quarantined"
    await db.commit()
    await write_audit(db, actor.id, "trust_authority_quarantined", f"{authority.identity_uri}: {body.reason}")
    return {"state": authority.state, "acl_apply_required": True}


@router.post("/authorities/{authority_id}/restore")
async def restore_authority(
    authority_id: str,
    body: ReasonIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    _, authority = await _direct_child(db, authority_id)
    if authority.state != "quarantined":
        raise HTTPException(status_code=409, detail="only a quarantined authority can be restored")
    authority.state = "active"
    await db.commit()
    await write_audit(db, actor.id, "trust_authority_restored", f"{authority.identity_uri}: {body.reason}")
    return {"state": authority.state, "acl_apply_required": True}


@router.post("/authorities/{authority_id}/decommission")
async def decommission_authority(
    authority_id: str,
    body: ReasonIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    _, authority = await _direct_child(db, authority_id)
    if authority.state == "decommissioned":
        raise HTTPException(status_code=409, detail="authority is already decommissioned")
    sequence = (await db.scalar(select(func.max(Revocation.sequence))) or 0) + 1
    authority.state = "decommissioned"
    for model in (Delegation, IssuedIdentity, LinkCredential):
        column = model.subject_authority_id if model is Delegation else model.authority_id
        records = (await db.execute(select(model).where(column == authority.id))).scalars().all()
        for record in records:
            record.state = "revoked"
    db.add(Revocation(
        target_type="authority",
        target_reference=authority.identity_uri,
        reason=body.reason,
        sequence=sequence,
        state="active",
        created_by=actor.id,
    ))
    await db.commit()
    await write_audit(db, actor.id, "trust_authority_decommissioned", f"{authority.identity_uri}: {body.reason}")
    return {"state": authority.state, "revocation_sequence": sequence, "acl_apply_required": True}


@router.post("/authorities/{authority_id}/rotate-link")
async def rotate_link_credential(
    authority_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    _, authority = await _direct_child(db, authority_id)
    if authority.state != "active":
        raise HTTPException(status_code=409, detail="link credentials can rotate only for an active child")
    current = get_child_secret(authority.id)
    if current is None:
        raise HTTPException(status_code=409, detail="current child link secret is unavailable")
    credential = (await db.execute(
        select(LinkCredential).where(
            LinkCredential.authority_id == authority.id,
            LinkCredential.state == "active",
        ).order_by(LinkCredential.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if credential is None:
        raise HTTPException(status_code=409, detail="active child link credential is unavailable")
    password = secrets.token_urlsafe(32)
    credential.password_hash = pwd_ctx.hash(password)
    credential.rotated_at = datetime.now(timezone.utc)
    credential.expires_at = min(authority.not_after, datetime.now(timezone.utc) + timedelta(days=30))
    put_child_secret(authority.id, credential.username, password)
    await db.commit()
    await write_audit(db, actor.id, "trust_link_rotated", f"identity={authority.identity_uri}")
    return {
        "link_credential": {"username": credential.username, "password": password},
        "expires_at": credential.expires_at.isoformat(),
        "acl_apply_required": True,
    }
