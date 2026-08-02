"""Generic UDP ingress ASTERIX dispatch and per-category validation."""

import struct
import sys
from pathlib import Path

import importlib
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))
sys.path.insert(0, str(ROOT / "compose" / "protocols"))
sys.path.insert(0, str(ROOT / "tools"))

import asterix_probe  # noqa: E402
import udp_ingress_bridge as bridge  # noqa: E402


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
        ("protocols.vendors.asterix.cat", 10),
        ("protocols.vendors.asterix.cat", 20),
        ("protocols.vendors.asterix.cat", 21),
        ("protocols.vendors.asterix.cat", 34),
        ("protocols.vendors.asterix.cat", 48),
        ("protocols.vendors.asterix.cat", 62),
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


def test_cat48_uses_matching_cat34_site_for_polar_geolocation():
    module = importlib.import_module("protocols.vendors.asterix.cat")
    sites = {}
    site_track = {
        "_src": "ASTERIX CAT-34 Ed.1.29",
        "sac": 112,
        "sic": 64,
        "lat_deg": 54.9701357,
        "lon_deg": 24.0824175,
    }

    assert module._cat48__cache_cat34_site(site_track, sites)
    target = {
        "sac": 112,
        "sic": 64,
        "range_nm": 1.0,
        "azimuth_deg": 0.0,
    }
    assert module._cat48__geolocate_from_site(target, [None, None], sites)
    assert target["lat_deg"] > site_track["lat_deg"]
    assert target["lon_deg"] == pytest.approx(site_track["lon_deg"], abs=1e-5)


def test_cat48_does_not_use_a_different_radar_site():
    module = importlib.import_module("protocols.vendors.asterix.cat")
    sites = {(1, 2): (54.0, 25.0)}
    target = {
        "sac": 112,
        "sic": 64,
        "range_nm": 2.0,
        "azimuth_deg": 90.0,
    }

    assert not module._cat48__geolocate_from_site(target, [None, None], sites)
    assert "lat_deg" not in target


def test_cat34_advertised_coverage_precedes_operator_fallback():
    module = importlib.import_module("protocols.vendors.asterix.cat")

    assert module._cat34__coverage_range_m(
        {"coverage_rho_end_nm": 25.0}, 200_000
    ) == (46_300, "advertised")
    assert module._cat34__coverage_range_m({}, 150_000) == (150_000, "configured")
    assert module._cat34__coverage_range_m({}, 0) == (None, None)


def test_cat34_keeps_live_site_position_per_sac_sic(monkeypatch):
    module = importlib.import_module("protocols.vendors.asterix.cat")
    messages = iter([
        {
            "msg_type": "north_marker",
            "sac": 112,
            "sic": 64,
            "site_lat": 54.9,
            "site_lon": 24.1,
        },
        {
            "msg_type": "north_marker",
            "sac": 112,
            "sic": 65,
            "site_lat": 55.1,
            "site_lon": 25.2,
        },
        {
            "msg_type": "north_marker",
            "sac": 112,
            "sic": 64,
        },
    ])
    published = []

    class NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(module, "_cat34_decode_cat034", lambda _data: next(messages))
    monkeypatch.setattr(module.threading, "Thread", NoThread)
    monkeypatch.setattr(
        module,
        "publish_dual",
        lambda _session, _topic, payload, *_args, **_kwargs:
            published.append(dict(payload)),
    )

    handler = module._cat34__make_cat034_handler(
        pub_sensor=object(),
        site=[None, None],
        radar_name="",
    )
    handler(b"first", False)
    handler(b"second", False)
    handler(b"first-again", False)

    assert [
        (item["sic"], item["sensor_name"], item["lat_deg"], item["lon_deg"])
        for item in published
    ] == [
        (64, "RADAR SAC112/SIC64", 54.9, 24.1),
        (65, "RADAR SAC112/SIC65", 55.1, 25.2),
        (64, "RADAR SAC112/SIC64", 54.9, 24.1),
    ]


def test_cat34_reports_missing_site_once(monkeypatch, capsys):
    module = importlib.import_module("protocols.vendors.asterix.cat")
    published = []
    monkeypatch.setattr(
        module,
        "_cat34_decode_cat034",
        lambda _data: {"msg_type": "north_marker", "sac": 112, "sic": 64},
    )
    monkeypatch.setattr(
        module,
        "publish_dual",
        lambda *_args, **_kwargs: published.append(True),
    )
    handler = module._cat34__make_cat034_handler(
        pub_sensor=object(),
        site=[None, None],
        radar_name="VERA-NG",
    )

    handler(b"first", False)
    handler(b"second", False)

    assert not published
    assert capsys.readouterr().out.count("has no site position") == 1
