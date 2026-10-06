"""geozones bridge: dronealerts GeoJSON -> standing blue zone polygons for the wanted countries."""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

_spec = importlib.util.spec_from_file_location("geozones_bridge", ROOT / "compose/bridges/vendors/dronealerts/geozones_bridge.py")
gz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gz)

RING = [[24.0, 59.0], [24.1, 59.0], [24.1, 59.1], [24.0, 59.1], [24.0, 59.0]]


def _feature(country, title="EEGZ24", ring=RING, kind="Polygon"):
    return {"type": "Feature", "properties": {"country": country, "title": title, "zoneType": "COMMON"},
            "geometry": {"type": kind, "coordinates": [ring]}}


def test_only_wanted_countries_and_valid_polygons_become_zones():
    data = {"features": [_feature("EE"), _feature("FI"), _feature("PL", kind="Point"), _feature("PL", ring=RING[:3]),
                         _feature("PL", ring=[[24.0, 959.0]] + RING[1:])]}
    zones = gz.zone_tracks(data, 1.0, {"EE", "PL"})
    assert len(zones) == 1
    (uid, track), = zones.items()
    assert uid.startswith("GEOZONE-EE-EEGZ24-") and track["shape_style"] == "geozone" and track["stale_s"] == 3600
    assert track["callsign"] == "EE UAS zone EEGZ24" and "not an air-raid alert" in track["remarks"]
    assert track["geometry"]["coordinates"][0][0] == track["geometry"]["coordinates"][0][-1]


def test_same_title_different_shape_gets_distinct_uids():
    other = [[p[0] + 1, p[1]] for p in RING]
    zones = gz.zone_tracks({"features": [_feature("EE"), _feature("EE", ring=other)]}, 1.0, {"EE"})
    assert len(zones) == 2
