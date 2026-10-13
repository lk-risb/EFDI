"""Radar-site CoT compatibility regressions."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "layers" / "vendors" / "tak"))

import tak_layer  # noqa: E402


class _Sender:
    def __init__(self):
        self.frames = []

    def send(self, xml):
        self.frames.append(xml)


class _Sample:
    key_expr = "test/land/112-64/radar/friendly/radar/CAT34-112-64"

    def __init__(self, track):
        self.payload = json.dumps(track).encode()


def test_cat34_radar_has_one_dedicated_route_and_friendly_affiliation():
    assert "land/**/neutral/radar/**" not in tak_layer._TOPIC_COT
    assert tak_layer._MIL_RADAR_COT_TYPE == "a-f-G-E-S-R" and tak_layer._RADAR_COT_TYPE == "a-n-G-E-S-R"   # ATC stations stay neutral

    sender = _Sender()
    handler = tak_layer.make_radar_status_handler(sender, verbose=False)
    handler(_Sample({
        "_src": "ASTERIX CAT-34 Ed.1.29",
        "_ts": 1_721_000_000,
        "sensor_id": "CAT34-112-64",
        "sensor_name": "VERA-NG 1",
        "sensor_type": "radar",
        "sac": 112,
        "sic": 64,
        "lat_deg": 54.9,
        "lon_deg": 24.1,
        "radar_range_m": 100_000,
    }))

    assert len(sender.frames) == 1
    event = ET.fromstring(sender.frames[0])
    assert event.attrib["uid"] == "EFDI-SENS-CAT34-112-64"
    assert event.attrib["type"] == "a-f-G-E-S-R"
    assert event.find("./detail/contact").attrib["callsign"] == "VERA-NG 1"
    sensor = event.find("./detail/sensor")
    assert sensor.attrib["type"] == "radar"
    assert sensor.attrib["fov"] == "360"
    assert sensor.attrib["azimuth"] == "0"
    assert sensor.attrib["range"] == "100000"


def test_cat34_beam_uses_separate_uid_without_changing_affiliation():
    sender = _Sender()
    handler = tak_layer.make_radar_status_handler(sender, verbose=False)
    handler(_Sample({
        "_ts": 1_721_000_000,
        "sensor_id": "CAT34-112-64",
        "sensor_name": "VERA-NG 1",
        "lat_deg": 54.9,
        "lon_deg": 24.1,
        "radar_range_m": 100_000,
        "rotation_s": 4,
        "sweep_azimuth_deg": 90,
    }))

    event = ET.fromstring(sender.frames[0])
    assert event.attrib["uid"] == "EFDI-SENS-BEAM-CAT34-112-64"
    assert event.attrib["type"] == tak_layer._MIL_RADAR_COT_TYPE
    sensor = event.find("./detail/sensor")
    assert sensor.attrib["fov"] == "5"
    assert sensor.attrib["azimuth"] == "90"
    assert sensor.attrib["range"] == "100000"


def test_a_site_on_the_neutral_slot_is_a_civil_radar_and_is_drawn_neutral():
    class _Civil(_Sample):
        key_expr = "test/land/048-010/radar/neutral/radar/CAT34-48-10"
    sender = _Sender()
    tak_layer.make_radar_status_handler(sender, verbose=False)(_Civil({
        "_ts": 1_721_000_000, "sensor_id": "CAT34-48-10", "sensor_name": "ATC 1", "sac": 48, "sic": 10,
        "lat_deg": 54.9, "lon_deg": 24.1, "radar_range_m": 100_000}))
    assert ET.fromstring(sender.frames[0]).attrib["type"] == "a-n-G-E-S-R"


def test_asterix_radar_sites_publish_friendly_unless_listed_as_civil(monkeypatch):
    from protocols.vendors.asterix import cat
    monkeypatch.delenv("ASTERIX_CIVIL_RADARS", raising=False)
    assert cat._radar_site_affil("112-064") == "friendly"
    monkeypatch.setenv("ASTERIX_CIVIL_RADARS", "48-10, 049-011, junk")
    assert cat._radar_site_affil("048-010") == "neutral" and cat._radar_site_affil("049-011") == "neutral"
    assert cat._radar_site_affil("112-064") == "friendly"
    assert cat._cat34_TOPIC_SENSOR.format(source="048-010", affil="neutral").endswith("/land/048-010/radar/neutral/radar")
