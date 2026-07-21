import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))

from api.federation_crypto import FederationVerifyError, sign_payload, verify_envelope  # noqa: E402


def policy_identity():
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "efdi-policy-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    )


def test_signature_purpose_is_domain_separated():
    key, cert = policy_identity()
    payload = {"version": 1, "health": "ok"}
    envelope = {"payload": payload, "signature": sign_payload(payload, key, purpose="status")}
    assert verify_envelope(envelope, cert, purpose="status") == payload
    with pytest.raises(FederationVerifyError):
        verify_envelope(envelope, cert, purpose="config")
