import hashlib
import json
import os
import re
import time

import zenoh
from .zenoh_auth import apply_zenoh_auth
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import ConfigFields, _render_config
from .db import get_db
from .deps import require_role, write_audit
from .federation_crypto import sign_payload
from .federation_paths import path_to
from .federation_relay import config_topic, relay_topic
from .config_revisions import create_revision, set_revision_state
from .models import FederatedChild

router = APIRouter(prefix="/api/federation", tags=["federation"])

_SAFE_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
_OWN_CERT_DIR = os.environ.get("EFDI_CERT_DIR", "")
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")


def _prefix() -> str:
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "EFDI")


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
    cert_sha256: str | None = None
    max_delegation_depth: int = 0

    class Config:
        from_attributes = True


class FederatedTargetPush(BaseModel):
    target_namespace: str
    fields: ConfigFields

    @field_validator("target_namespace")
    @classmethod
    def _check_target_namespace(cls, value: str) -> str:
        if not _SAFE_NAMESPACE_RE.fullmatch(value):
            raise ValueError("target namespace contains unsupported characters")
        return value


def _child_out(c: FederatedChild) -> FederatedChildOut:
    return FederatedChildOut(
        id=c.id, name=c.name, namespace=c.namespace,
        created_by=c.created_by, created_at=c.created_at.isoformat(),
        last_status=c.last_status, last_status_version=c.last_status_version,
        last_status_at=c.last_status_at.isoformat() if c.last_status_at else None,
        last_status_error=c.last_status_error,
        cert_sha256=c.cert_sha256,
        max_delegation_depth=c.max_delegation_depth,
    )


def _own_signing_key_path() -> str:
    return os.environ.get("EFDI_POLICY_SIGNER_KEY_PATH", "/certs/policy-signer-key.pem")


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
    apply_zenoh_auth(conf)
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
    raise HTTPException(
        status_code=410,
        detail="manual federation rows are disabled; enroll the child through Certificate Authority",
    )


@router.delete("/{child_id}")
async def delete_child(
    child_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    raise HTTPException(
        status_code=410,
        detail="deletion is disabled; quarantine or decommission the managed authority",
    )


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
    return await _push_to_target(child.namespace, fields, db, actor.id)


@router.post("/push-config")
async def push_target_config(
    request: FederatedTargetPush,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    """Push to any proven descendant through direct-child re-signing hops."""
    return await _push_to_target(request.target_namespace, request.fields, db, actor.id)


async def _push_to_target(
    target_namespace: str,
    fields: ConfigFields,
    db: AsyncSession,
    actor_id: str,
) -> dict:
    if fields.partner_namespace != target_namespace:
        raise HTTPException(
            status_code=422,
            detail=(
                "Remote config partner_namespace must equal the selected target namespace; "
                "refusing a candidate that could overwrite the child's identity"
            ),
        )

    direct_result = await db.execute(
        select(FederatedChild).where(FederatedChild.namespace == target_namespace)
    )
    direct_target = direct_result.scalar_one_or_none()
    path = [target_namespace] if direct_target is not None else path_to(target_namespace)
    if not path:
        raise HTTPException(status_code=409, detail="Target is not a proven descendant in the live topology")
    first_hop_result = await db.execute(
        select(FederatedChild).where(FederatedChild.namespace == path[0])
    )
    first_hop = first_hop_result.scalar_one_or_none()
    if first_hop is None:
        raise HTTPException(
            status_code=403,
            detail="Topology path does not begin with a locally registered direct child",
        )

    # version is assigned before the try block so it's always available for
    # the failure-path audit entry below, even if rendering itself fails.
    # Milliseconds avoid same-second collisions while remaining exactly
    # representable by JavaScript and safely inside SQL BIGINT.
    version = int(time.time() * 1000)
    revision = None
    try:
        rendered = _render_config(fields)
        if len(rendered.encode("utf-8")) > 256 * 1024:
            raise HTTPException(status_code=422, detail="Rendered config exceeds the 256 KiB relay limit")
        if len(path) == 1:
            payload = {"config": rendered, "version": version, "signed_at": time.time()}
            topic = config_topic(target_namespace)
            delivery = "direct"
        else:
            payload = {
                "path": path,
                "config": rendered,
                "version": version,
                "signed_at": time.time(),
            }
            topic = relay_topic(path[0])
            delivery = "relay"

        revision = await create_revision(
            db,
            target_namespace=target_namespace,
            version=version,
            source=delivery,
            state="validating",
            config_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
            created_by=actor_id,
        )

        key_path = _own_signing_key_path()
        if not os.path.isfile(key_path):
            raise HTTPException(status_code=500, detail=f"Own signing key not found at {key_path}")
        with open(key_path, "rb") as f:
            key_pem = f.read()

        signature = sign_payload(payload, key_pem, purpose="config" if delivery == "direct" else "relay")
        envelope = {"payload": payload, "signature": signature}

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
        if revision is not None:
            await set_revision_state(db, revision, "failed", detail)
        await write_audit(
            db,
            actor_id,
            "federation_config_push_failed",
            f"target={target_namespace}, version={version}, error={detail}",
        )
        raise

    await set_revision_state(db, revision, "pending")

    await write_audit(
        db,
        actor_id,
        "federation_config_pushed",
        f"target={target_namespace}, first_hop={path[0]}, hops={len(path)}, version={version}",
    )

    return {
        "status": "pushed",
        "version": version,
        "topic": topic,
        "delivery": delivery,
        "path": path,
        "revision_id": revision.id,
    }
