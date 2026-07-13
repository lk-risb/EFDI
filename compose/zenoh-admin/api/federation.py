import json
import os
import re
import time

import zenoh
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import ConfigFields, _render_config
from .db import get_db
from .deps import require_role, write_audit
from .federation_crypto import sign_payload
from .models import FederatedChild

router = APIRouter(prefix="/api/federation", tags=["federation"])

_SAFE_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
_OWN_CERT_DIR = os.environ.get("EFDI_CERT_DIR", "")


class FederatedChildIn(BaseModel):
    name: str
    namespace: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v

    @field_validator("namespace")
    @classmethod
    def _check_namespace(cls, v: str) -> str:
        if not _SAFE_NAMESPACE_RE.match(v):
            raise ValueError("namespace: only letters, digits, '.', '_', '/', '-' are allowed")
        return v


class FederatedChildOut(BaseModel):
    id: str
    name: str
    namespace: str
    created_by: str
    created_at: str
    last_status: str | None = None
    last_status_version: int | None = None
    last_status_at: str | None = None
    last_status_error: str | None = None

    class Config:
        from_attributes = True


def _child_out(c: FederatedChild) -> FederatedChildOut:
    return FederatedChildOut(
        id=c.id, name=c.name, namespace=c.namespace,
        created_by=c.created_by, created_at=c.created_at.isoformat(),
        last_status=c.last_status, last_status_version=c.last_status_version,
        last_status_at=c.last_status_at.isoformat() if c.last_status_at else None,
        last_status_error=c.last_status_error,
    )


def _own_signing_key_path() -> str:
    return os.path.join(_OWN_CERT_DIR, f"{_OWN_NAMESPACE}-key.pem")


def _publish_endpoint() -> str:
    return os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")


def _open_publish_session() -> "zenoh.Session":
    """One-shot session for a push — federation pushes are infrequent
    (an operator action), unlike the persistent subscriber session in
    federation_apply.py, so a fresh session per push keeps this module
    independent of that one's lifecycle."""
    endpoint = _publish_endpoint()
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([endpoint]))
    if endpoint.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_OWN_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_OWN_CERT_DIR, _OWN_NAMESPACE + "-cert.pem"),
            "connect_private_key": os.path.join(_OWN_CERT_DIR, _OWN_NAMESPACE + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return zenoh.open(conf)


@router.get("", response_model=list[FederatedChildOut])
async def list_children(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "superadmin")),
):
    result = await db.execute(select(FederatedChild).order_by(FederatedChild.created_at))
    return [_child_out(c) for c in result.scalars().all()]


@router.post("", response_model=FederatedChildOut)
async def create_child(
    child_in: FederatedChildIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    child = FederatedChild(name=child_in.name, namespace=child_in.namespace, created_by=actor.id)
    db.add(child)
    await db.commit()
    await db.refresh(child)

    # No ACL refresh needed — pod-federation-config is a mesh-wide
    # LTU/CISB/**/@config/** wildcard (see host/zenoh-router.json5.tmpl),
    # so every namespace is already transport-reachable. This row is pure
    # bookkeeping for the UI dropdown and for push-config's topic lookup.
    await write_audit(db, actor.id, "federation_child_created",
                       f"name={child.name}, namespace={child.namespace}")
    return _child_out(child)


@router.delete("/{child_id}")
async def delete_child(
    child_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    child = await db.get(FederatedChild, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="Federated child not found")
    name, namespace = child.name, child.namespace
    await db.delete(child)
    await db.commit()

    # No ACL narrowing needed — see create_child: the wildcard doesn't
    # change based on which children are registered.
    await write_audit(db, actor.id, "federation_child_deleted",
                       f"name={name}, namespace={namespace}")
    return {"status": "deleted"}


@router.post("/{child_id}/push-config")
async def push_config(
    child_id: str,
    fields: ConfigFields,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    child = await db.get(FederatedChild, child_id)
    if not child:
        raise HTTPException(status_code=404, detail="Federated child not found")

    # version is assigned before the try block so it's always available for
    # the failure-path audit entry below, even if rendering itself fails.
    version = int(time.time())
    try:
        rendered = _render_config(fields)
        payload = {"config": rendered, "version": version, "signed_at": time.time()}

        key_path = _own_signing_key_path()
        if not os.path.isfile(key_path):
            raise HTTPException(status_code=500, detail=f"Own signing key not found at {key_path}")
        with open(key_path, "rb") as f:
            key_pem = f.read()

        signature = sign_payload(payload, key_pem)
        envelope = {"payload": payload, "signature": signature}

        topic = f"LTU/CISB/{child.namespace}/@config/v1"
        session = _open_publish_session()
        try:
            session.put(topic, json.dumps(envelope).encode())
        finally:
            session.close()
    except Exception as exc:
        # Every apply attempt gets an audit entry, per this plan's global
        # constraint — not just the success path. Without this, a render
        # failure, missing key, signing error, or zenoh publish failure would
        # leave no trace at all, unlike the receiving side (federation_apply.py)
        # which deliberately audits every one of its own failure branches.
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        await write_audit(db, actor.id, "federation_config_push_failed",
                           f"child={child.name} ({child.namespace}), version={version}, error={detail}")
        raise

    await write_audit(db, actor.id, "federation_config_pushed",
                       f"child={child.name} ({child.namespace}), version={version}")

    return {"status": "pushed", "version": version, "topic": topic}
