"""deepstate bridge: DeepState map document -> unit and attack-direction markers only."""

import importlib.util
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

_spec = importlib.util.spec_from_file_location("deepstate_bridge", ROOT / "compose/bridges/vendors/deepstate/deepstate_bridge.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)

NOW = time.time()
BIG = [[30, 48], [32, 48], [32, 50], [30, 50], [30, 48]]


def _poly(key):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [BIG]},
            "properties": {"name": "UA /// x /// geoJSON.{}".format(key)}}


def _point(key, name, lon=36.0, lat=49.0):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat, 0]},
            "properties": {"name": "UA /// {} /// geoJSON.{}".format(name, key)}}


def _doc(*features, updated=None):
    return {"id": updated or NOW - 3600, "map": {"type": "FeatureCollection", "features": list(features)}}


DOC = _doc(_poly("status.occupied"), _poly("status.dismissed"), _poly("status.unknown"), _poly("territories.crimea"),
           _point("status.attack_direction", "Direction of attack"),
           _point("units.regiment.488", "488th Motor Rifle Regiment"), _point("moskow_cruiser", "Cruiser"))


def test_only_units_and_attack_arrows_become_hostile_unit_markers():
    out = ds.map_tracks(DOC, NOW)
    assert sorted(t["callsign"] for _, t in out.values()) == ["488th Motor Rifle Regiment", "Direction of attack"]
    assert all(p.endswith("/land/deepstate/frontline/hostile/unit") for p, _ in out.values())
    assert all("DeepStateMap" in t["remarks"] and "context only" in t["remarks"] for _, t in out.values())
    assert all("geometry" not in t for _, t in out.values())          # no territory polygons


def test_markers_can_be_switched_off():
    assert ds.map_tracks(DOC, NOW, markers=False) == {}


def test_malformed_documents_give_nothing_instead_of_raising():
    assert ds.map_tracks({}, NOW) == {} and ds.map_tracks({"map": {"features": [{"geometry": None}, {}]}}, NOW) == {}
    bad = _doc(_point("units.regiment.1", "x", lon="bad"), _point("units.regiment.2", "y", lat=95))
    assert ds.map_tracks(bad, NOW) == {}
