"""NEPTUN threats become hostile tracks with stable uids; layers have a missile mapping."""

import pathlib
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

from bridges.vendors.dangausakis import neptun as bridge  # noqa: E402
from protocols.vendors.random.track_views import semantic_topic, add_version  # noqa: E402


def _iso(age_s):
    return datetime.fromtimestamp(time.time() - age_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(**kw):
    t = {"id": "trk_1", "type": "uav", "lat": 50.8, "lon": 31.8, "heading": 183, "status": "active",
         "locality": "Zgurivka", "region": "Kyiv", "confidenceLevel": "medium", "sourceCount": 2,
         "positionQuality": "approx", "uncertaintyKm": 4, "updatedAt": _iso(20)}
    t.update(kw)
    return t


def test_uav_track_fields_and_key():
    (prefix, track), = bridge.threat_tracks({"threats": [_t()]})
    assert prefix.endswith("/air/neptun/hostile/uav")
    assert track["uid"] == "NEPTUN-trk_1" and track["heading_deg"] == 183
    assert track["position_uncertainty_m"] == 4000 and track["callsign"] == "NEPTUN UAV Zgurivka"
    key = add_version(semantic_topic(prefix, track))
    assert "/hostile/uav/uav/neptun-trk_1/" in key.lower() and key.endswith("/tracks/v1")


def test_missile_and_ballistic_use_missile_entity_and_kab_is_a_bomb():
    for kind in ("missile", "ballistic"):
        (prefix, _), = bridge.threat_tracks({"threats": [_t(type=kind)]})
        assert prefix.endswith("/air/neptun/hostile/missile")
    (prefix, track), = bridge.threat_tracks({"threats": [_t(type="kab")]})
    assert prefix.endswith("/air/neptun/hostile/bomb") and track["target_type"] == "bomb"


def test_mig31k_is_a_hostile_aircraft_and_unknown_types_are_not_drones():
    (prefix, track), = bridge.threat_tracks({"threats": [_t(type="mig31k")]})
    assert prefix.endswith("/air/neptun/hostile/aircraft") and track["target_type"] == "aircraft"
    for kind in ("unknown", "something_new"):
        (prefix, _), = bridge.threat_tracks({"threats": [_t(type=kind)]})
        assert prefix.endswith("/air/neptun/unknown/aircraft")


def test_area_only_inactive_and_incomplete_are_skipped():
    threats = [_t(areaOnly=True), _t(id="b", status="expired"), _t(id=None), _t(id="c", lat=None)]
    assert bridge.threat_tracks({"threats": threats}) == []


def test_same_position_different_ids_are_not_merged():
    out = bridge.threat_tracks({"threats": [_t(id="a"), _t(id="b")]})
    assert {tr["uid"] for _, tr in out} == {"NEPTUN-a", "NEPTUN-b"}


def test_layers_map_hostile_missile_and_bomb_apart():
    tak = (ROOT / "compose/layers/vendors/tak/tak_layer.py").read_text()
    sitaware = (ROOT / "compose/layers/vendors/tak/systematic/sitaware_layer.py").read_text()
    for text in (tak, sitaware):
        assert '"air/**/hostile/missile/**"' in text and '"air/**/hostile/bomb/**"' in text
    assert '("a-h-A-W-B"' in tak and '"SHAPWB----*****"' in sitaware


def test_attribution_link_is_in_remarks():
    (_, track), = bridge.threat_tracks({"threats": [_t()]})
    assert "https://neptun.in.ua/" in track["remarks"]


def test_receipt_time_is_ts_and_report_age_is_carried():
    (_, track), = bridge.threat_tracks({"threats": [_t(updatedAt=_iso(60))]})
    assert abs(track["_ts"] - time.time()) < 5                 # we stamp receipt, so layers keep it alive
    assert 55 <= track["position_age_s"] <= 70 and track["observed_ts"] < track["_ts"]
    assert "report_stale" not in track and "STALE" not in track["callsign"]


def test_old_report_is_marked_stale_but_still_published():
    (_, track), = bridge.threat_tracks({"threats": [_t(updatedAt=_iso(bridge.STALE_S + 60))]})
    assert track["report_stale"] is True and track["callsign"].endswith("(STALE)")
    assert "STALE" in track["remarks"] and "last report " in track["remarks"]


def test_report_older_than_max_age_is_dropped():
    assert bridge.threat_tracks({"threats": [_t(updatedAt=_iso(bridge.MAX_AGE_S + 60))]}) == []


def test_remarks_carry_confidence_sources_accuracy_and_disclaimer():
    (_, track), = bridge.threat_tracks({"threats": [_t()]})
    for part in ("https://neptun.in.ua/", "not radar or an official warning", "confidence: medium",
                 "sources: 2", "position +/-4 km", "last report"):
        assert part in track["remarks"]


def test_missing_updated_at_is_treated_as_fresh():
    (_, track), = bridge.threat_tracks({"threats": [_t(updatedAt=None)]})
    assert "position_age_s" not in track and "report_stale" not in track


def test_place_names_are_romanised_and_the_explanation_is_translated():
    (_, track), = bridge.threat_tracks({"threats": [_t(
        locality="Славутич", region="Київська область", district="Бахмутський район",
        explanationShort="Розвідувальний БпЛА курсом на Старий Салтів. Підтверджень: 3.")]})
    assert track["callsign"] == "NEPTUN UAV Slavutych"
    assert track["region"] == "Kyivska Oblast" and track["district"] == "Bakhmutskyi District"
    assert track["explanation"] == "Reconnaissance UAV heading for Staryi Saltiv. Confirmations: 3."
    assert "Reconnaissance UAV heading for Staryi Saltiv" in track["remarks"]


def test_count_lifecycle_confirmation_and_presumed_course_are_carried():
    (_, track), = bridge.threat_tracks({"threats": [_t(count=3, lifecycle="confirmed", presumptiveCourse=True,
                                                        confirmedAt=_iso(60))]})
    assert track["count"] == 3 and track["callsign"] == "NEPTUN UAV x3 Zgurivka"
    assert track["lifecycle"] == "confirmed" and track["course_presumptive"] is True
    assert track["confirmed_ts"] is not None
    assert "count: 3" in track["remarks"] and "track status: confirmed" in track["remarks"]
    assert "course is presumed" in track["remarks"]
    (_, plain), = bridge.threat_tracks({"threats": [_t(count=1)]})
    assert "count" not in plain and "course_presumptive" not in plain and " x" not in plain["callsign"]


def test_trail_becomes_a_line_track_ending_at_the_current_position():
    trail = [{"lat": 50.0, "lon": 31.0, "t": _iso(300)}, {"lat": 50.4, "lon": 31.4, "t": _iso(200)},
             {"lat": 50.4, "lon": 31.4, "t": _iso(190)}, {"lat": "bad", "lon": 31.5}]
    out = bridge.threat_tracks({"threats": [_t(trail=trail)]})
    assert [p.rsplit("/", 1)[-1] for p, _ in out] == ["uav", "line"]
    prefix, line = out[1]
    assert prefix.endswith("/air/neptun/trail/line") and line["uid"] == "NEPTUN-trk_1-TRAIL"
    assert line["geometry"] == {"type": "LineString", "coordinates": [[31.0, 50.0], [31.4, 50.4], [31.8, 50.8]]}
    assert line["shape_style"] == "trail" and "Path of the last 3 reports" in line["remarks"]
    assert not any(t["uid"].endswith("-TRAIL") for _, t in bridge.threat_tracks({"threats": [_t()]}))
    assert len(bridge.threat_tracks({"threats": [_t(trail=[{"lat": 50.8, "lon": 31.8}])]})) == 1   # one point is no line


def test_trail_is_published_as_json_and_follows_the_fusion_suppression_of_its_threat():
    from unittest import mock
    out = bridge.threat_tracks({"threats": [_t(trail=[{"lat": 50.0, "lon": 31.0}])]})
    session, suppression = mock.Mock(), bridge.Suppression()
    with mock.patch.object(bridge, "publish_dual") as dual:
        bridge.publish_threats(session, out, suppression, set())
        assert dual.call_count == 1 and session.put.call_count == 1          # marker via dual, trail as JSON
        suppression.on_sample(mock.Mock(payload=b'{"uid": "NEPTUN-trk_1"}'))
        retracted = set()
        bridge.publish_threats(session, out, suppression, retracted)
        assert dual.call_count == 1 and retracted == {"NEPTUN-trk_1", "NEPTUN-trk_1-TRAIL"}


def test_tak_draws_a_trail_as_an_open_drawing_line():
    sys.path.insert(0, str(ROOT / "compose" / "layers" / "vendors" / "tak"))
    import tak_layer
    out = bridge.threat_tracks({"threats": [_t(trail=[{"lat": 50.0, "lon": 31.0}, {"lat": 50.4, "lon": 31.4}])]})
    xml = tak_layer.track_to_cot(out[1][1], "a-h-A")
    assert 'type="u-d-f"' in xml and 'how="h-e"' in xml and xml.count("<link point=") == 3
    assert "strokeColor" in xml and 'fillColor value="0"' in xml and "<shape>" not in xml
    assert '"air/**/trail/**"' in (ROOT / "compose/layers/vendors/tak/tak_layer.py").read_text()


def test_region_crossing_alerts_only_when_a_known_threat_changes_region():
    state: dict = {}
    first = bridge.threat_tracks({"threats": [_t(region="Sumy Oblast", count=3)]})
    assert bridge.region_crossings(state, first, 100.0) == []                      # first sighting: nothing
    same = bridge.threat_tracks({"threats": [_t(region="Sumy Oblast", lat=50.9)]})
    assert bridge.region_crossings(state, same, 130.0) == []
    moved = bridge.threat_tracks({"threats": [_t(region="Poltava Oblast", lat=50.2, count=3)]})
    (event,) = bridge.region_crossings(state, moved, 160.0)
    assert event["alert_type"] == "drone_crossing" and event["from"] == "Sumy Oblast" and event["to"] == "Poltava Oblast"
    assert event["kind"] == "uav" and event["count"] == 3 and event["uid"] == "NEPTUN-trk_1"
    assert bridge.region_crossings(state, [], 190.0) == [] and state == {}         # a threat that left is forgotten


def test_region_crossing_ignores_trail_lines_and_threats_without_a_region():
    state: dict = {}
    tracks = bridge.threat_tracks({"threats": [_t(region="", trail=[{"lat": 50.0, "lon": 31.0}, {"lat": 50.5, "lon": 31.5}])]})
    assert bridge.region_crossings(state, tracks, 1.0) == [] and state == {}
