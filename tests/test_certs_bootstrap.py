#!/usr/bin/env python3
"""Focused unit tests for certs_bootstrap.py's /api/certs/bootstrap upload —
specifically that a misconfigured EFDI_CERT_DIR (e.g. set to a host path that
doesn't exist inside this container, as happened when it mirrored BUNDLE_DIR
by mistake) surfaces as a clear 500 detail instead of a bare, content-free
crash."""

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

from fastapi import HTTPException, UploadFile  # noqa: E402

from api import certs_bootstrap  # noqa: E402


def _ca_and_leaf(tmp: str) -> tuple[bytes, bytes, bytes]:
    """A real CA + a leaf it actually signed + the leaf's matching key —
    enough to pass _load_identity() and reach the disk-write section."""
    ca_key = os.path.join(tmp, "ca-key.pem")
    ca_cert = os.path.join(tmp, "ca-cert.pem")
    leaf_key = os.path.join(tmp, "leaf-key.pem")
    leaf_csr = os.path.join(tmp, "leaf.csr")
    leaf_cert = os.path.join(tmp, "leaf-cert.pem")

    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", ca_key, "-out", ca_cert, "-days", "1",
         "-subj", "/O=Test/CN=Test Root CA"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-keyout", leaf_key, "-out", leaf_csr,
         "-subj", "/CN=test-leaf"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", leaf_csr, "-CA", ca_cert, "-CAkey", ca_key,
         "-CAcreateserial", "-out", leaf_cert, "-days", "1"],
        check=True, capture_output=True,
    )
    return (
        pathlib.Path(ca_cert).read_bytes(),
        pathlib.Path(leaf_cert).read_bytes(),
        pathlib.Path(leaf_key).read_bytes(),
    )


class UploadBootstrapIdentityTests(unittest.TestCase):
    def test_unwritable_efdi_cert_dir_returns_clear_500_not_a_bare_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, key_pem = _ca_and_leaf(tmp)

            router_tls_dir = os.path.join(tmp, "router-tls")
            # A plain file where a directory is expected — mirrors the real
            # bug: EFDI_CERT_DIR pointed at a path (a host path, mirroring
            # BUNDLE_DIR) that doesn't resolve to a real directory inside
            # this container, so os.makedirs fails with an OSError subtype.
            blocker_file = os.path.join(tmp, "not-a-directory")
            pathlib.Path(blocker_file).write_text("x")
            own_cert_dir = os.path.join(blocker_file, "efdi")

            orig_router_tls_dir = certs_bootstrap._ROUTER_TLS_DIR
            orig_own_cert_dir = certs_bootstrap._OWN_CERT_DIR
            certs_bootstrap._ROUTER_TLS_DIR = router_tls_dir
            certs_bootstrap._OWN_CERT_DIR = own_cert_dir
            try:
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(certs_bootstrap.upload_bootstrap_identity(
                        ca_root=UploadFile(filename="ca.pem", file=io.BytesIO(ca_pem)),
                        certificate=UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem)),
                        private_key=UploadFile(filename="key.pem", file=io.BytesIO(key_pem)),
                        partner_namespace="hq",
                        namespace_prefix="EFDI",
                        db=None,
                        actor=None,
                    ))
                self.assertEqual(ctx.exception.status_code, 500)
                self.assertIn("EFDI_CERT_DIR", ctx.exception.detail)
                self.assertIn("/certs/efdi", ctx.exception.detail)
            finally:
                certs_bootstrap._ROUTER_TLS_DIR = orig_router_tls_dir
                certs_bootstrap._OWN_CERT_DIR = orig_own_cert_dir


if __name__ == "__main__":
    unittest.main()
