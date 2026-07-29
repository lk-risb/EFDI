import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
sys.path.insert(0, str(COMPOSE))

spec = importlib.util.spec_from_file_location(
    "asterix_bridge",
    COMPOSE / "bridges" / "asterix_bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bridge)


class _Sample:
    def __init__(self, key: str, payload: bytes) -> None:
        self.key_expr = key
        self.payload = payload


class _Session:
    def __init__(self) -> None:
        self.puts = []

    def put(self, key, payload, *, encoding) -> None:
        self.puts.append((key, payload, encoding))


def _frame(category: int, body: bytes = b"\x80") -> bytes:
    return bytes([category]) + (len(body) + 3).to_bytes(2, "big") + body


def test_category_from_key_accepts_all_numeric_asterix_categories():
    root = "partner/org"
    assert bridge.category_from_key(
        "partner/org/raw/asterix/cat0", root
    ) == 0
    assert bridge.category_from_key(
        "partner/org/raw/asterix/cat255", root
    ) == 255

    with pytest.raises(ValueError):
        bridge.category_from_key("partner/org/raw/asterix/cat256", root)
    with pytest.raises(ValueError):
        bridge.category_from_key("partner/org/raw/asterix/cat48/extra", root)
    with pytest.raises(ValueError):
        bridge.category_from_key("other/raw/asterix/cat48", root)


def test_validate_frame_rejects_mismatched_category_and_length():
    bridge.validate_frame(_frame(48), 48)

    with pytest.raises(ValueError, match="contains CAT-48"):
        bridge.validate_frame(_frame(48), 34)
    with pytest.raises(ValueError, match="does not match"):
        bridge.validate_frame(b"\x30\x00\x20payload", 48)


def test_relay_preserves_frame_and_maps_to_local_root():
    session = _Session()
    recent = bridge.RecentFrames(ttl_s=1.0)
    frame = _frame(48, b"\x01\x02")
    sample = _Sample("remote/raw/asterix/cat48", frame)

    destination = bridge.relay_sample(
        session,
        sample,
        upstream_root="remote",
        local_root="local",
        recent=recent,
    )

    assert destination == "local/raw/asterix/cat48"
    assert session.puts[0][0] == destination
    assert session.puts[0][1] == frame


def test_relay_suppresses_immediate_reflection():
    session = _Session()
    recent = bridge.RecentFrames(ttl_s=1.0)
    sample = _Sample("same/raw/asterix/cat34", _frame(34))

    assert bridge.relay_sample(
        session,
        sample,
        upstream_root="same",
        local_root="same",
        recent=recent,
    )
    assert bridge.relay_sample(
        session,
        sample,
        upstream_root="same",
        local_root="same",
        recent=recent,
    ) is None
    assert len(session.puts) == 1
