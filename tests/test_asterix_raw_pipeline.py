"""Mixed ASTERIX UDP ingress and per-category raw Zenoh validation."""

import importlib
import struct
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))
sys.path.insert(0, str(ROOT / "compose" / "protocols"))
sys.path.insert(0, str(ROOT / "tools"))

import asterix_probe  # noqa: E402
import asterix_udp_bridge as bridge  # noqa: E402


def frame(category: int, payload: bytes) -> bytes:
    return bytes([category]) + struct.pack(">H", len(payload) + 3) + payload


def test_mixed_datagram_is_split_into_complete_unchanged_frames():
    cat34 = frame(34, b"\x80\x01\x02")
    cat48 = frame(48, b"\x80\x03\x04")

    assert bridge.split_asterix_datagram(cat34 + cat48) == [
        (34, cat34),
        (48, cat48),
    ]


@pytest.mark.parametrize(
    "datagram",
    [b"", b"\x22\x00", b"\x22\x00\x02", b"\x22\x00\x06\x80"],
)
def test_malformed_datagram_is_rejected_as_a_whole(datagram):
    with pytest.raises(ValueError):
        bridge.split_asterix_datagram(datagram)


def test_category_and_source_configuration_validation():
    assert bridge.parse_categories("10, 34,48") == frozenset({10, 34, 48})
    with pytest.raises(ValueError):
        bridge.parse_categories("34,999")

    networks = bridge.parse_source_networks(["192.0.2.5,198.51.100.0/24"])
    assert bridge.source_allowed("192.0.2.5", networks)
    assert bridge.source_allowed("198.51.100.77", networks)
    assert not bridge.source_allowed("203.0.113.1", networks)


@pytest.mark.parametrize(
    ("module_name", "category"),
    [
        ("asterix_cat10", 10),
        ("asterix_cat20", 20),
        ("asterix_cat21", 21),
        ("asterix_cat34", 34),
        ("asterix_cat48", 48),
        ("asterix_cat62", 62),
    ],
)
def test_each_category_translator_accepts_only_its_exact_raw_frame(module_name, category):
    module = importlib.import_module(module_name)
    payload = b"\x80\x01\x02"
    valid = frame(category, payload)

    assert module._raw_frame_payload(valid, category) == payload
    with pytest.raises(ValueError):
        module._raw_frame_payload(frame((category + 1) % 256, payload), category)
    with pytest.raises(ValueError):
        module._raw_frame_payload(valid + b"\x00", category)


def test_probe_extracts_first_frn_sac_sic():
    # FSPEC FRN1 set, no extension; Ixxx/010 follows as SAC/SIC.
    sample = frame(48, b"\x80\x93\x17")
    assert asterix_probe.extract_sac_sic(sample) == (0x93, 0x17)


def test_probe_reports_unknown_when_first_frn_is_absent():
    assert asterix_probe.extract_sac_sic(frame(48, b"\x40\x00\x00")) is None
