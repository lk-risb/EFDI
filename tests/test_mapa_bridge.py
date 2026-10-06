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
