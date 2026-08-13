"""First-identity cert upload: switches the pod from its plaintext bootstrap
listener (see install.sh's unconditional bootstrap config.json5 write) to
real mTLS.

Distinct from pki.py, which issues delegated identities to CHILD routers
once this pod already has its own identity — this module is what gets this
pod its very FIRST identity, from zero prior cert material, over the WebUI.
"""

import os
import re

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .config import (
    CONFIG_PATH,
    ConfigFields,
    _data_topic_root,
    _is_bootstrap_config,
    _render_config,
    apply_rendered_config,
)
from .db import get_db
from .deps import require_role, write_audit

router = APIRouter(prefix="/api/certs", tags=["certs"])
_superadmin = require_role("superadmin")

# Router's own listen/connect identity — read-only mounted into zenoh-router
# at /etc/zenoh/tls (see config.py's _TLS_PROFILES["efdi"]).
_ROUTER_TLS_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "tls")
# This container's own client identity — rw-mounted at EFDI_CERT_DIR (see
# docker-compose.yml).
_OWN_CERT_DIR = os.environ.get("EFDI_CERT_DIR", "/certs/efdi")

_SAFE_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_PEM_BYTES = 64 * 1024  # generous for a leaf cert, short chain, or RSA-4096 key


@router.get("/bootstrap/status")
async def bootstrap_status(_=Depends(require_role("admin", "superadmin"))):
    if not os.path.isfile(CONFIG_PATH):
        raise HTTPException(status_code=404, detail=f"Config file not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r") as f:
        raw = f.read()
    return {"bootstrap": _is_bootstrap_config(raw)}


async def _read_pem(upload: UploadFile, label: str) -> bytes:
    data = await upload.read(_MAX_PEM_BYTES + 1)
    if len(data) > _MAX_PEM_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} too large (max {_MAX_PEM_BYTES} bytes)")
    if b"-----BEGIN" not in data:
        raise HTTPException(status_code=400, detail=f"{label} does not look like PEM")
    return data


def _load_identity(ca_pem: bytes, cert_pem: bytes, key_pem: bytes) -> None:
    """Raise HTTPException unless cert/key match and the CA actually signed cert."""
    try:
        ca_cert = x509.load_pem_x509_certificate(ca_pem)
        leaf_cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid certificate: {exc}") from exc
    try:
        private_key = serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid or encrypted private key: {exc}") from exc

    if leaf_cert.public_key().public_numbers() != private_key.public_key().public_numbers():
        raise HTTPException(status_code=400, detail="certificate and private key do not match")

    ca_public_key = ca_cert.public_key()
    try:
        if isinstance(ca_public_key, rsa.RSAPublicKey):
            ca_public_key.verify(
                leaf_cert.signature, leaf_cert.tbs_certificate_bytes,
                padding.PKCS1v15(), leaf_cert.signature_hash_algorithm,
            )
        elif isinstance(ca_public_key, ec.EllipticCurvePublicKey):
            ca_public_key.verify(
                leaf_cert.signature, leaf_cert.tbs_certificate_bytes,
                ec.ECDSA(leaf_cert.signature_hash_algorithm),
            )
        else:
            raise HTTPException(status_code=400, detail="unsupported CA key type")
    except InvalidSignature as exc:
        raise HTTPException(
            status_code=400, detail="certificate was not issued by the supplied CA root"
        ) from exc


def _write_pem(path: str, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.chmod(path, mode)  # O_CREAT's mode is masked by umask; enforce it


@router.post("/bootstrap")
async def upload_bootstrap_identity(
    ca_root: UploadFile = File(...),
    certificate: UploadFile = File(...),
    private_key: UploadFile = File(...),
    partner_namespace: str = Form(...),
    namespace_prefix: str = Form("EFDI"),
    db: AsyncSession = Depends(get_db),
    actor=Depends(_superadmin),
):
    if not _SAFE_NAMESPACE_RE.match(partner_namespace):
        raise HTTPException(
            status_code=400,
            detail="partner_namespace: only letters, digits, '.', '_', '/', '-' are allowed",
        )
    if not _SAFE_NAMESPACE_RE.match(namespace_prefix):
        raise HTTPException(
            status_code=400,
            detail="namespace_prefix: only letters, digits, '.', '_', '/', '-' are allowed",
        )

    ca_pem = await _read_pem(ca_root, "CA root")
    cert_pem = await _read_pem(certificate, "certificate")
    key_pem = await _read_pem(private_key, "private key")
    _load_identity(ca_pem, cert_pem, key_pem)

    os.makedirs(_ROUTER_TLS_DIR, exist_ok=True)
    os.makedirs(_OWN_CERT_DIR, exist_ok=True)

    # Router's own listen/connect identity (consumed by zenoh-router itself).
    _write_pem(os.path.join(_ROUTER_TLS_DIR, "ca-roots.pem"), ca_pem, 0o644)
    _write_pem(os.path.join(_ROUTER_TLS_DIR, "pod-cert.pem"), cert_pem, 0o644)
    _write_pem(os.path.join(_ROUTER_TLS_DIR, "pod-key.pem"), key_pem, 0o600)

    # This container's own client identity, filename-keyed by namespace (see
    # local_zenoh.py, federation.py, pki.py — all read EFDI_CERT_DIR this way).
    _write_pem(os.path.join(_OWN_CERT_DIR, "efdi-ca-root.pem"), ca_pem, 0o644)
    _write_pem(os.path.join(_OWN_CERT_DIR, f"{partner_namespace}-cert.pem"), cert_pem, 0o644)
    _write_pem(os.path.join(_OWN_CERT_DIR, f"{partner_namespace}-key.pem"), key_pem, 0o600)

    fields = ConfigFields(
        mtls_port=int(os.environ.get("ZENOH_LISTEN_PORT", "7447")),
        local_tcp_port=int(os.environ.get("ZENOH_LOCAL_TCP_PORT", "7448")),
        fabric_endpoint="",
        fabric_endpoints=[],
        partner_namespace=partner_namespace,
        inbound_namespace=partner_namespace,
        namespace_prefix=namespace_prefix,
        publish_prefix=namespace_prefix,
        verify_name_on_connect=False,
        plugins_loading_enabled=True,
        fabric_tls_profile="efdi",
    )
    rendered = _render_config(fields)
    # No existing remote management link to preserve on a bootstrap → mTLS
    # switch — there is nothing federated yet, so skip that health proof.
    result = apply_rendered_config(rendered, fields, restart_native=True, preserve_management=False)
    if result["status"] != "applied":
        await write_audit(
            db, actor.id, "certs_bootstrap_failed",
            f"namespace={partner_namespace}: {result.get('error')}",
        )
        raise HTTPException(
            status_code=409,
            detail=f"Router did not come up on the new certs ({result['status']}): {result.get('error')}",
        )

    await write_audit(db, actor.id, "certs_bootstrap_applied", f"namespace={partner_namespace}")

    # PARTNER_NAMESPACE/NAMESPACE_PREFIX are baked into this container's own
    # env and bind-mount paths at create time — write them to .env and recreate
    # so this container itself picks up the new identity, not just the router.
    # The recreate tears down the very container handling this request, so a
    # failure here (including the connection simply dying mid-response) is
    # expected, not fatal — the router-side switch above already succeeded.
    try:
        _control_env_update({"PARTNER_NAMESPACE": partner_namespace, "NAMESPACE_PREFIX": namespace_prefix})
        _control_recreate_self()
        admin_recreate = "requested"
    except Exception as exc:  # noqa: BLE001 — best-effort; see comment above
        admin_recreate = f"failed: {exc}"

    return {
        "status": "applied",
        "partner_namespace": partner_namespace,
        "namespace_prefix": namespace_prefix,
        "admin_recreate": admin_recreate,
    }


def _control_env_update(values: dict[str, str]) -> dict:
    from .control import _control
    return _control("/v1/config", method="PUT", body={"values": values})


def _control_recreate_self() -> dict:
    from .control import _control
    return _control("/v1/containers/zenoh-admin/recreate", method="POST")
