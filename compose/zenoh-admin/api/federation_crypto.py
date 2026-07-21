import base64
import json
from datetime import datetime, timezone

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class FederationVerifyError(Exception):
    pass


def canonicalize(payload: dict, purpose: str = "config") -> bytes:
    """Deterministic encoding so signer and verifier hash the exact same bytes."""
    if not isinstance(payload, dict):
        raise FederationVerifyError("payload must be an object")
    if purpose not in {"config", "status", "topology", "relay"}:
        raise FederationVerifyError("unknown signature purpose")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"EFDI-{purpose.upper()}-V1\0".encode() + body


def sign_payload(payload: dict, key_pem: bytes, purpose: str = "config") -> str:
    """Sign `payload` with an EC private key (PEM, no password — matches the
    format scripts/gen-certs.sh already issues). Returns a base64-encoded DER
    signature."""
    private_key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(private_key.curve, ec.SECP256R1):
        raise FederationVerifyError("policy signer must use ECDSA P-256")
    signature = private_key.sign(canonicalize(payload, purpose), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


def verify_envelope(
    envelope: dict,
    trusted_cert_pem: bytes,
    expected_cn: str | None = None,
    purpose: str = "config",
) -> dict:
    """Verify `envelope = {"payload": {...}, "signature": "<base64 DER>"}`
    against `trusted_cert_pem`. Raises FederationVerifyError on any failure —
    wrong signer, tampered payload, or a cert whose CN doesn't match
    `expected_cn`. Returns the verified payload dict on success."""
    try:
        payload = envelope["payload"]
        signature = base64.b64decode(envelope["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FederationVerifyError(f"malformed envelope: {exc}")

    try:
        cert = x509.load_pem_x509_certificate(trusted_cert_pem)
    except (TypeError, ValueError) as exc:
        raise FederationVerifyError(f"invalid trusted parent cert: {exc}")

    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    actual_cn = cn_attrs[0].value if cn_attrs else None
    # `not actual_cn` (not just `!=`) so a certless cert can never match a
    # falsy/unset expected_cn — without this, a misconfigured caller passing
    # expected_cn=None would silently accept any cert with no CN at all.
    if expected_cn is not None and (not actual_cn or actual_cn != expected_cn):
        raise FederationVerifyError(
            f"trusted parent cert CN mismatch: expected {expected_cn!r}, cert has {actual_cn!r}"
        )

    public_key = cert.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(public_key.curve, ec.SECP256R1):
        raise FederationVerifyError("trusted policy signer does not contain an ECDSA P-256 key")
    now = datetime.now(timezone.utc)
    if now < cert.not_valid_before_utc or now >= cert.not_valid_after_utc:
        raise FederationVerifyError("trusted policy signer certificate is not currently valid")
    try:
        public_key.verify(signature, canonicalize(payload, purpose), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise FederationVerifyError("signature verification failed")

    return payload
