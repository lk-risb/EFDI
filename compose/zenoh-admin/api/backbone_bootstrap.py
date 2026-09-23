"""Backbone identity upload: gives the dedicated zenoh-router-backbone
container its mTLS identity for the EFDI Backbone trial fabric.

Distinct from certs_bootstrap.py, which bootstraps THIS pod's own EFDI-CA
identity on the primary zenoh-router. Zenoh 1.x applies one TLS identity to
the whole router session (see config.py's _TLS_PROFILES header comment), so
the backbone's own Desert-Bread-CA identity cannot live on the primary
router alongside the EFDI-CA identity zenoh1/zenoh2 verify — it needs its
own router process, and that process needs its own cert upload path. See
examples/zenoh-router-backbone.json5.tmpl for the config this renders.
"""

import json
import os

import json5
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .certs_bootstrap import _load_identity, _read_pem, _write_pem
from .config import ConfigFields
from .db import get_db
from .deps import require_role, write_audit

router = APIRouter(prefix="/api/certs/backbone", tags=["certs"])
_superadmin = require_role("superadmin")

# Mirrors certs_bootstrap.py's own _ROUTER_TLS_DIR/_BACKBONE_CONFIG_PATH split:
# zenoh-admin mounts this dir read-write, zenoh-router-backbone mounts the
# same host path read-only at /etc/zenoh (see docker-compose.yml).
_BACKBONE_CONFIG_PATH = os.environ.get(
    "ZENOH_BACKBONE_CONFIG_PATH", "/zenoh-config-backbone/config.json5"
)
_BACKBONE_TLS_DIR = os.path.join(os.path.dirname(_BACKBONE_CONFIG_PATH), "tls")
_BACKBONE_TEMPLATE_PATH = os.environ.get(
    "ZENOH_BACKBONE_TEMPLATE_PATH",
    "/zenoh-config-template/zenoh-router-backbone.json5.tmpl",
)


def _local_router_endpoint() -> str:
    port = os.environ.get("ZENOH_LOCAL_TCP_PORT", "7448")
    return "tcp/127.0.0.1:{}".format(port)


def _backbone_fabric_endpoints() -> list[str]:
    raw = os.environ.get("EFDI_BACKBONE_FABRIC_ENDPOINTS", "[]")
    try:
        endpoints = json5.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"EFDI_BACKBONE_FABRIC_ENDPOINTS is not a JSON array: {exc}",
        ) from exc
    if not isinstance(endpoints, list) or any(not isinstance(item, str) for item in endpoints):
        raise HTTPException(
            status_code=500,
            detail="EFDI_BACKBONE_FABRIC_ENDPOINTS must be a JSON array of endpoint strings",
        )
    if not endpoints:
        raise HTTPException(
            status_code=409,
            detail="EFDI_BACKBONE_FABRIC_ENDPOINTS is empty — set the backbone router "
                   "endpoint (e.g. tls/zenoh.efdi.netbird.efdi-backbone.net:7447) in "
                   "Integration Settings before uploading a backbone identity.",
        )
    ConfigFields._check_safe_endpoints(endpoints)
    return endpoints


def _render_backbone_config() -> str:
    if not os.path.isfile(_BACKBONE_TEMPLATE_PATH):
        raise HTTPException(
            status_code=500, detail=f"Template not found at {_BACKBONE_TEMPLATE_PATH}"
        )
    with open(_BACKBONE_TEMPLATE_PATH, "r") as f:
        rendered = f.read()

    connect_endpoints = [_local_router_endpoint(), *_backbone_fabric_endpoints()]
    rendered = rendered.replace(
        "${ZENOH_BACKBONE_CONNECT_ENDPOINTS}", json.dumps(connect_endpoints)
    )
    return rendered


@router.get("/status")
async def backbone_status(_=Depends(require_role("admin", "superadmin"))):
    has_identity = all(
        os.path.isfile(os.path.join(_BACKBONE_TLS_DIR, name))
        for name in ("ca-roots.pem", "cert.pem", "key.pem")
    )
    endpoints_configured = bool(json5.loads(os.environ.get("EFDI_BACKBONE_FABRIC_ENDPOINTS", "[]")))
    return {"identity_uploaded": has_identity, "endpoints_configured": endpoints_configured}


@router.post("/bootstrap")
async def upload_backbone_identity(
    ca_root: UploadFile = File(...),
    certificate: UploadFile = File(...),
    private_key: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    actor=Depends(_superadmin),
):
    ca_pem = await _read_pem(ca_root, "CA root")
    cert_pem = await _read_pem(certificate, "certificate")
    key_pem = await _read_pem(private_key, "private key")
    _load_identity(ca_pem, cert_pem, key_pem)

    # Fail before writing anything if the remote endpoint isn't configured —
    # a router with an identity but nowhere to dial would just crash-loop.
    _backbone_fabric_endpoints()

    try:
        os.makedirs(_BACKBONE_TLS_DIR, exist_ok=True)
        _write_pem(os.path.join(_BACKBONE_TLS_DIR, "ca-roots.pem"), ca_pem, 0o644)
        _write_pem(os.path.join(_BACKBONE_TLS_DIR, "cert.pem"), cert_pem, 0o644)
        _write_pem(os.path.join(_BACKBONE_TLS_DIR, "key.pem"), key_pem, 0o600)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write backbone certificate material to "
                   f"{exc.filename or _BACKBONE_TLS_DIR!r}: {exc.strerror}.",
        ) from exc

    rendered = _render_backbone_config()
    try:
        os.makedirs(os.path.dirname(_BACKBONE_CONFIG_PATH), exist_ok=True)
        with open(_BACKBONE_CONFIG_PATH, "w") as f:
            f.write(rendered)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write {_BACKBONE_CONFIG_PATH}: {exc.strerror}.",
        ) from exc

    result = _control_start_backbone_router()
    if not result.get("ok"):
        await write_audit(
            db, actor.id, "backbone_bootstrap_failed", result.get("output", "unknown error")
        )
        raise HTTPException(
            status_code=409,
            detail=f"Backbone router container did not start: {result.get('output')}",
        )

    await write_audit(db, actor.id, "backbone_bootstrap_applied", "zenoh-router-backbone started")
    return {"status": "applied", "container_output": result.get("output", "")}


def _control_start_backbone_router() -> dict:
    from .control import _control
    return _control("/v1/containers/zenoh-router-backbone/start", method="POST")
