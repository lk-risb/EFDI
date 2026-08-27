"""TAK Server mTLS client credential upload — the "Option B" bundle referenced
by compose/layers/tak_layer.py's own header comment (cert/key/CA PEM files,
generated with `make add-service NAME=efdi-pod` in the TAK repo, which writes
them to certs/<name>/{ca,cert,key}.pem on the operator's machine). Accepts
those three files individually, or all at once as a zip of that directory —
not a TAK ATAK data package, just a convenience bundle of the same three PEMs.

tak_layer.py runs as a host-native process (started by start.sh), not inside
this container, so TAK_CERT/TAK_KEY/TAK_CA must end up as real host
filesystem paths. This mirrors certs_bootstrap.py's write pattern but reads
the host-equivalent path from EFDI_TAK_PACKAGE_HOST_DIR (see docker-compose.yml)
instead of using the in-container path directly, then reuses control.py's
already-authenticated link to the host control agent to persist those paths
into .env — the same seam update_runtime_config already uses.
"""

import io
import os
import zipfile

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
_MAX_ZIP_BYTES = 512 * 1024  # a handful of small PEM files, generous headroom


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


def _classify_zip_member(name: str, content: bytes) -> str | None:
    """Guess whether one zip entry is the CA root, leaf certificate, or private
    key — same three-file convention TAK's own generate_service_cert.sh writes
    (ca.pem/cert.pem/key.pem), just bundled into one archive by the operator."""
    if b"-----BEGIN" not in content:
        return None
    lower = name.lower()
    if "key" in lower:
        try:
            serialization.load_pem_private_key(content, password=None)
            return "key"
        except (ValueError, TypeError):
            return None
    try:
        x509.load_pem_x509_certificate(content)
    except ValueError:
        return None
    if "ca" in lower:
        return "ca"
    return "cert"


async def _extract_service_package(upload: UploadFile) -> dict[str, bytes]:
    """Pull ca/cert/key PEM bytes out of a zipped TAK service-cert bundle —
    the whole `certs/<name>/` directory generate_service_cert.sh writes,
    zipped up by the operator instead of picked apart file by file."""
    data = await upload.read(_MAX_ZIP_BYTES + 1)
    if len(data) > _MAX_ZIP_BYTES:
        raise HTTPException(status_code=413, detail=f"Service package too large (max {_MAX_ZIP_BYTES} bytes)")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"Service package is not a valid zip file: {exc}") from exc

    found: dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir() or info.file_size > _MAX_PEM_BYTES:
            continue
        name = os.path.basename(info.filename)
        if not name:
            continue
        try:
            content = archive.read(info)
        except (zipfile.BadZipFile, RuntimeError):
            continue
        kind = _classify_zip_member(name, content)
        if kind and kind not in found:
            found[kind] = content

    if not found:
        raise HTTPException(
            status_code=400,
            detail="No CA root, certificate, or private key found inside that zip "
            "(looked for filenames containing 'ca', 'cert', or 'key' with valid PEM content)",
        )
    return found


@router.post("")
async def upload_tak_package(
    ca_root: UploadFile | None = File(None),
    certificate: UploadFile | None = File(None),
    private_key: UploadFile | None = File(None),
    service_package: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    if not (ca_root or certificate or private_key or service_package):
        raise HTTPException(
            status_code=400,
            detail="upload at least one of CA root, certificate, private key, or a zipped service package",
        )

    from_zip: dict[str, bytes] = {}
    if service_package is not None:
        from_zip = await _extract_service_package(service_package)

    values: dict[str, str] = {}
    # Explicit single-file uploads always win over whatever the zip contained
    # for the same slot — more intentional, and lets an operator override just
    # one file from a package that's otherwise correct.
    if ca_root is not None:
        data = await _read_pem(ca_root, "CA root")
        try:
            x509.load_pem_x509_certificate(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"CA root is not a valid certificate: {exc}") from exc
        values["TAK_CA"] = _write_pem("ca.pem", data, 0o644)
    elif "ca" in from_zip:
        values["TAK_CA"] = _write_pem("ca.pem", from_zip["ca"], 0o644)

    if certificate is not None:
        data = await _read_pem(certificate, "Certificate")
        try:
            x509.load_pem_x509_certificate(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Certificate is not a valid certificate: {exc}") from exc
        values["TAK_CERT"] = _write_pem("cert.pem", data, 0o644)
    elif "cert" in from_zip:
        values["TAK_CERT"] = _write_pem("cert.pem", from_zip["cert"], 0o644)

    if private_key is not None:
        data = await _read_pem(private_key, "Private key")
        try:
            serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Private key is not a valid unencrypted PEM key: {exc}") from exc
        values["TAK_KEY"] = _write_pem("key.pem", data, 0o600)
    elif "key" in from_zip:
        values["TAK_KEY"] = _write_pem("key.pem", from_zip["key"], 0o600)

    result = _control("/v1/config", method="PUT", body={"values": values})
    await write_audit(db, actor.id, "upload_tak_package", ",".join(sorted(values)))
    return {"ok": True, "updated": sorted(values), "paths": values, "control_result": result}
