"""mapa bridge: MAPA.UA /current document -> hostile uav/missile/bomb tracks with trail lines."""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

_spec = importlib.util.spec_from_file_location("mapa_bridge", ROOT / "compose/bridges/vendors/mapa/mapa_bridge.py")
mapa_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mapa_bridge)

NOW = 1791263500.0


def _obj(**kw):
    base = {"id": 7, "kind": "drone_jet", "amount": 1, "status": "active", "heading": 233, "lat": 51.04801, "lon": 31.88688,
            "last_seen": NOW - 10, "from_zone": "bryansk", "to_city": "nizhyn", "subkind": "drone_jet", "speed_kmh": 450,
            "predicted_lat": 50.98, "predicted_lon": 31.8, "trail": [[33.4, 52.3, 1], [31.9, 51.1, 2]]}
    base.update(kw)
    return base


def test_active_drone_becomes_a_hostile_uav_with_a_trail_line():
    tracks = mapa_bridge.object_tracks({"objects": [_obj()]}, NOW)
    (prefix, track), (trail_prefix, trail) = tracks
    assert prefix.endswith("/air/mapa/hostile/uav") and track["uid"] == "MAPA-7"
    assert track["callsign"] == "MAPA UAV Nizhyn" and track["stale_s"] == mapa_bridge.STALE_S
    assert round(track["speed_ms"]) == 125 and "MAPA.UA" in track["remarks"]
    assert trail_prefix.endswith("/air/mapa/trail/line") and trail["uid"] == "MAPA-7-TRAIL"
    assert trail["geometry"]["coordinates"][-1] == [31.88688, 51.04801]


def test_only_active_recent_valid_objects_are_published():
    objs = [_obj(id=1, status="lost"), _obj(id=2, last_seen=NOW - 99999), _obj(id=3, lat=999), _obj(id=4, kind="plane"),
            _obj(id=5, kind="missile", amount=3, trail=[]), _obj(id=6, kind="bomb_kab")]
    tracks = mapa_bridge.object_tracks({"objects": objs}, NOW, max_age_s=900)
    assert [t["uid"] for _, t in tracks] == ["MAPA-5", "MAPA-6", "MAPA-6-TRAIL"]
    assert tracks[0][1]["callsign"] == "MAPA MISSILE x3 Nizhyn" and tracks[0][0].endswith("/hostile/missile")
    assert tracks[1][0].endswith("/hostile/bomb")


def test_old_active_object_stays_by_default_but_is_marked_stale():
    old = _obj(last_seen=NOW - 1071)                       # the Sumy KAB: mapa.ua still showed it active
    (prefix, track), *_ = mapa_bridge.object_tracks({"objects": [old]}, NOW)
    assert track["callsign"] == "MAPA UAV Nizhyn (STALE)" and "STALE" in track["remarks"]
    fresh, *_ = mapa_bridge.object_tracks({"objects": [_obj(last_seen=NOW - 30)]}, NOW)
    assert "STALE" not in fresh[1]["callsign"]


def test_default_age_limit_drops_an_object_mapa_still_calls_active_after_hours():
    assert mapa_bridge.MAX_AGE_S == 1800
    tracks = mapa_bridge.object_tracks({"objects": [_obj(id=1, last_seen=NOW - 3 * 3600), _obj(id=2, last_seen=NOW - 60)]}, NOW)
    assert [t["uid"] for _, t in tracks if not t["uid"].endswith("-TRAIL")] == ["MAPA-2"]


def test_units_of_one_group_become_one_marker_with_a_count_at_their_centre():
    units = [_obj(id="9_u%d" % i, lat=51.0 + i * 0.001, lon=31.0, amount=1) for i in range(4)]
    markers = [t for _, t in mapa_bridge.object_tracks({"objects": units}, NOW) if not t["uid"].endswith("-TRAIL")]
    assert len(markers) == 1 and markers[0]["uid"] == "MAPA-9" and markers[0]["count"] == 4
    assert markers[0]["callsign"] == "MAPA UAV x4 Nizhyn" and abs(markers[0]["lat_deg"] - 51.0015) < 1e-6


def _mapa(uid, lat, lon, slot="uav", heading=None):
    return {"uid": uid, "target_type": slot, "lat_deg": lat, "lon_deg": lon, "heading_deg": heading}


def _neptun(uid, lat, lon, kind="uav", uncertainty_m=4000, heading=None):
    return {"uid": uid, "type": kind, "lat_deg": lat, "lon_deg": lon, "position_uncertainty_m": uncertainty_m, "heading_deg": heading}


def test_one_to_one_match_inside_the_neptun_error_radius_merges():
    assert mapa_bridge.merge_with_neptun({"MAPA-1": _mapa("MAPA-1", 50.0, 30.0)},
                                         {"NEPTUN-A": _neptun("NEPTUN-A", 50.02, 30.0)}) == {"MAPA-1": "NEPTUN-A"}


def test_no_merge_when_far_wrong_class_fpv_or_heading_disagrees():
    near = _neptun("NEPTUN-A", 50.02, 30.0)
    assert mapa_bridge.merge_with_neptun({"M": _mapa("M", 50.0, 30.0)}, {"N": _neptun("N", 50.5, 30.0)}) == {}
    assert mapa_bridge.merge_with_neptun({"M": _mapa("M", 50.0, 30.0, slot="missile")}, {"N": near}) == {}
    assert mapa_bridge.merge_with_neptun({"M": _mapa("M", 50.0, 30.0)}, {"N": _neptun("N", 50.0, 30.0, kind="fpv")}) == {}
    assert mapa_bridge.merge_with_neptun({"M": _mapa("M", 50.0, 30.0, heading=0)}, {"N": _neptun("N", 50.0, 30.0, heading=180)}) == {}


def test_ambiguous_pairings_are_left_as_two_markers():
    two_mapa = {"M1": _mapa("M1", 50.0, 30.0), "M2": _mapa("M2", 50.01, 30.0)}
    assert mapa_bridge.merge_with_neptun(two_mapa, {"N": _neptun("N", 50.0, 30.0)}) == {}
    two_neptun = {"N1": _neptun("N1", 50.0, 30.0), "N2": _neptun("N2", 50.01, 30.0)}
    assert mapa_bridge.merge_with_neptun({"M": _mapa("M", 50.0, 30.0)}, two_neptun) == {}
