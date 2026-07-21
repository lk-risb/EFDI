"""Canonical grant, scope, and signature security tests."""

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))

from api.trust_crypto import TrustVerifyError, sign_grant, strict_json_loads, verify_grant_envelope  # noqa: E402
from api.models import TrustAuthority  # noqa: E402
from api.trust_store import TrustStoreError, verify_trust_chain  # noqa: E402
from api.trust_types import DelegationGrant, grant_is_subset, scope_contains  # noqa: E402


NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def signer(identity: str):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "policy-signer")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(identity)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    )


def grant(**updates) -> DelegationGrant:
    values = {
        "id": uuid4(),
        "issuer_identity": "spiffe://efdi.global/router/LTU",
        "subject_ca_sha256": "a" * 64,
        "subject_identity": "spiffe://efdi.global/router/LTU/CISB",
        "namespace": "LTU/CISB/**",
        "roles": ["router-ca", "router", "workload-issuer"],
        "publish": ["LTU/CISB/**"],
        "subscribe": ["LTU/**"],
        "control": ["topology", "status", "config-from-parent", "manage-children"],
        "max_delegation_depth": 3,
        "sequence": 1,
        "not_before": NOW - timedelta(hours=1),
        "not_after": NOW + timedelta(days=7),
    }
    values.update(updates)
    return DelegationGrant.model_validate(values)


def test_scope_containment_rejects_sibling_and_wildcard_widening():
    assert scope_contains("LTU/CISB/**", "LTU/CISB/child/**")
    assert not scope_contains("LTU/CISB/**", "LTU/CYBER/**")
    assert not scope_contains("LTU/CISB/child", "LTU/CISB/child/**")
    with pytest.raises(ValueError):
        scope_contains("LTU/*/child", "LTU/CISB/child")


def test_child_grant_must_be_strictly_narrower():
    parent = grant()
    child = grant(
        issuer_identity=parent.subject_identity,
        subject_identity=parent.subject_identity + "/branch",
        namespace="LTU/CISB/branch/**",
        publish=["LTU/CISB/branch/**"],
        subscribe=["LTU/CISB/**"],
        max_delegation_depth=2,
    )
    widened = child.model_copy(update={"publish": ["LTU/**"]})
    exhausted = child.model_copy(update={"max_delegation_depth": 3})
    assert grant_is_subset(child, parent)
    assert not grant_is_subset(widened, parent)
    assert not grant_is_subset(exhausted, parent)


def test_domain_separated_grant_signature_and_tamper_rejection():
    value = grant()
    key, cert = signer(value.issuer_identity)
    envelope = sign_grant(value, key, cert)
    verified, _ = verify_grant_envelope(envelope, now=NOW)
    assert verified == value
    envelope["payload"]["sequence"] = 2
    with pytest.raises(TrustVerifyError, match="signature"):
        verify_grant_envelope(envelope, now=NOW)


def test_strict_json_rejects_duplicate_keys_nan_and_unknown_fields():
    with pytest.raises(TrustVerifyError, match="duplicate"):
        strict_json_loads('{"schema":"a","schema":"b"}')
    with pytest.raises(TrustVerifyError, match="non-finite"):
        strict_json_loads('{"value":NaN}')

    value = grant()
    key, cert = signer(value.issuer_identity)
    envelope = sign_grant(value, key, cert)
    envelope["payload"]["unexpected"] = True
    with pytest.raises(TrustVerifyError, match="invalid delegation"):
        verify_grant_envelope(json.dumps(envelope), now=NOW)


def _ca(subject: str, path_length: int, issuer_key=None, issuer_cert=None):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    issuer_key = issuer_key or key
    issuer_name = issuer_cert.subject if issuer_cert else name
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(issuer_name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )
    return key, cert


def _policy(identity: str, issuer_key, issuer_cert):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "policy")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(issuer_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=20))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage([
            ObjectIdentifier("1.3.6.1.4.1.55555.1.1")
        ]), critical=False)
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(identity)
        ]), critical=False)
        .sign(issuer_key, hashes.SHA256())
    )
    return key, cert


def _pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _private_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def test_root_verifies_complete_grandchild_delegation_chain():
    root_identity = "spiffe://efdi.global/router/LTU"
    child_identity = root_identity + "/CISB"
    grandchild_identity = child_identity + "/branch"
    root_key, root_ca = _ca("root-ca", 3)
    root_policy_key, root_policy = _policy(root_identity, root_key, root_ca)
    child_key, child_ca = _ca("child-ca", 2, root_key, root_ca)
    child_policy_key, child_policy = _policy(child_identity, root_key, root_ca)
    _, grandchild_ca = _ca("grandchild-ca", 1, child_key, child_ca)
    _, grandchild_policy = _policy(grandchild_identity, child_key, child_ca)

    child_grant = grant(
        issuer_identity=root_identity,
        subject_identity=child_identity,
        subject_ca_sha256=child_ca.fingerprint(hashes.SHA256()).hex(),
        namespace="LTU/CISB/**",
        publish=["LTU/CISB/**"],
        subscribe=["LTU/**"],
        max_delegation_depth=2,
    )
    grandchild_grant = grant(
        issuer_identity=child_identity,
        subject_identity=grandchild_identity,
        subject_ca_sha256=grandchild_ca.fingerprint(hashes.SHA256()).hex(),
        namespace="LTU/CISB/branch/**",
        publish=["LTU/CISB/branch/**"],
        subscribe=["LTU/CISB/**"],
        max_delegation_depth=1,
        sequence=1,
    )
    root = TrustAuthority(
        identity_uri=root_identity,
        namespace_scope="LTU/**",
        ca_fingerprint=root_ca.fingerprint(hashes.SHA256()).hex(),
        ca_cert_pem=_pem(root_ca),
        policy_signer_fingerprint=root_policy.fingerprint(hashes.SHA256()).hex(),
        policy_signer_cert_pem=_pem(root_policy),
        parent_id=None,
        max_delegation_depth=3,
        state="active",
        not_after=NOW + timedelta(days=20),
    )
    proof = {
        "schema": "efdi.trust-chain/v1",
        "anchor": {
            "identity_uri": root.identity_uri,
            "namespace_scope": root.namespace_scope,
            "ca_sha256": root.ca_fingerprint,
            "ca_certificate": root.ca_cert_pem,
            "policy_signer_sha256": root.policy_signer_fingerprint,
            "policy_signer_certificate": root.policy_signer_cert_pem,
            "max_delegation_depth": root.max_delegation_depth,
            "not_after": root.not_after.isoformat(),
        },
        "steps": [
            {
                "delegation_envelope": sign_grant(
                    child_grant, _private_pem(root_policy_key), _pem(root_policy).encode()
                ),
                "subject_ca_certificate": _pem(child_ca),
                "subject_policy_signer_certificate": _pem(child_policy),
            },
            {
                "delegation_envelope": sign_grant(
                    grandchild_grant, _private_pem(child_policy_key), _pem(child_policy).encode()
                ),
                "subject_ca_certificate": _pem(grandchild_ca),
                "subject_policy_signer_certificate": _pem(grandchild_policy),
            },
        ],
    }

    verified = verify_trust_chain(proof, root, now=NOW)
    assert verified.identity_uri == grandchild_identity
    assert verified.namespace_scope == "LTU/CISB/branch/**"

    proof["steps"][1]["delegation_envelope"]["payload"]["publish"] = ["LTU/**"]
    with pytest.raises((TrustStoreError, TrustVerifyError)):
        verify_trust_chain(proof, root, now=NOW)
