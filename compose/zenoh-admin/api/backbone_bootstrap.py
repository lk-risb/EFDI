"""Backbone identity upload: gives the dedicated zenoh-router-backbone
container its mTLS identity for the EFDI Backbone trial fabric — and ALSO
stages the same identity for the primary router's own "backbone" TLS
profile slot, so the Zenoh Config page's "Backbone" fabric preset pill
works as a deliberate, one-at-a-time swap of the primary router itself
(distinct from, and in addition to, the always-on dedicated router below).

Distinct from certs_bootstrap.py, which bootstraps THIS pod's own EFDI-CA
identity on the primary zenoh-router's DEFAULT ("efdi") profile. Zenoh 1.x
applies one TLS identity to the whole router session (see config.py's
_TLS_PROFILES header comment), so a router can only ever be on ONE of
these profiles at a time — selecting "Backbone" on the Config page drops
the primary router's EFDI identity (and with it, zenoh1/zenoh2
connectivity) for as long as it's selected. That tradeoff is the operator's
deliberate choice via that page's pill, not something this upload endpoint
decides; this endpoint only makes the swap actually WORK once chosen,
instead of failing with "Zenoh transport not established" because the
profile's cert slot was never populated. See
examples/zenoh-router-backbone.json5.tmpl for the SEPARATE dedicated
router's own config, which stays on unaffected either way.
"""

import json
import os

import json5
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .certs_bootstrap import _ROUTER_TLS_DIR, _load_identity, _read_pem, _write_pem
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


def _parse_backbone_endpoint(raw: str) -> list[str]:
    """A single endpoint string, or a JSON array of them, direct from the
    upload form — never from this container's own os.environ. Endpoint
    changes made via Integration Settings only reach the LIVE zenoh-admin
    process on its next container recreate (env is baked in at container
    create time — same class of staleness certs_bootstrap.py's own ordering
    fix addresses elsewhere), so depending on that env var here meant a
    freshly-changed value silently kept 409'ing until an unrelated recreate
    happened to occur. Taking it as this form's own field sidesteps the
    staleness entirely."""
    raw = raw.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="backbone_endpoint is required")
    if raw.startswith("["):
        try:
            endpoints = json5.loads(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"backbone_endpoint is not a valid JSON array: {exc}") from exc
        if not isinstance(endpoints, list) or any(not isinstance(item, str) for item in endpoints):
            raise HTTPException(status_code=400, detail="backbone_endpoint array must contain only strings")
    else:
        endpoints = [raw]
    if not endpoints:
        raise HTTPException(status_code=400, detail="backbone_endpoint is required")
    ConfigFields._check_safe_endpoints(endpoints)
    return endpoints


def _render_backbone_config(backbone_endpoints: list[str]) -> str:
    if not os.path.isfile(_BACKBONE_TEMPLATE_PATH):
        raise HTTPException(
            status_code=500, detail=f"Template not found at {_BACKBONE_TEMPLATE_PATH}"
        )
    with open(_BACKBONE_TEMPLATE_PATH, "r") as f:
        rendered = f.read()

    connect_endpoints = [_local_router_endpoint(), *backbone_endpoints]
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
    has_config = os.path.isfile(_BACKBONE_CONFIG_PATH)
    return {"identity_uploaded": has_identity, "configured": has_config}


@router.post("/bootstrap")
async def upload_backbone_identity(
    ca_root: UploadFile = File(...),
    certificate: UploadFile = File(...),
    private_key: UploadFile = File(...),
    backbone_endpoint: str = Form(...),
    db: AsyncSession = Depends(get_db),
    actor=Depends(_superadmin),
):
    backbone_endpoints = _parse_backbone_endpoint(backbone_endpoint)
    ca_pem = await _read_pem(ca_root, "CA root")
    cert_pem = await _read_pem(certificate, "certificate")
    key_pem = await _read_pem(private_key, "private key")
    _load_identity(ca_pem, cert_pem, key_pem)

    # Written for display/consistency on the Integration Settings page —
    # this endpoint no longer depends on it being read back correctly (see
    # _parse_backbone_endpoint above), so a failure here is non-fatal.
    try:
        _control_env_update({
            "EFDI_BACKBONE_FABRIC_ENDPOINTS": json.dumps(backbone_endpoints),
            "EFDI_BACKBONE_FABRIC_PROFILE": "backbone",
        })
    except Exception:  # noqa: BLE001 — best-effort, see comment above
        pass

    # Primary router's own "backbone" TLS profile slot (config.py's
    # _TLS_PROFILES["backbone"]: connect_certificate/_private_key/root_ca at
    # exactly these paths) — populated so the Zenoh Config page's "Backbone"
    # fabric preset pill actually works when an operator deliberately
    # selects it, instead of failing with "Zenoh transport not established"
    # because this slot was never provisioned. Separate from, and in
    # addition to, the dedicated router's own identity below.
    _primary_router_backbone_tls_dir = os.path.join(_ROUTER_TLS_DIR, "backbone")
    try:
        os.makedirs(_BACKBONE_TLS_DIR, exist_ok=True)
        _write_pem(os.path.join(_BACKBONE_TLS_DIR, "ca-roots.pem"), ca_pem, 0o644)
        _write_pem(os.path.join(_BACKBONE_TLS_DIR, "cert.pem"), cert_pem, 0o644)
        _write_pem(os.path.join(_BACKBONE_TLS_DIR, "key.pem"), key_pem, 0o600)

        os.makedirs(_primary_router_backbone_tls_dir, exist_ok=True)
        _write_pem(os.path.join(_primary_router_backbone_tls_dir, "ca-roots.pem"), ca_pem, 0o644)
        _write_pem(os.path.join(_primary_router_backbone_tls_dir, "cert.pem"), cert_pem, 0o644)
        _write_pem(os.path.join(_primary_router_backbone_tls_dir, "key.pem"), key_pem, 0o600)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write backbone certificate material to "
                   f"{exc.filename or _BACKBONE_TLS_DIR!r}: {exc.strerror}.",
        ) from exc

    rendered = _render_backbone_config(backbone_endpoints)
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

    # Best-effort, like the env update above: none of these are the
    # connection itself, just what makes the data flowing across it useful —
    # backbone-bridge/backbone_layer (raw ingress/egress to the fabric),
    # json/geojson (the normalizers backbone-bridge's raw output feeds), and
    # asterix (cat34/cat48's --zenoh-raw mode turns on once this identity
    # file exists — see start.sh's asterix_category_uses_raw). A
    # control-agent hiccup on any one of these must not fail an otherwise-
    # successful identity upload.
    for service in ("backbone-bridge", "backbone_layer", "generic_json", "geojson", "asterix"):
        try:
            _control_start_service(service)
        except Exception:  # noqa: BLE001
            pass

    await write_audit(db, actor.id, "backbone_bootstrap_applied", "zenoh-router-backbone started")
    return {"status": "applied", "container_output": result.get("output", "")}


def _control_env_update(values: dict[str, str]) -> dict:
    from .control import _control
    return _control("/v1/config", method="PUT", body={"values": values})


def _control_start_backbone_router() -> dict:
    from .control import _control
    # admin_control.py's own subprocess timeout for this is 60s (a cold
    # `docker compose up` may need to pull/build) — must exceed that or a
    # slow-but-succeeding start reports as a false failure client-side.
    return _control("/v1/containers/zenoh-router-backbone/start", method="POST", timeout=70)


def _control_start_service(name: str) -> dict:
    from .control import _control
    return _control(f"/v1/services/{name}/start", method="POST", timeout=20)
