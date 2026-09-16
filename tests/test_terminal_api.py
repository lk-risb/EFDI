import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")
os.environ.setdefault("ZENOH_ADMIN_SECRET_KEY", "test-secret")
os.environ.setdefault("PARTNER_NAMESPACE", "router-a")

from api import terminal  # noqa: E402


class Sample:
    def __init__(self, key_expr: str, payload: bytes):
        self.key_expr = key_expr
        self.payload = payload


# ── _speed_kts ────────────────────────────────────────────────────────────

def test_speed_kts_prefers_mainline_terminals_own_ground_speed_field():
    assert terminal._speed_kts({"ground_speed_kts": 42, "speed_ms": 1}) == 42


def test_speed_kts_converts_normalized_tracks_speed_ms_to_knots():
    assert terminal._speed_kts({"speed_ms": 10}) == 10 * 1.943844


def test_speed_kts_returns_none_when_neither_field_present():
    assert terminal._speed_kts({}) is None


# ── _alt_m ────────────────────────────────────────────────────────────────

def test_alt_m_prefers_mainline_terminals_own_alt_m_field():
    assert terminal._alt_m({"alt_m": 1, "baro_alt_m": 2, "geo_alt_m": 3, "height_m": 4}) == 1


def test_alt_m_falls_back_through_normalized_track_altitude_fields_in_order():
    assert terminal._alt_m({"baro_alt_m": 2, "geo_alt_m": 3, "height_m": 4}) == 2
    assert terminal._alt_m({"geo_alt_m": 3, "height_m": 4}) == 3
    assert terminal._alt_m({"height_m": 4}) == 4


def test_alt_m_returns_none_when_no_altitude_field_present():
    assert terminal._alt_m({}) is None


# ── _drone_status ─────────────────────────────────────────────────────────

def test_drone_status_emergency_wins_over_everything_else():
    assert terminal._drone_status({"emergency": True, "flight_state": "flying", "armed": True}) == "emergency"


def test_drone_status_uses_mainline_terminals_own_flight_state_next():
    assert terminal._drone_status({"flight_state": "flying", "on_ground": True, "armed": True}) == "flying"


def test_drone_status_falls_back_to_on_ground():
    assert terminal._drone_status({"on_ground": True, "armed": True}) == "on_ground"


def test_drone_status_falls_back_to_armed():
    assert terminal._drone_status({"armed": True}) == "armed"


def test_drone_status_defaults_to_airborne():
    assert terminal._drone_status({}) == "airborne"


# ── _normalize_drone ────────────────────────────────────────────────────────

def test_normalize_drone_maps_normalized_track_shaped_payload():
    payload = {
        "uid": "trk-1",
        "callsign": "Falcon-1",
        "lat_deg": 54.1,
        "lon_deg": 25.2,
        "baro_alt_m": 100,
        "heading_deg": 90,
        "speed_ms": 5,
        "on_ground": False,
        "_src": "asterix",
        "_ts": 123.0,
    }
    entity = terminal._normalize_drone(payload)
    assert entity["id"] == "trk-1"
    assert entity["kind"] == "drone"
    assert entity["source"] == "asterix"
    assert entity["callsign"] == "Falcon-1"
    assert entity["lat"] == 54.1
    assert entity["lon"] == 25.2
    assert entity["alt_m"] == 100
    assert entity["heading_deg"] == 90
    assert entity["speed_kts"] == 5 * 1.943844
    assert entity["status"] == "airborne"
    assert entity["updated_ts"] == 123.0
    assert entity["raw"] is payload


def test_normalize_drone_maps_mainline_terminal_shaped_payload():
    payload = {
        "uid": "01M284Z4KQZV7DWTN951D0KDEK",
        "lat_deg": 54.376,
        "lon_deg": 25.341,
        "alt_m": 120,
        "armed": True,
        "flight_state": "flying",
        "ground_speed_kts": 25.3,
    }
    entity = terminal._normalize_drone(payload)
    assert entity["callsign"] == payload["uid"]  # no explicit callsign -> falls back to uid
    assert entity["source"] == "unknown"  # no _src/source field
    assert entity["alt_m"] == 120
    assert entity["speed_kts"] == 25.3
    assert entity["status"] == "flying"


def test_normalize_drone_returns_none_without_a_position_fix():
    assert terminal._normalize_drone({"uid": "trk-1", "lat_deg": None, "lon_deg": 25.2}) is None
    assert terminal._normalize_drone({"uid": "trk-1"}) is None


# ── _normalize_sensor ───────────────────────────────────────────────────────

def test_normalize_sensor_maps_dronuradaras_shaped_payload():
    payload = {
        "sensor_id": "sensor-1",
        "sensor_name": "IsoLOG 180",
        "lat_deg": 54.1,
        "lon_deg": 25.2,
        "is_online": True,
        "_ts": 111.0,
    }
    entity = terminal._normalize_sensor(payload)
    assert entity["id"] == "sensor-1"
    assert entity["kind"] == "sensor"
    assert entity["callsign"] == "IsoLOG 180"
    assert entity["status"] == "online"
    assert entity["alt_m"] is None
    assert entity["heading_deg"] is None
    assert entity["speed_kts"] is None
    assert entity["raw"] is payload


def test_normalize_sensor_falls_back_to_sensor_id_without_a_name():
    entity = terminal._normalize_sensor({"sensor_id": "sensor-1", "lat_deg": 1, "lon_deg": 2, "is_online": False})
    assert entity["callsign"] == "sensor-1"
    assert entity["status"] == "offline"


def test_normalize_sensor_returns_none_without_a_position_fix():
    assert terminal._normalize_sensor({"sensor_id": "sensor-1"}) is None


# ── _observe_drone / _observe_sensor ────────────────────────────────────────

def test_observe_drone_stores_by_uid_and_deletes_on_tombstone():
    terminal._DRONES.clear()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1}'))
    assert "trk-1" in terminal._DRONES
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "deleted": true}'))
    assert "trk-1" not in terminal._DRONES


def test_observe_drone_ignores_malformed_or_non_dict_payloads():
    terminal._DRONES.clear()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b"not json"))
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b"[1, 2]"))
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"no_uid": true}'))
    assert terminal._DRONES == {}


# ── _observe_ack ─────────────────────────────────────────────────────────────

def test_observe_ack_stores_latest_payload_keyed_by_entity_id_from_topic():
    terminal._ACKS.clear()
    terminal._observe_ack(Sample(
        "EFDI/router-a/air/mavlink/ack/drone-1",
        b'{"cmd": "arm", "result": "MAV_RESULT_ACCEPTED"}',
    ))
    assert terminal._ACKS["drone-1"] == {"cmd": "arm", "result": "MAV_RESULT_ACCEPTED"}
    # A later ack for the same entity replaces, not accumulates.
    terminal._observe_ack(Sample(
        "EFDI/router-a/air/mavlink/ack/drone-1",
        b'{"cmd": "land", "result": "TIMEOUT"}',
    ))
    assert terminal._ACKS["drone-1"] == {"cmd": "land", "result": "TIMEOUT"}


def test_observe_ack_ignores_malformed_or_non_dict_payloads():
    terminal._ACKS.clear()
    terminal._observe_ack(Sample("EFDI/router-a/air/mavlink/ack/drone-1", b"not json"))
    terminal._observe_ack(Sample("EFDI/router-a/air/mavlink/ack/drone-1", b"[1, 2]"))
    assert terminal._ACKS == {}


# ── _topic_root / _cmd_topic ─────────────────────────────────────────────────

def test_topic_root_matches_native_bridges_topic_root_shape(monkeypatch, tmp_path):
    # Same DATA_NAMESPACE_PREFIX_FILE-driven resolution namespace_prefix.py's
    # own topic_root() uses natively — see terminal._topic_root()'s docstring
    # for why this container reuses topics.py's _data_prefix() instead of
    # reimplementing state-file resolution.
    from api import topics
    data_prefix = tmp_path / "data-prefix"
    data_prefix.write_text("EFDI\n")
    monkeypatch.setattr(topics, "_DATA_PREFIX_FILE", str(data_prefix))
    monkeypatch.setenv("PARTNER_NAMESPACE", "site-alpha")

    assert terminal._topic_root() == "EFDI/site-alpha"


def test_topic_root_supports_slot_root_namespace(monkeypatch, tmp_path):
    from api import topics
    data_prefix = tmp_path / "data-prefix"
    data_prefix.write_text("")
    monkeypatch.setattr(topics, "_DATA_PREFIX_FILE", str(data_prefix))
    monkeypatch.setenv("PARTNER_NAMESPACE", "site-alpha")

    assert terminal._topic_root() == "site-alpha"


def test_cmd_topic_matches_mavlink_command_bridge_subscription_shape(monkeypatch, tmp_path):
    from api import topics
    data_prefix = tmp_path / "data-prefix"
    data_prefix.write_text("EFDI\n")
    monkeypatch.setattr(topics, "_DATA_PREFIX_FILE", str(data_prefix))
    monkeypatch.setenv("PARTNER_NAMESPACE", "site-alpha")

    assert terminal._cmd_topic("drone-1") == "EFDI/site-alpha/air/mavlink/cmd/drone-1"
