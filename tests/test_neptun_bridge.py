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


def test_missile_ballistic_and_kab_use_missile_entity():
    for kind in ("missile", "ballistic", "kab"):
        (prefix, _), = bridge.threat_tracks({"threats": [_t(type=kind)]})
        assert prefix.endswith("/air/neptun/hostile/missile")


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


def test_layers_map_hostile_missile():
    assert '"air/**/hostile/missile/**"' in (ROOT / "compose/layers/vendors/tak/tak_layer.py").read_text()
    assert '"air/**/hostile/missile/**"' in (ROOT / "compose/layers/vendors/tak/systematic/sitaware_layer.py").read_text()


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
