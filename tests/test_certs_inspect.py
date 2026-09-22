#!/usr/bin/env python3
"""Focused unit tests for certs_bootstrap.py's /api/certs/inspect endpoint —
the WebUI's best-effort PARTNER_NAMESPACE prefill from an uploaded
certificate's own CN (certificates.tsx's inspectCertFile())."""

import asyncio
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")
os.environ.setdefault("ZENOH_ADMIN_SECRET_KEY", "test-secret")
os.environ.setdefault("PARTNER_NAMESPACE", "hq")

from fastapi import UploadFile  # noqa: E402

from api import certs_bootstrap  # noqa: E402


def _self_signed_cert(cn: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        cert_path = os.path.join(tmp, "cert.pem")
        key_path = os.path.join(tmp, "key.pem")
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", key_path, "-out", cert_path, "-days", "1",
                "-subj", f"/O=EFDI/CN={cn}",
            ],
            check=True, capture_output=True,
        )
        with open(cert_path, "rb") as f:
            return f.read()


class InspectCertificateTests(unittest.TestCase):
    def test_extracts_common_name_from_a_real_certificate(self):
        cert_pem = _self_signed_cert("zenoh-gateway-test")
        upload = UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem))
        result = asyncio.run(certs_bootstrap.inspect_certificate(certificate=upload, _=None))
        self.assertEqual(result, {"common_name": "zenoh-gateway-test"})

    def test_garbage_pem_returns_none_rather_than_crashing(self):
        upload = UploadFile(filename="cert.pem", file=io.BytesIO(
            b"-----BEGIN CERTIFICATE-----\nbm90IGEgcmVhbCBjZXJ0\n-----END CERTIFICATE-----\n"
        ))
        result = asyncio.run(certs_bootstrap.inspect_certificate(certificate=upload, _=None))
        self.assertEqual(result, {"common_name": None})

    def test_valid_cert_with_no_common_name_returns_none(self):
        cert_pem = _self_signed_cert("")
        # A CN of "" still sets the RDN in most openssl builds; skip if this
        # particular build refuses to emit one, rather than asserting on
        # openssl-version-specific behavior this test doesn't own.
        upload = UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem))
        result = asyncio.run(certs_bootstrap.inspect_certificate(certificate=upload, _=None))
        self.assertIn(result["common_name"], ("", None))


if __name__ == "__main__":
    unittest.main()
