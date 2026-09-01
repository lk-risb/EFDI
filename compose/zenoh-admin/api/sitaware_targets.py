"""CRUD for independent SitaWare HQ ingress targets.

Each enabled row gets its own bridges/sitaware_bridge.py process on the
host, launched and reconciled by admin_control.py — this module is the
only writer of the two files that hand configuration off to it:

  EFDI_SITAWARE_TARGETS_MANIFEST_PATH  non-secret desired state (this
                                       table's rows, minus credentials)
  EFDI_SITAWARE_TARGET_SECRET_PATH     per-target username/password,
                                       mode 0600 (sitaware_target_secrets.py)

Both live under the same POD_STATE_DIR/zenoh bind mount link_secrets.py
already uses, so admin_control.py (host-side, outside Docker) can read
them without a database connection of its own — see that module's
_reconcile_sitaware_targets() for the other half of this.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .control import _control
from .db import get_db
from .deps import require_role, write_audit
from .models import SitawareTarget
from .sitaware_target_secrets import (
    get_target_secret,
    remove_target_secret,
    set_target_secret,
)

router = APIRouter(prefix="/api/sitaware-targets", tags=["sitaware-targets"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_MANIFEST_PATH = Path(os.environ.get(
    "EFDI_SITAWARE_TARGETS_MANIFEST_PATH", "/zenoh-config/sitaware-targets.json"
))


class SitawareTargetIn(BaseModel):
    name: str
    url: str
    url_fallback: str | None = None
    url_tailscale: str | None = None
    username: str = ""
    password: str = ""
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not _NAME_RE.match(v):
            raise ValueError(
                "name must be lowercase letters, digits, and internal hyphens only "
                "(this becomes the process name and Zenoh topic segment)"
            )
        return v

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("url must start with http:// or https://")
        return v.rstrip("/")

    @field_validator("url_fallback", "url_tailscale")
    @classmethod
    def _check_optional_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("url must start with http:// or https://")
        return v.rstrip("/")


class SitawareTargetUpdate(BaseModel):
    """Same fields as create, all optional — only supplied fields change.
    Credentials are only rotated if both username and password are given;
    omitting them keeps whatever is already stored."""
    url: str | None = None
    url_fallback: str | None = None
    url_tailscale: str | None = None
    username: str | None = None
    password: str | None = None
    enabled: bool | None = None

    _check_url = field_validator("url")(SitawareTargetIn._check_url.__func__)
    _check_optional_url = field_validator("url_fallback", "url_tailscale")(
        SitawareTargetIn._check_optional_url.__func__
    )


class SitawareTargetOut(BaseModel):
    id: str
    name: str
    url: str
    url_fallback: str | None
    url_tailscale: str | None
    enabled: bool
    created_by: str
    created_at: str
    has_credentials: bool
    running: bool | None = None

    class Config:
        from_attributes = True


def _write_manifest(rows: list[SitawareTarget]) -> None:
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "id": row.id,
            "name": row.name,
            "url": row.url,
            "url_fallback": row.url_fallback,
            "url_tailscale": row.url_tailscale,
            "enabled": row.enabled,
        }
        for row in rows
    ]
    tmp = _MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, _MANIFEST_PATH)


async def _sync_manifest_and_reconcile(db: AsyncSession) -> None:
    result = await db.execute(select(SitawareTarget))
    _write_manifest(list(result.scalars().all()))
    try:
        _control("/v1/sitaware-targets/reconcile", method="POST")
    except HTTPException:
        # Config is already persisted; a control-agent hiccup just delays
        # the process actually starting/stopping until the next reconcile
        # (admin_control.py also reconciles once on its own startup).
        pass


async def _out(row: SitawareTarget, statuses: dict) -> SitawareTargetOut:
    return SitawareTargetOut(
        id=row.id, name=row.name, url=row.url,
        url_fallback=row.url_fallback, url_tailscale=row.url_tailscale,
        enabled=row.enabled, created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        has_credentials=get_target_secret(row.id) is not None,
        running=statuses.get(row.id, {}).get("running"),
    )


def _live_statuses() -> dict:
    try:
        return _control("/v1/sitaware-targets/status")
    except HTTPException:
        return {}


@router.get("", response_model=list[SitawareTargetOut])
async def list_targets(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    result = await db.execute(select(SitawareTarget).order_by(SitawareTarget.created_at))
    statuses = _live_statuses()
    return [await _out(row, statuses) for row in result.scalars().all()]


@router.post("", response_model=SitawareTargetOut)
async def create_target(
    target_in: SitawareTargetIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    existing = await db.execute(select(SitawareTarget).where(SitawareTarget.name == target_in.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"A target named '{target_in.name}' already exists")

    row = SitawareTarget(
        name=target_in.name, url=target_in.url,
        url_fallback=target_in.url_fallback, url_tailscale=target_in.url_tailscale,
        enabled=target_in.enabled, created_by=actor.id,
    )
    db.add(row)
    await db.flush()
    if target_in.username and target_in.password:
        set_target_secret(row.id, target_in.username, target_in.password)
    await db.commit()
    await db.refresh(row)

    await _sync_manifest_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_target_created", row.name)
    return await _out(row, _live_statuses())


@router.put("/{target_id}", response_model=SitawareTargetOut)
async def update_target(
    target_id: str,
    target_in: SitawareTargetUpdate,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    row = await db.get(SitawareTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SitaWare target not found")

    if target_in.url is not None:
        row.url = target_in.url
    if target_in.url_fallback is not None or "url_fallback" in target_in.model_fields_set:
        row.url_fallback = target_in.url_fallback
    if target_in.url_tailscale is not None or "url_tailscale" in target_in.model_fields_set:
        row.url_tailscale = target_in.url_tailscale
    if target_in.enabled is not None:
        row.enabled = target_in.enabled
    if target_in.username and target_in.password:
        set_target_secret(row.id, target_in.username, target_in.password)

    await db.commit()
    await db.refresh(row)

    await _sync_manifest_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_target_updated", row.name)
    return await _out(row, _live_statuses())


@router.delete("/{target_id}")
async def delete_target(
    target_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    row = await db.get(SitawareTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SitaWare target not found")
    name = row.name
    await db.delete(row)
    await db.commit()
    remove_target_secret(target_id)

    await _sync_manifest_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_target_deleted", name)
    return {"status": "deleted"}
