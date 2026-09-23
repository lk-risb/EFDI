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
from unittest import mock

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


def _ec_ca_and_ed25519_leaf(tmp: str) -> tuple[bytes, bytes, bytes]:
    """An EC CA signing an Ed25519 leaf — the real shape of the EFDI Backbone
    trial fabric's own certs (confirmed live: its client keys are Ed25519,
    its root CA is EC). _load_identity()'s original public-key comparison
    only handled RSA/EC (.public_numbers()), so this exact combination
    crashed with a bare AttributeError instead of a clean 400/200."""
    ca_key = os.path.join(tmp, "ec-ca-key.pem")
    ca_cert = os.path.join(tmp, "ec-ca-cert.pem")
    leaf_key = os.path.join(tmp, "ed25519-leaf-key.pem")
    leaf_csr = os.path.join(tmp, "ed25519-leaf.csr")
    leaf_cert = os.path.join(tmp, "ed25519-leaf-cert.pem")

    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", ca_key, "-out", ca_cert, "-days", "1",
         "-subj", "/O=Test/CN=Test EC Root CA"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["openssl", "req", "-newkey", "ed25519", "-nodes",
         "-keyout", leaf_key, "-out", leaf_csr, "-subj", "/CN=test-ed25519-leaf"],
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


class LoadIdentityKeyTypeTests(unittest.TestCase):
    def test_ec_ca_with_ed25519_leaf_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, key_pem = _ec_ca_and_ed25519_leaf(tmp)
            # Raises nothing — this used to be an unhandled AttributeError.
            certs_bootstrap._load_identity(ca_pem, cert_pem, key_pem)

    def test_ed25519_leaf_key_mismatch_still_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, _ = _ec_ca_and_ed25519_leaf(tmp)
            _, _, other_key_pem = _ec_ca_and_ed25519_leaf(tmp)
            with self.assertRaises(HTTPException) as ctx:
                certs_bootstrap._load_identity(ca_pem, cert_pem, other_key_pem)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("do not match", ctx.exception.detail)


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


class EnvUpdateOrderingTests(unittest.TestCase):
    def test_env_update_happens_before_native_process_restart(self):
        """Confirmed live on zenoh-gateway: when _control_env_update() ran
        *after* apply_rendered_config(restart_native=True), native bridge/layer
        scripts restarted reading the still-old PARTNER_NAMESPACE/
        NAMESPACE_PREFIX from .env — they came back up "RUNNING" but silently
        publishing/subscribing under the previous identity, so nothing
        downstream ever saw data under the new one. env_update must run
        first."""
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, key_pem = _ca_and_leaf(tmp)
            certs_bootstrap._ROUTER_TLS_DIR = os.path.join(tmp, "router-tls")
            certs_bootstrap._OWN_CERT_DIR = os.path.join(tmp, "own-certs")

            calls: list[str] = []

            def record_env_update(_values):
                calls.append("env_update")
                return {}

            def record_apply(_rendered, _fields, **_kwargs):
                calls.append("apply_rendered_config")
                return {"status": "applied", "native_process_restart_required": True,
                         "native_process_restart_failures": []}

            actor = mock.Mock(id="actor-1")
            db = mock.Mock()

            with mock.patch.object(certs_bootstrap, "_control_env_update", side_effect=record_env_update), \
                 mock.patch.object(certs_bootstrap, "_render_config", return_value="dummy-rendered-config"), \
                 mock.patch.object(certs_bootstrap, "apply_rendered_config", side_effect=record_apply), \
                 mock.patch.object(certs_bootstrap, "_control_recreate_self", return_value={}), \
                 mock.patch.object(certs_bootstrap, "write_audit", new=mock.AsyncMock()):
                asyncio.run(certs_bootstrap.upload_bootstrap_identity(
                    ca_root=UploadFile(filename="ca.pem", file=io.BytesIO(ca_pem)),
                    certificate=UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem)),
                    private_key=UploadFile(filename="key.pem", file=io.BytesIO(key_pem)),
                    partner_namespace="zenoh_gateway",
                    namespace_prefix="LTU/CISB",
                    db=db,
                    actor=actor,
                ))

            self.assertEqual(calls, ["env_update", "apply_rendered_config"])


if __name__ == "__main__":
    unittest.main()
