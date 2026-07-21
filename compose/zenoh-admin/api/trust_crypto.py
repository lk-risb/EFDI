"""Domain-separated signing and strict parsing for delegation grants."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtensionOID
from pydantic import ValidationError

from .trust_types import DelegationEnvelope, DelegationGrant


_DOMAIN = b"EFDI-DELEGATION-V1\0"


class TrustVerifyError(ValueError):
    pass


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TrustVerifyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                TrustVerifyError(f"non-finite JSON number: {value}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrustVerifyError(f"invalid JSON: {exc}") from exc


def canonical_grant(grant: DelegationGrant) -> bytes:
    payload = grant.model_dump(mode="json", by_alias=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def certificate_sha256(certificate: x509.Certificate) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex()


def certificate_router_identity(certificate: x509.Certificate) -> str:
    try:
        san = certificate.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value
    except x509.ExtensionNotFound as exc:
        raise TrustVerifyError("signer certificate has no URI SAN") from exc
    identities = san.get_values_for_type(x509.UniformResourceIdentifier)
    if len(identities) != 1 or not identities[0].startswith("spiffe://"):
        raise TrustVerifyError("signer certificate must contain exactly one SPIFFE URI SAN")
    return identities[0]


def sign_grant(grant: DelegationGrant, key_pem: bytes, certificate_pem: bytes) -> dict:
    key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise TrustVerifyError("policy signer must use ECDSA P-256")
    certificate = x509.load_pem_x509_certificate(certificate_pem)
    if certificate.public_key().public_numbers() != key.public_key().public_numbers():
        raise TrustVerifyError("policy signer certificate and private key do not match")
    if certificate_router_identity(certificate) != grant.issuer_identity:
        raise TrustVerifyError("policy signer identity does not match grant issuer")
    signature = key.sign(_DOMAIN + canonical_grant(grant), ec.ECDSA(hashes.SHA256()))
    return {
        "schema": "efdi.delegation-envelope/v1",
        "payload": grant.model_dump(mode="json", by_alias=True),
        "signature": base64.b64encode(signature).decode(),
        "signer_certificate": certificate_pem.decode(),
        "signer_sha256": certificate_sha256(certificate),
    }


def verify_grant_envelope(
    raw: str | bytes | dict,
    *,
    expected_signer_sha256: str | None = None,
    now: datetime | None = None,
) -> tuple[DelegationGrant, x509.Certificate]:
    document = strict_json_loads(raw) if isinstance(raw, (str, bytes)) else raw
    try:
        envelope = DelegationEnvelope.model_validate(document)
        grant = DelegationGrant.model_validate(envelope.payload)
        certificate = x509.load_pem_x509_certificate(envelope.signer_certificate.encode())
    except (ValidationError, ValueError) as exc:
        raise TrustVerifyError(f"invalid delegation envelope: {exc}") from exc
    fingerprint = certificate_sha256(certificate)
    if fingerprint != envelope.signer_sha256:
        raise TrustVerifyError("signer certificate fingerprint does not match envelope")
    if expected_signer_sha256 and fingerprint != expected_signer_sha256.lower():
        raise TrustVerifyError("delegation was not signed by the expected authority")
    if certificate_router_identity(certificate) != grant.issuer_identity:
        raise TrustVerifyError("signer identity does not match grant issuer")
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(public_key.curve, ec.SECP256R1):
        raise TrustVerifyError("policy signer certificate must use ECDSA P-256")
    try:
        signature = base64.b64decode(envelope.signature, validate=True)
        public_key.verify(signature, _DOMAIN + canonical_grant(grant), ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature) as exc:
        raise TrustVerifyError("delegation signature verification failed") from exc
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current < grant.not_before or current >= grant.not_after:
        raise TrustVerifyError("delegation is not currently valid")
    cert_before = certificate.not_valid_before_utc
    cert_after = certificate.not_valid_after_utc
    if current < cert_before or current >= cert_after:
        raise TrustVerifyError("policy signer certificate is not currently valid")
    return grant, certificate
