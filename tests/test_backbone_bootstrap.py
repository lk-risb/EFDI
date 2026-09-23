#!/usr/bin/env python3
"""Focused unit tests for backbone_bootstrap.py's /api/certs/backbone/bootstrap
upload — the WebUI path for giving the dedicated zenoh-router-backbone
container its EFDI Backbone trial-fabric identity, separate from the pod's
own EFDI-CA identity (certs_bootstrap.py).

The endpoint is taken as this form's own field (backbone_endpoint), not read
from EFDI_BACKBONE_FABRIC_ENDPOINTS — that env var is only baked into this
container at create time, so a value just changed via Integration Settings
wouldn't be visible here until an unrelated container recreate happened to
occur (confirmed live: this was reported as "nothing works" after a fresh
Integration Settings edit kept 409'ing)."""

import asyncio
import io
import os
import pathlib
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

from api import backbone_bootstrap  # noqa: E402
from tests.test_certs_bootstrap import _ca_and_leaf  # noqa: E402


class BackboneBootstrapTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._env_update_patch = mock.patch.object(
            backbone_bootstrap, "_control_env_update", return_value={}
        )
        self._env_update_patch.start()

    def tearDown(self):
        self._env_update_patch.stop()
        os.environ.clear()
        os.environ.update(self._env)

    def test_missing_endpoint_field_returns_400_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, key_pem = _ca_and_leaf(tmp)
            backbone_bootstrap._BACKBONE_TLS_DIR = os.path.join(tmp, "backbone-tls")

            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(backbone_bootstrap.upload_backbone_identity(
                    ca_root=UploadFile(filename="ca.pem", file=io.BytesIO(ca_pem)),
                    certificate=UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem)),
                    private_key=UploadFile(filename="key.pem", file=io.BytesIO(key_pem)),
                    backbone_endpoint="   ",
                    db=None,
                    actor=None,
                ))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("backbone_endpoint", ctx.exception.detail)
            self.assertFalse(os.path.exists(backbone_bootstrap._BACKBONE_TLS_DIR))

    def test_valid_upload_writes_identity_renders_config_and_starts_router(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, key_pem = _ca_and_leaf(tmp)
            backbone_bootstrap._BACKBONE_TLS_DIR = os.path.join(tmp, "backbone-tls")
            backbone_bootstrap._BACKBONE_CONFIG_PATH = os.path.join(tmp, "backbone-config", "config.json5")
            backbone_bootstrap._ROUTER_TLS_DIR = os.path.join(tmp, "primary-router-tls")

            template_dir = os.path.join(tmp, "tmpl")
            os.makedirs(template_dir)
            template_path = os.path.join(template_dir, "zenoh-router-backbone.json5.tmpl")
            with open(template_path, "w") as f:
                f.write('{ connect: { endpoints: ${ZENOH_BACKBONE_CONNECT_ENDPOINTS} } }')
            backbone_bootstrap._BACKBONE_TEMPLATE_PATH = template_path

            os.environ["ZENOH_LOCAL_TCP_PORT"] = "7448"

            actor = mock.Mock(id="actor-1")
            db = mock.Mock()

            with mock.patch.object(
                backbone_bootstrap, "_control_start_backbone_router",
                return_value={"ok": True, "output": "started"},
            ) as start_mock, \
                mock.patch.object(backbone_bootstrap, "write_audit", new=mock.AsyncMock()):
                result = asyncio.run(backbone_bootstrap.upload_backbone_identity(
                    ca_root=UploadFile(filename="ca.pem", file=io.BytesIO(ca_pem)),
                    certificate=UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem)),
                    private_key=UploadFile(filename="key.pem", file=io.BytesIO(key_pem)),
                    backbone_endpoint="tls/zenoh.efdi.netbird.efdi-backbone.net:7447",
                    db=db,
                    actor=actor,
                ))

            self.assertEqual(result["status"], "applied")
            start_mock.assert_called_once()
            for name in ("ca-roots.pem", "cert.pem", "key.pem"):
                self.assertTrue(os.path.isfile(os.path.join(backbone_bootstrap._BACKBONE_TLS_DIR, name)))
            # Also staged into the primary router's own "backbone" TLS
            # profile slot, so the Zenoh Config page's "Backbone" fabric
            # preset pill works if an operator deliberately selects it —
            # separate from, and in addition to, the dedicated router above.
            primary_backbone_dir = os.path.join(backbone_bootstrap._ROUTER_TLS_DIR, "backbone")
            for name in ("ca-roots.pem", "cert.pem", "key.pem"):
                self.assertTrue(os.path.isfile(os.path.join(primary_backbone_dir, name)))
            with open(backbone_bootstrap._BACKBONE_CONFIG_PATH) as f:
                rendered = f.read()
            self.assertIn("tcp/127.0.0.1:7448", rendered)
            self.assertIn("tls/zenoh.efdi.netbird.efdi-backbone.net:7447", rendered)

    def test_env_update_failure_does_not_block_the_upload(self):
        """The env var is written for Integration Settings display only —
        this endpoint's own correctness never depended on reading it back
        (see module docstring), so a control-agent hiccup here must not
        fail an otherwise-successful upload."""
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, key_pem = _ca_and_leaf(tmp)
            backbone_bootstrap._BACKBONE_TLS_DIR = os.path.join(tmp, "backbone-tls")
            backbone_bootstrap._BACKBONE_CONFIG_PATH = os.path.join(tmp, "backbone-config", "config.json5")
            backbone_bootstrap._ROUTER_TLS_DIR = os.path.join(tmp, "primary-router-tls")
            template_path = os.path.join(tmp, "zenoh-router-backbone.json5.tmpl")
            with open(template_path, "w") as f:
                f.write('{ connect: { endpoints: ${ZENOH_BACKBONE_CONNECT_ENDPOINTS} } }')
            backbone_bootstrap._BACKBONE_TEMPLATE_PATH = template_path

            actor = mock.Mock(id="actor-1")
            db = mock.Mock()

            with mock.patch.object(
                backbone_bootstrap, "_control_env_update", side_effect=RuntimeError("agent unreachable"),
            ), mock.patch.object(
                backbone_bootstrap, "_control_start_backbone_router",
                return_value={"ok": True, "output": "started"},
            ), mock.patch.object(backbone_bootstrap, "write_audit", new=mock.AsyncMock()):
                result = asyncio.run(backbone_bootstrap.upload_backbone_identity(
                    ca_root=UploadFile(filename="ca.pem", file=io.BytesIO(ca_pem)),
                    certificate=UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem)),
                    private_key=UploadFile(filename="key.pem", file=io.BytesIO(key_pem)),
                    backbone_endpoint="tls/zenoh.efdi.netbird.efdi-backbone.net:7447",
                    db=db,
                    actor=actor,
                ))
            self.assertEqual(result["status"], "applied")

    def test_router_start_failure_returns_409_and_does_not_swallow_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca_pem, cert_pem, key_pem = _ca_and_leaf(tmp)
            backbone_bootstrap._BACKBONE_TLS_DIR = os.path.join(tmp, "backbone-tls")
            backbone_bootstrap._BACKBONE_CONFIG_PATH = os.path.join(tmp, "backbone-config", "config.json5")
            backbone_bootstrap._ROUTER_TLS_DIR = os.path.join(tmp, "primary-router-tls")
            template_path = os.path.join(tmp, "zenoh-router-backbone.json5.tmpl")
            with open(template_path, "w") as f:
                f.write('{ connect: { endpoints: ${ZENOH_BACKBONE_CONNECT_ENDPOINTS} } }')
            backbone_bootstrap._BACKBONE_TEMPLATE_PATH = template_path

            actor = mock.Mock(id="actor-1")
            db = mock.Mock()

            with mock.patch.object(
                backbone_bootstrap, "_control_start_backbone_router",
                return_value={"ok": False, "output": "docker compose failed"},
            ), mock.patch.object(backbone_bootstrap, "write_audit", new=mock.AsyncMock()):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(backbone_bootstrap.upload_backbone_identity(
                        ca_root=UploadFile(filename="ca.pem", file=io.BytesIO(ca_pem)),
                        certificate=UploadFile(filename="cert.pem", file=io.BytesIO(cert_pem)),
                        private_key=UploadFile(filename="key.pem", file=io.BytesIO(key_pem)),
                        backbone_endpoint="tls/zenoh.efdi.netbird.efdi-backbone.net:7447",
                        db=db,
                        actor=actor,
                    ))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("docker compose failed", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
