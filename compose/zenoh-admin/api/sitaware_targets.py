"""CRUD for independent SitaWare HQ endpoints — both directions.

Ingress: EFDI polls one or more SitaWare HQ instances over REST
(bridges/sitaware_bridge.py). Egress: one or more SitaWare HQ instances poll
EFDI's NVG 2.0.2 feed (layers/sitaware_layer.py). Each enabled row of either
kind gets its own independent host process, reconciled by admin_control.py —
this module is the only writer of the (up to) four files that hand
configuration off to it:

  EFDI_SITAWARE_TARGETS_MANIFEST_PATH   ingress non-secret desired state
  EFDI_SITAWARE_TARGET_SECRET_PATH      ingress per-target username/password
  EFDI_SITAWARE_EGRESS_MANIFEST_PATH    egress non-secret desired state
  EFDI_SITAWARE_EGRESS_SECRET_PATH      egress per-target username/password

All four live under the same POD_STATE_DIR/zenoh bind mount link_secrets.py
already uses, so admin_control.py (host-side, outside Docker) can read them
without a database connection of its own — see that module's
_reconcile_sitaware_targets() / _reconcile_sitaware_egress_targets().

Settings that don't vary per target (ingress: API path, poll interval,
discover mode, TLS verify; egress: TLS cert/key, staleness threshold, max
tracks, anonymous/insecure-http policy) are deliberately NOT modeled here —
they stay shared deployment .env settings, edited via IntegrationSettings'
"SitaWare HQ" group, and are inherited by every spawned process."""

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
from .models import SitawareEgressTarget, SitawareIngressTarget
from . import sitaware_egress_secrets
from . import sitaware_target_secrets as sitaware_ingress_secrets

router = APIRouter(prefix="/api/sitaware-targets", tags=["sitaware-targets"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_INGRESS_MANIFEST_PATH = Path(os.environ.get(
    "EFDI_SITAWARE_TARGETS_MANIFEST_PATH", "/zenoh-config/sitaware-targets.json"))
_EGRESS_MANIFEST_PATH = Path(os.environ.get(
    "EFDI_SITAWARE_EGRESS_MANIFEST_PATH", "/zenoh-config/sitaware-egress-targets.json"))


def _check_name(v: str) -> str:
    v = v.strip().lower()
    if not _NAME_RE.match(v):
        raise ValueError(
            "name must be lowercase letters, digits, and internal hyphens only "
            "(this becomes the process name and, for ingress, the Zenoh topic segment)"
        )
    return v


def _check_url(v: str) -> str:
    v = v.strip()
    if not (v.startswith("https://") or v.startswith("http://")):
        raise ValueError("url must start with http:// or https://")
    return v.rstrip("/")


def _check_optional_url(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    return _check_url(v)


def _check_port(v: int) -> int:
    if not (1 <= v <= 65535):
        raise ValueError("port must be between 1 and 65535")
    return v


def _check_path(v: str) -> str:
    v = v.strip()
    if not v.startswith("/") or "?" in v or "#" in v:
        raise ValueError("path must be an absolute path without query or fragment, e.g. /nvg")
    return v


# --------------------------------------------------------------------------
# Ingress (EFDI -> polls -> SitaWare HQ)
# --------------------------------------------------------------------------

class SitawareIngressIn(BaseModel):
    name: str
    url: str
    url_fallback: str | None = None
    url_tailscale: str | None = None
    username: str = ""
    password: str = ""
    enabled: bool = True

    _check_name = field_validator("name")(classmethod(lambda cls, v: _check_name(v)))
    _check_url = field_validator("url")(classmethod(lambda cls, v: _check_url(v)))
    _check_optional_url = field_validator("url_fallback", "url_tailscale")(classmethod(lambda cls, v: _check_optional_url(v)))


class SitawareIngressUpdate(BaseModel):
    url: str | None = None
    url_fallback: str | None = None
    url_tailscale: str | None = None
    username: str | None = None
    password: str | None = None
    enabled: bool | None = None

    _check_url = field_validator("url")(classmethod(lambda cls, v: _check_url(v) if v is not None else v))
    _check_optional_url = field_validator("url_fallback", "url_tailscale")(classmethod(lambda cls, v: _check_optional_url(v)))


class SitawareIngressOut(BaseModel):
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


def _write_ingress_manifest(rows: list[SitawareIngressTarget]) -> None:
    _INGRESS_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "id": row.id, "name": row.name, "url": row.url,
            "url_fallback": row.url_fallback, "url_tailscale": row.url_tailscale,
            "enabled": row.enabled,
        }
        for row in rows
    ]
    tmp = _INGRESS_MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, _INGRESS_MANIFEST_PATH)


async def _sync_ingress_and_reconcile(db: AsyncSession) -> None:
    result = await db.execute(select(SitawareIngressTarget))
    _write_ingress_manifest(list(result.scalars().all()))
    try:
        _control("/v1/sitaware-targets/reconcile", method="POST")
    except HTTPException:
        pass  # config is persisted; admin_control.py also reconciles at its own startup


def _live_ingress_statuses() -> dict:
    try:
        return _control("/v1/sitaware-targets/status")
    except HTTPException:
        return {}


async def _ingress_out(row: SitawareIngressTarget, statuses: dict) -> SitawareIngressOut:
    return SitawareIngressOut(
        id=row.id, name=row.name, url=row.url,
        url_fallback=row.url_fallback, url_tailscale=row.url_tailscale,
        enabled=row.enabled, created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        has_credentials=sitaware_ingress_secrets.get_target_secret(row.id) is not None,
        running=statuses.get(row.id, {}).get("running"),
    )


@router.get("/ingress", response_model=list[SitawareIngressOut])
async def list_ingress_targets(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    result = await db.execute(select(SitawareIngressTarget).order_by(SitawareIngressTarget.created_at))
    statuses = _live_ingress_statuses()
    return [await _ingress_out(row, statuses) for row in result.scalars().all()]


@router.post("/ingress", response_model=SitawareIngressOut)
async def create_ingress_target(
    target_in: SitawareIngressIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    existing = await db.execute(select(SitawareIngressTarget).where(SitawareIngressTarget.name == target_in.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"An ingress target named '{target_in.name}' already exists")

    row = SitawareIngressTarget(
        name=target_in.name, url=target_in.url,
        url_fallback=target_in.url_fallback, url_tailscale=target_in.url_tailscale,
        enabled=target_in.enabled, created_by=actor.id,
    )
    db.add(row)
    await db.flush()
    if target_in.username and target_in.password:
        sitaware_ingress_secrets.set_target_secret(row.id, target_in.username, target_in.password)
    await db.commit()
    await db.refresh(row)

    await _sync_ingress_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_ingress_target_created", row.name)
    return await _ingress_out(row, _live_ingress_statuses())


@router.put("/ingress/{target_id}", response_model=SitawareIngressOut)
async def update_ingress_target(
    target_id: str,
    target_in: SitawareIngressUpdate,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    row = await db.get(SitawareIngressTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SitaWare ingress target not found")

    fields_set = target_in.model_fields_set
    if target_in.url is not None:
        row.url = target_in.url
    if "url_fallback" in fields_set:
        row.url_fallback = target_in.url_fallback
    if "url_tailscale" in fields_set:
        row.url_tailscale = target_in.url_tailscale
    if target_in.enabled is not None:
        row.enabled = target_in.enabled
    if target_in.username and target_in.password:
        sitaware_ingress_secrets.set_target_secret(row.id, target_in.username, target_in.password)

    await db.commit()
    await db.refresh(row)

    await _sync_ingress_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_ingress_target_updated", row.name)
    return await _ingress_out(row, _live_ingress_statuses())


@router.delete("/ingress/{target_id}")
async def delete_ingress_target(
    target_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    row = await db.get(SitawareIngressTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SitaWare ingress target not found")
    name = row.name
    await db.delete(row)
    await db.commit()
    sitaware_ingress_secrets.remove_target_secret(target_id)

    await _sync_ingress_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_ingress_target_deleted", name)
    return {"status": "deleted"}


# --------------------------------------------------------------------------
# Egress (SitaWare HQ -> polls -> EFDI's NVG feed)
# --------------------------------------------------------------------------

class SitawareEgressIn(BaseModel):
    name: str
    bind: str = "0.0.0.0"
    port: int
    path: str = "/nvg"
    username: str = ""
    password: str = ""
    enabled: bool = True

    _check_name = field_validator("name")(classmethod(lambda cls, v: _check_name(v)))
    _check_port = field_validator("port")(classmethod(lambda cls, v: _check_port(v)))
    _check_path = field_validator("path")(classmethod(lambda cls, v: _check_path(v)))


class SitawareEgressUpdate(BaseModel):
    bind: str | None = None
    port: int | None = None
    path: str | None = None
    username: str | None = None
    password: str | None = None
    enabled: bool | None = None

    _check_port = field_validator("port")(classmethod(lambda cls, v: _check_port(v) if v is not None else v))
    _check_path = field_validator("path")(classmethod(lambda cls, v: _check_path(v) if v is not None else v))


class SitawareEgressOut(BaseModel):
    id: str
    name: str
    bind: str
    port: int
    path: str
    enabled: bool
    created_by: str
    created_at: str
    has_credentials: bool
    running: bool | None = None

    class Config:
        from_attributes = True


def _write_egress_manifest(rows: list[SitawareEgressTarget]) -> None:
    _EGRESS_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest = [
        {
            "id": row.id, "name": row.name, "bind": row.bind,
            "port": row.port, "path": row.path, "enabled": row.enabled,
        }
        for row in rows
    ]
    tmp = _EGRESS_MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, _EGRESS_MANIFEST_PATH)


async def _sync_egress_and_reconcile(db: AsyncSession) -> None:
    result = await db.execute(select(SitawareEgressTarget))
    _write_egress_manifest(list(result.scalars().all()))
    try:
        _control("/v1/sitaware-egress-targets/reconcile", method="POST")
    except HTTPException:
        pass


def _live_egress_statuses() -> dict:
    try:
        return _control("/v1/sitaware-egress-targets/status")
    except HTTPException:
        return {}


async def _egress_out(row: SitawareEgressTarget, statuses: dict) -> SitawareEgressOut:
    return SitawareEgressOut(
        id=row.id, name=row.name, bind=row.bind, port=row.port, path=row.path,
        enabled=row.enabled, created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        has_credentials=sitaware_egress_secrets.get_target_secret(row.id) is not None,
        running=statuses.get(row.id, {}).get("running"),
    )


@router.get("/egress", response_model=list[SitawareEgressOut])
async def list_egress_targets(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    result = await db.execute(select(SitawareEgressTarget).order_by(SitawareEgressTarget.created_at))
    statuses = _live_egress_statuses()
    return [await _egress_out(row, statuses) for row in result.scalars().all()]


@router.post("/egress", response_model=SitawareEgressOut)
async def create_egress_target(
    target_in: SitawareEgressIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    existing = await db.execute(select(SitawareEgressTarget).where(SitawareEgressTarget.name == target_in.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"An egress target named '{target_in.name}' already exists")

    row = SitawareEgressTarget(
        name=target_in.name, bind=target_in.bind, port=target_in.port, path=target_in.path,
        enabled=target_in.enabled, created_by=actor.id,
    )
    db.add(row)
    await db.flush()
    if target_in.username and target_in.password:
        sitaware_egress_secrets.set_target_secret(row.id, target_in.username, target_in.password)
    await db.commit()
    await db.refresh(row)

    await _sync_egress_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_egress_target_created", row.name)
    return await _egress_out(row, _live_egress_statuses())


@router.put("/egress/{target_id}", response_model=SitawareEgressOut)
async def update_egress_target(
    target_id: str,
    target_in: SitawareEgressUpdate,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    row = await db.get(SitawareEgressTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SitaWare egress target not found")

    if target_in.bind is not None:
        row.bind = target_in.bind
    if target_in.port is not None:
        row.port = target_in.port
    if target_in.path is not None:
        row.path = target_in.path
    if target_in.enabled is not None:
        row.enabled = target_in.enabled
    if target_in.username and target_in.password:
        sitaware_egress_secrets.set_target_secret(row.id, target_in.username, target_in.password)

    await db.commit()
    await db.refresh(row)

    await _sync_egress_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_egress_target_updated", row.name)
    return await _egress_out(row, _live_egress_statuses())


@router.delete("/egress/{target_id}")
async def delete_egress_target(
    target_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    row = await db.get(SitawareEgressTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SitaWare egress target not found")
    name = row.name
    await db.delete(row)
    await db.commit()
    sitaware_egress_secrets.remove_target_secret(target_id)

    await _sync_egress_and_reconcile(db)
    await write_audit(db, actor.id, "sitaware_egress_target_deleted", name)
    return {"status": "deleted"}
