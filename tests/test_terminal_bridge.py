import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
sys.path.insert(0, str(COMPOSE))
sys.path.insert(0, str(COMPOSE / "control"))

spec = importlib.util.spec_from_file_location(
    "terminal_bridge",
    COMPOSE / "bridges" / "vendors" / "mainline" / "terminal_bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bridge)


def _state(**overrides):
    base = {
        "position": {"data": {"lat": 54.376, "lng": 25.341, "alt": 120, "heading": 224.7, "hSpeed": 13}},
        "flightState": {"data": {"armed": True, "state": "flying", "modeCode": 5}},
        "battery": {"data": {"percent": 54.2}},
        "health": {"data": {"alerts": []}},
        "tasking": {"data": {"missionId": "stg-patrol-5", "phase": "onstation"}},
    }
    base.update(overrides)
    return base


def test_flight_state_maps_position_and_status_fields():
    track = bridge._flight_state_to_track("01M284Z4KQZV7DWTN951D0KDEK", _state(), "Drone-1")
    assert track["_src"] == "mainline_terminal"
    assert track["uid"] == "01M284Z4KQZV7DWTN951D0KDEK"
    assert track["callsign"] == "Drone-1"
    assert track["lat_deg"] == 54.376
    assert track["lon_deg"] == 25.341
    assert track["alt_m"] == 120
    assert track["armed"] is True
    assert track["flight_state"] == "flying"
    assert track["battery_pct"] == 54.2
    assert track["mission_id"] == "stg-patrol-5"
    assert track["mission_phase"] == "onstation"
    assert "alerts" not in track


def test_flight_state_falls_back_to_entity_id_without_a_name():
    track = bridge._flight_state_to_track("01M284Z4KQZV7DWTN951D0KDEK", _state(), None)
    assert track["callsign"] == "01M284Z4KQZV7DWTN951D0KDEK"


def test_flight_state_converts_ground_speed_to_knots():
    track = bridge._flight_state_to_track("e1", _state(), "d")
    assert track["ground_speed_kts"] == 13 * 1.943844


def test_flight_state_joins_health_alerts_into_one_string():
    state = _state(health={"data": {"alerts": [
        {"code": "storage-full", "message": "SD card full: 100%", "severity": "crit"},
    ]}})
    track = bridge._flight_state_to_track("e1", state, "d")
    assert track["alerts"] == "SD card full: 100% (crit)"


def test_flight_state_returns_none_without_a_position_fix():
    state = _state(position={"data": None})
    assert bridge._flight_state_to_track("e1", state, "d") is None


def test_flight_state_returns_none_when_lat_or_lng_missing():
    state = _state(position={"data": {"alt": 100}})
    assert bridge._flight_state_to_track("e1", state, "d") is None
