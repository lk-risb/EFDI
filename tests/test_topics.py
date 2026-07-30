import os
import pathlib
import sys

import pytest
from pydantic import ValidationError

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")
os.environ.setdefault("ZENOH_ADMIN_SECRET_KEY", "test-secret")
os.environ.setdefault("PARTNER_NAMESPACE", "router-a")

from api import topics  # noqa: E402


def test_topic_registration_accepts_patterns_and_mime_types():
    request = topics.TopicIn(
        key_expr="EFDI/router-a/air/**",
        encoding="application/protobuf",
        direction="subscribe",
        description="Air track contract",
    )

    assert request.key_expr == "EFDI/router-a/air/**"
    assert request.encoding == "application/protobuf"


@pytest.mark.parametrize("direction", ("publish", "bidirectional"))
def test_topic_registration_rejects_wildcard_publish_contracts(direction):
    with pytest.raises(ValidationError, match="wildcards are subscription-only"):
        topics.TopicIn(
            key_expr="EFDI/router-a/air/**",
            direction=direction,
        )


@pytest.mark.parametrize(
    "key_expr",
    ("", "/leading//gap", "space is invalid", "../escape"),
)
def test_topic_registration_rejects_invalid_key_expressions(key_expr):
    with pytest.raises(ValidationError):
        topics.TopicIn(key_expr=key_expr)


def test_catalog_topic_uses_runtime_prefix(monkeypatch, tmp_path):
    data_prefix = tmp_path / "data-prefix"
    data_prefix.write_text("ORG/UNIT\n")
    monkeypatch.setattr(topics, "_DATA_PREFIX_FILE", str(data_prefix))
    monkeypatch.setenv("PARTNER_NAMESPACE", "router-a")

    assert topics._catalog_topic() == "ORG/UNIT/router-a/@catalog/topics/v1"


def test_catalog_topic_supports_slot_root_namespace(monkeypatch, tmp_path):
    data_prefix = tmp_path / "data-prefix"
    data_prefix.write_text("")
    monkeypatch.setattr(topics, "_DATA_PREFIX_FILE", str(data_prefix))
    monkeypatch.setenv("PARTNER_NAMESPACE", "router-slot")

    assert topics._catalog_topic() == "router-slot/@catalog/topics/v1"


def test_topic_observer_retains_metadata_only():
    class Sample:
        key_expr = "EFDI/router-a/raw/asterix/cat48"
        encoding = "application/octet-stream"
        payload = b"must-not-be-retained"

    with topics._OBSERVED_LOCK:
        topics._OBSERVED.clear()
    topics._observe(Sample())
    topics._observe(Sample())

    observed = topics._observed_topics()
    assert observed[0]["key_expr"] == Sample.key_expr
    assert observed[0]["encoding"] == Sample.encoding
    assert observed[0]["sample_count"] == 2
    assert "payload" not in observed[0]
