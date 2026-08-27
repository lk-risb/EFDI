"""TAK Server mTLS client credential upload — the "Option B" bundle referenced
by compose/layers/tak_layer.py's own header comment (cert/key/CA PEM files,
not a TAK .zip data package; generate with `make add-service NAME=efdi-pod`
in the TAK repo).

tak_layer.py runs as a host-native process (started by start.sh), not inside
this container, so TAK_CERT/TAK_KEY/TAK_CA must end up as real host
filesystem paths. This mirrors certs_bootstrap.py's write pattern but reads
the host-equivalent path from EFDI_TAK_PACKAGE_HOST_DIR (see docker-compose.yml)
instead of using the in-container path directly, then reuses control.py's
already-authenticated link to the host control agent to persist those paths
into .env — the same seam update_runtime_config already uses.
"""

import os

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .control import _control
from .db import get_db
from .deps import require_role, write_audit

router = APIRouter(prefix="/api/integrations/tak", tags=["integrations"])

_PACKAGE_DIR = os.environ.get("EFDI_TAK_PACKAGE_DIR", "/integrations/tak")
_PACKAGE_HOST_DIR = os.environ.get("EFDI_TAK_PACKAGE_HOST_DIR", "") or _PACKAGE_DIR
_MAX_PEM_BYTES = 64 * 1024  # generous for a leaf cert, short chain, or RSA-4096 key


async def _read_pem(upload: UploadFile, label: str) -> bytes:
    data = await upload.read(_MAX_PEM_BYTES + 1)
    if len(data) > _MAX_PEM_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} too large (max {_MAX_PEM_BYTES} bytes)")
    if b"-----BEGIN" not in data:
        raise HTTPException(status_code=400, detail=f"{label} does not look like PEM")
    return data


def _write_pem(name: str, data: bytes, mode: int) -> str:
    try:
        os.makedirs(_PACKAGE_DIR, exist_ok=True, mode=0o700)
        target = os.path.join(_PACKAGE_DIR, name)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        finally:
            os.chmod(target, mode)  # O_CREAT's mode is masked by umask; enforce it
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write {name} to {_PACKAGE_DIR}: {exc}. "
            f"Check that this directory is writable by uid/gid 10001 (see install.sh).",
        ) from exc
    return os.path.join(_PACKAGE_HOST_DIR, name)


@router.post("")
async def upload_tak_package(
    ca_root: UploadFile | None = File(None),
    certificate: UploadFile | None = File(None),
    private_key: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    if not (ca_root or certificate or private_key):
        raise HTTPException(status_code=400, detail="upload at least one of CA root, certificate, or private key")

    values: dict[str, str] = {}
    if ca_root is not None:
        data = await _read_pem(ca_root, "CA root")
        try:
            x509.load_pem_x509_certificate(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"CA root is not a valid certificate: {exc}") from exc
        values["TAK_CA"] = _write_pem("ca.pem", data, 0o644)
    if certificate is not None:
        data = await _read_pem(certificate, "Certificate")
        try:
            x509.load_pem_x509_certificate(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Certificate is not a valid certificate: {exc}") from exc
        values["TAK_CERT"] = _write_pem("cert.pem", data, 0o644)
    if private_key is not None:
        data = await _read_pem(private_key, "Private key")
        try:
            serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Private key is not a valid unencrypted PEM key: {exc}") from exc
        values["TAK_KEY"] = _write_pem("key.pem", data, 0o600)

    result = _control("/v1/config", method="PUT", body={"values": values})
    await write_audit(db, actor.id, "upload_tak_package", ",".join(sorted(values)))
    return {"ok": True, "updated": sorted(values), "paths": values, "control_result": result}
