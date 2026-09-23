"""First-identity cert upload: switches the pod from its plaintext bootstrap
listener (see install.sh's unconditional bootstrap config.json5 write) to
real mTLS.

Distinct from pki.py, which issues delegated identities to CHILD routers
once this pod already has its own identity — this module is what gets this
pod its very FIRST identity, from zero prior cert material, over the WebUI.
"""

import os
import re
import tempfile

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .config import (
    CONFIG_PATH,
    ConfigFields,
    _TLS_PROFILES,
    _data_topic_root,
    _is_bootstrap_config,
    _render_config,
    apply_rendered_config,
)
from .db import get_db
from .deps import require_role, write_audit

router = APIRouter(prefix="/api/certs", tags=["certs"])
_superadmin = require_role("superadmin")

# The profile this endpoint switches a pod to after a successful upload —
# must be the one whose listen/connect cert paths are the exact files
# written below (/etc/zenoh/tls/{ca-roots,pod-cert,pod-key}.pem), and must
# never be a plaintext profile (config.py's _TLS_PROFILES "plaintext" flag)
# — a plaintext target would apply a config that ignores what was just
# uploaded, leaving the router silently still unsecured. Named as a module
# constant, not an inline literal, so tests/test_managed_router.py can
# assert this invariant directly instead of it silently drifting.
_BOOTSTRAP_TLS_PROFILE = "efdi"

# Router's own listen/connect identity — read-only mounted into zenoh-router
# at /etc/zenoh/tls (see config.py's _TLS_PROFILES[_BOOTSTRAP_TLS_PROFILE]).
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

    # .public_numbers() only exists on RSA/EC keys — crashes with a bare
    # AttributeError (confirmed live, uploading a real EFDI Backbone trial
    # fabric identity: its leaf keys are Ed25519) on any key type that isn't
    # one of those two. Comparing the algorithm-agnostic DER
    # SubjectPublicKeyInfo encoding instead works for every key type
    # `cryptography` supports, not just the two this fabric happened to be
    # tested against first.
    leaf_pub_der = leaf_cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key_pub_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if leaf_pub_der != key_pub_der:
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
        elif isinstance(ca_public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            # EdDSA signs the message directly — no separate hash algorithm
            # or padding parameter, unlike RSA/ECDSA above.
            ca_public_key.verify(leaf_cert.signature, leaf_cert.tbs_certificate_bytes)
        else:
            raise HTTPException(status_code=400, detail="unsupported CA key type")
    except InvalidSignature as exc:
        raise HTTPException(
            status_code=400, detail="certificate was not issued by the supplied CA root"
        ) from exc


def _write_pem(path: str, data: bytes, mode: int) -> None:
    # Replace via rename rather than truncating the target in place: a prior
    # identity at this exact path (e.g. host-generated by scripts/gen-certs.sh,
    # owned by the operator's uid, not this container's fixed uid) would
    # otherwise make every later O_TRUNC write here fail with EACCES even
    # though this container legitimately owns the directory. Rename only
    # needs write permission on the directory, not on the file being replaced.
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".bootstrap-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)  # mkstemp always creates 0600; enforce the real mode
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@router.post("/inspect")
async def inspect_certificate(
    certificate: UploadFile = File(...),
    _=Depends(require_role("admin", "superadmin")),
):
    """Read-only best-effort peek at an uploaded cert, before /bootstrap.

    Lets the WebUI prefill PARTNER_NAMESPACE from the certificate's own CN
    instead of making the operator run `openssl x509 -noout -subject`
    themselves and copy it in by hand — the convention this endpoint reads
    from is the same one /bootstrap writes: PARTNER_NAMESPACE becomes the
    "{partner_namespace}-cert.pem" filename stem, which is expected to match
    the CN the cert was actually issued for. No CA/key needed, no identity
    switch, nothing written to disk — just a parse.
    """
    cert_pem = await _read_pem(certificate, "certificate")
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError:
        return {"common_name": None}
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return {"common_name": str(names[0].value) if names else None}


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

    # _ROUTER_TLS_DIR is always a real bind-mount target inside this container
    # (derived from CONFIG_PATH, which the app itself needs to boot). _OWN_CERT_DIR
    # is operator-configurable (EFDI_CERT_DIR) and has been set to a *host* path
    # by mistake before (mirroring BUNDLE_DIR, which correctly is one) — that
    # doesn't exist inside this container's filesystem at all, so the mkdir
    # fails with a plain PermissionError/OSError. Without this, that surfaced as
    # a bare, content-free 500 with a raw traceback in the container logs and
    # nothing useful in the WebUI toast — same class of gotcha as
    # docs/11-troubleshooting.md's "unhandled exception in an API handler".
    try:
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
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not write certificate material to {exc.filename or _OWN_CERT_DIR!r}: {exc.strerror}. "
                "EFDI_CERT_DIR must be the path *inside* this container (compose's own default is "
                "/certs/efdi) — a host-side path (e.g. mirroring BUNDLE_DIR) will fail exactly like this."
            ),
        ) from exc

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
        fabric_tls_profile=_BOOTSTRAP_TLS_PROFILE,
    )
    rendered = _render_config(fields)

    # Must happen *before* apply_rendered_config's restart_native=True below —
    # confirmed live on zenoh-gateway: native bridge/layer scripts (start.sh)
    # read PARTNER_NAMESPACE/NAMESPACE_PREFIX fresh from .env at their own
    # restart, so restarting them before .env is updated silently republishes
    # them under the *old* identity. They come back up looking healthy
    # ("RUNNING") while actually still on the stale namespace — nothing
    # downstream (tak_layer, SitaWare, …) ever sees data under the new one.
    # Previously this ran only after apply_rendered_config, best-effort — that
    # ordering is exactly what reproduced the bug.
    try:
        _control_env_update({"PARTNER_NAMESPACE": partner_namespace, "NAMESPACE_PREFIX": namespace_prefix})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not update PARTNER_NAMESPACE/NAMESPACE_PREFIX before restarting native "
                    f"processes: {exc}. Not proceeding — restarting them against the old .env would "
                    f"silently republish under the previous namespace.",
        ) from exc

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

    # .env was already updated above (before the native-process restart). This
    # container's own env/bind-mounts are still fixed at create time though,
    # so it needs a recreate to pick up the new identity too — that tears
    # down the very container handling this request, so a failure here
    # (including the connection simply dying mid-response) is expected, not
    # fatal — the router-side switch above already succeeded.
    try:
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
