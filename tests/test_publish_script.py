import os
import pathlib
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")
os.environ.setdefault("ZENOH_ADMIN_SECRET_KEY", "test-secret")

from api import publish_script  # noqa: E402


def test_publish_row_requires_concrete_topic():
    row = publish_script.PublishRow(
        topic="router-slot/health/publish-test/v1",
        message="hello",
    )
    assert row.topic == "router-slot/health/publish-test/v1"

    with pytest.raises(ValidationError, match="cannot contain wildcards"):
        publish_script.PublishRow(topic="router-slot/**", message="hello")


def test_ltu_fixed_filenames_do_not_require_client_cn():
    request = publish_script.PublishScriptRequest(
        router_endpoint="tls/zenoh1.example:7447",
        cert_dir="/opt/efdi-certs",
        tls_profile="ltu-local",
        rows=[
            publish_script.PublishRow(
                topic="router-slot/health/publish-test/v1",
                message="hello",
            )
        ],
    )
    assert request.client_cn == ""


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("router_endpoint", "tls/zenoh1.example:7447\ninjected"),
        ("cert_dir", "/opt/efdi-certs\ninjected"),
    ),
)
def test_publish_request_rejects_trailing_injection(field, value):
    kwargs = {
        "router_endpoint": "tls/zenoh1.example:7447",
        "cert_dir": "/opt/efdi-certs",
        "tls_profile": "ltu-local",
        "rows": [
            publish_script.PublishRow(
                topic="router-slot/health/publish-test/v1",
                message="hello",
            )
        ],
    }
    kwargs[field] = value

    with pytest.raises(ValidationError):
        publish_script.PublishScriptRequest(**kwargs)


def test_efdi_dynamic_filenames_require_client_cn():
    with pytest.raises(ValidationError, match="client_cn is required"):
        publish_script.PublishScriptRequest(
            router_endpoint="tls/zenoh1.example:7447",
            cert_dir="/opt/efdi-certs",
            tls_profile="efdi",
            rows=[
                publish_script.PublishRow(
                    topic="router-slot/health/publish-test/v1",
                    message="hello",
                )
            ],
        )


def test_ltu_publish_root_uses_environment_slot_without_legacy_prefix(monkeypatch):
    monkeypatch.setenv("PARTNER_NAMESPACE", "router-slot")
    fields = SimpleNamespace(
        fabric_tls_profile="ltu-local",
        partner_namespace="stale-slot",
        publish_prefix="legacy/vendor",
    )

    assert publish_script._publish_root(fields) == "router-slot"
