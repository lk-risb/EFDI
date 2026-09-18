import asyncio
import json
import os
import pathlib
import re
import sys

import pydantic
import pytest

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


# ── _mgrs_string ──────────────────────────────────────────────────────────
# Same mgrs library tak_layer.py already trusts for its own CoT remarks —
# guarded the same way, so a venv without the C extension degrades to no
# MGRS field instead of a boot crash.

@pytest.mark.skipif(terminal._MGRS is None, reason="mgrs not installed in this venv")
def test_mgrs_string_formats_a_known_lithuania_point():
    assert terminal._mgrs_string(54.376, 25.341) == "35U LA 9224 2662"


@pytest.mark.skipif(terminal._MGRS is None, reason="mgrs not installed in this venv")
def test_mgrs_string_pads_and_splits_digits_into_easting_northing_halves():
    result = terminal._mgrs_string(54.1, 25.2)
    zone, square, easting, northing = result.split(" ")
    assert re.fullmatch(r"\d{1,2}[A-Z]", zone)
    assert re.fullmatch(r"[A-Z]{2}", square)
    assert len(easting) == len(northing) == 4


def test_mgrs_string_returns_none_when_mgrs_is_unavailable(monkeypatch):
    monkeypatch.setattr(terminal, "_MGRS", None)
    assert terminal._mgrs_string(54.1, 25.2) is None


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
    assert entity["mgrs"] == terminal._mgrs_string(54.1, 25.2)
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


# ── _normalize_unit / _observe_unit ─────────────────────────────────────────
# Ground/operator-position tracks (e.g. AARTOS's WiFi/direction-finding
# block, once wired) publish under land/**/*/unit/** — a real gap found by
# comparing against mainline.inc TERMINAL's own phone/operator markers: this
# data already reached tak_layer.py's identical wildcard and rendered on
# TAK, but terminal.py never subscribed to it, so it never reached this map.

def test_normalize_unit_maps_a_ground_position_track():
    payload = {
        "uid": "aartos-op-1",
        "callsign": "Operator-1",
        "lat_deg": 54.1,
        "lon_deg": 25.2,
        "_src": "AARTOS",
        "_ts": 111.0,
    }
    entity = terminal._normalize_unit(payload)
    assert entity["id"] == "aartos-op-1"
    assert entity["kind"] == "unit"
    assert entity["callsign"] == "Operator-1"
    assert entity["lat"] == 54.1
    assert entity["lon"] == 25.2
    assert entity["raw"] is payload
    # AARTOS's own ground tracks carry none of _drone_status's fields
    # (armed/flight_state/on_ground only mean something for an airframe) —
    # must not silently default to "airborne" via _drone_status.
    assert entity["status"] == "unknown"


def test_normalize_unit_falls_back_to_uid_without_a_callsign():
    entity = terminal._normalize_unit({"uid": "aartos-op-2", "lat_deg": 1, "lon_deg": 2})
    assert entity["callsign"] == "aartos-op-2"


def test_normalize_unit_returns_none_without_a_position_fix():
    assert terminal._normalize_unit({"uid": "aartos-op-1"}) is None


# ── _unit_status ─────────────────────────────────────────────────────────

def test_unit_status_emergency_wins_over_everything_else():
    assert terminal._unit_status({"emergency": True, "is_online": False}) == "emergency"


def test_unit_status_reports_online_from_is_online_true():
    assert terminal._unit_status({"is_online": True}) == "online"


def test_unit_status_reports_offline_from_is_online_false():
    assert terminal._unit_status({"is_online": False}) == "offline"


def test_unit_status_defaults_to_unknown_without_online_data():
    assert terminal._unit_status({}) == "unknown"


def test_observe_unit_stores_by_uid_and_deletes_on_tombstone():
    terminal._UNITS.clear()
    terminal._observe_unit(Sample("EFDI/x/land/aartos/passive_rf/unknown/unit", b'{"uid": "op-1", "lat_deg": 1, "lon_deg": 2}'))
    assert "op-1" in terminal._UNITS
    terminal._observe_unit(Sample("EFDI/x/land/aartos/passive_rf/unknown/unit", b'{"uid": "op-1", "deleted": true}'))
    assert "op-1" not in terminal._UNITS


def test_observe_unit_ignores_malformed_or_non_dict_payloads():
    terminal._UNITS.clear()
    terminal._observe_unit(Sample("EFDI/x/land/aartos/passive_rf/unknown/unit", b"not json"))
    terminal._observe_unit(Sample("EFDI/x/land/aartos/passive_rf/unknown/unit", b"[1, 2]"))
    terminal._observe_unit(Sample("EFDI/x/land/aartos/passive_rf/unknown/unit", b'{"no_uid": true}'))
    assert terminal._UNITS == {}


def test_list_entities_key_expressions_cover_air_sensor_and_unit_topics():
    # Regression guard for the exact gap that was found: a unit-kind track
    # under land/**/*/unit/** must actually match _UNIT_KEY_EXPR, the same
    # way tak_layer.py's own wildcard already matches it.
    pattern = terminal._UNIT_KEY_EXPR.replace("**", ".*")
    topic = "EFDI/site-a/land/aartos/passive_rf/hostile/unit/aartos-op-1"
    assert re.fullmatch(pattern, topic)
    assert not re.fullmatch(terminal._AIR_KEY_EXPR.replace("**", ".*"), topic)
    assert not re.fullmatch(terminal._SENSOR_KEY_EXPR.replace("**", ".*"), topic)


# ── AssetIn / ZoneIn validation (pure Pydantic, no DB needed) ────────────────

def test_asset_in_accepts_a_valid_payload():
    asset = terminal.AssetIn(name="GSM Tower 1", category="gsm_tower", lat_deg=54.1, lon_deg=25.2)
    assert asset.category == "gsm_tower"


def test_asset_in_rejects_an_empty_name():
    with pytest.raises(pydantic.ValidationError):
        terminal.AssetIn(name="", lat_deg=54.1, lon_deg=25.2)


def test_zone_in_rejects_a_non_positive_radius():
    with pytest.raises(pydantic.ValidationError):
        terminal.ZoneIn(name="Alpha Zone", center_lat_deg=54.1, center_lon_deg=25.2, radius_m=0)


def test_zone_in_rejects_an_absurdly_large_radius():
    with pytest.raises(pydantic.ValidationError):
        terminal.ZoneIn(name="Alpha Zone", center_lat_deg=54.1, center_lon_deg=25.2, radius_m=500_000)


def test_zone_in_accepts_a_valid_payload():
    zone = terminal.ZoneIn(name="Alpha Zone", tier="crit", center_lat_deg=54.1, center_lon_deg=25.2, radius_m=500)
    assert zone.tier == "crit"


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


# ── event log (_push_event / _observe_drone / _observe_ack) ─────────────────

def _reset_event_state():
    terminal._EVENTS.clear()
    terminal._LAST_STATUS.clear()
    terminal._LAST_ALERTS.clear()
    terminal._DRONES.clear()
    terminal._ACKS.clear()


def test_observe_drone_does_not_log_a_status_event_on_first_sighting():
    _reset_event_state()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "armed": true}'))
    assert terminal._EVENTS == []
    assert terminal._LAST_STATUS["trk-1"] == "armed"


def test_observe_drone_logs_a_status_transition_after_first_sighting():
    _reset_event_state()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "armed": true}'))
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "on_ground": true}'))
    assert len(terminal._EVENTS) == 1
    event = terminal._EVENTS[0]
    assert event["entity_id"] == "trk-1"
    assert event["kind"] == "status"
    assert event["message"] == "status -> on_ground"
    assert event["severity"] == "info"


def test_observe_drone_logs_emergency_status_as_critical():
    _reset_event_state()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "armed": true}'))
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "emergency": true}'))
    assert terminal._EVENTS[-1]["severity"] == "crit"


def test_observe_drone_does_not_repeat_an_unchanged_status():
    _reset_event_state()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "armed": true}'))
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "armed": true}'))
    assert terminal._EVENTS == []


def test_observe_drone_logs_a_new_alert_after_first_sighting_with_parsed_severity():
    _reset_event_state()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1}'))
    terminal._observe_drone(Sample(
        "EFDI/x/air/trk-1",
        json.dumps({"uid": "trk-1", "lat_deg": 1, "alerts": "SD card full: 100% (crit)"}).encode(),
    ))
    alert_events = [e for e in terminal._EVENTS if e["kind"] == "alert"]
    assert len(alert_events) == 1
    assert alert_events[0]["message"] == "SD card full: 100% (crit)"
    assert alert_events[0]["severity"] == "crit"


def test_observe_drone_does_not_log_alert_on_first_sighting_even_if_present():
    _reset_event_state()
    terminal._observe_drone(Sample(
        "EFDI/x/air/trk-1",
        json.dumps({"uid": "trk-1", "lat_deg": 1, "alerts": "SD card full: 100% (crit)"}).encode(),
    ))
    assert terminal._EVENTS == []
    assert terminal._LAST_ALERTS["trk-1"] == "SD card full: 100% (crit)"


def test_observe_drone_clears_deleted_entity_from_status_and_alert_caches():
    _reset_event_state()
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "lat_deg": 1, "armed": true}'))
    terminal._observe_drone(Sample("EFDI/x/air/trk-1", b'{"uid": "trk-1", "deleted": true}'))
    assert "trk-1" not in terminal._LAST_STATUS
    assert "trk-1" not in terminal._DRONES


def test_observe_ack_logs_a_cmd_event_with_severity_from_result():
    _reset_event_state()
    terminal._observe_ack(Sample(
        "EFDI/router-a/air/mavlink/ack/drone-1",
        b'{"cmd": "arm", "result": "MAV_RESULT_ACCEPTED"}',
    ))
    terminal._observe_ack(Sample(
        "EFDI/router-a/air/mavlink/ack/drone-1",
        b'{"cmd": "land", "result": "MAV_RESULT_DENIED"}',
    ))
    assert terminal._EVENTS[0]["message"] == "arm MAV_RESULT_ACCEPTED"
    assert terminal._EVENTS[0]["severity"] == "info"
    assert terminal._EVENTS[1]["message"] == "land MAV_RESULT_DENIED"
    assert terminal._EVENTS[1]["severity"] == "warn"


def test_events_ring_buffer_caps_at_events_cap(monkeypatch):
    _reset_event_state()
    monkeypatch.setattr(terminal, "_EVENTS_CAP", 5)
    for i in range(10):
        terminal._push_event("trk-1", "status", "info", "event {}".format(i))
    assert len(terminal._EVENTS) == 5
    assert terminal._EVENTS[-1]["message"] == "event 9"


# ── system health poller (_poll_system_health) ───────────────────────────────

class _StopPoll(Exception):
    pass


def _run_one_poll_cycle(monkeypatch, control_responses):
    """Drives _poll_system_health() through exactly len(control_responses)
    iterations, then stops it the same way task.cancel() would."""
    responses = iter(control_responses)

    def fake_control(path):
        assert path == "/v1/runtime"
        return next(responses)

    calls = {"n": 0}

    async def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= len(control_responses):
            raise _StopPoll

    monkeypatch.setattr(terminal, "_control", fake_control)
    monkeypatch.setattr(terminal.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(terminal._poll_system_health())
    except _StopPoll:
        pass


def _reset_health_state():
    terminal._EVENTS.clear()
    terminal._LAST_SERVICE_STATUS.clear()


def test_system_health_poll_does_not_log_on_first_sighting(monkeypatch):
    _reset_health_state()
    _run_one_poll_cycle(monkeypatch, [
        {"services": [{"name": "aartos-raw", "status": "running"}]},
    ])
    assert terminal._EVENTS == []
    assert terminal._LAST_SERVICE_STATUS["aartos-raw"] == "running"


def test_system_health_poll_logs_a_crash_transition_as_critical(monkeypatch):
    _reset_health_state()
    _run_one_poll_cycle(monkeypatch, [
        {"services": [{"name": "aartos-raw", "status": "running"}]},
        {"services": [{"name": "aartos-raw", "status": "crashed"}]},
    ])
    events = [e for e in terminal._EVENTS if e["entity_id"] == "aartos-raw"]
    assert len(events) == 1
    assert events[0]["kind"] == "system"
    assert events[0]["severity"] == "crit"
    assert events[0]["message"] == "aartos-raw -> crashed"


def test_system_health_poll_does_not_repeat_an_unchanged_status(monkeypatch):
    _reset_health_state()
    _run_one_poll_cycle(monkeypatch, [
        {"services": [{"name": "aartos-raw", "status": "running"}]},
        {"services": [{"name": "aartos-raw", "status": "running"}]},
    ])
    assert terminal._EVENTS == []


def test_system_health_poll_survives_a_control_agent_error(monkeypatch):
    _reset_health_state()

    def fake_control(path):
        raise ConnectionError("host control agent unavailable")

    async def fake_sleep(_seconds):
        raise _StopPoll

    monkeypatch.setattr(terminal, "_control", fake_control)
    monkeypatch.setattr(terminal.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(terminal._poll_system_health())
    except _StopPoll:
        pass
    assert terminal._EVENTS == []
