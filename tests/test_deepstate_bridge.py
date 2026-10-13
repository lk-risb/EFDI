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


def test_a_unit_key_tells_the_size_and_the_arm_of_service():
    uc = ds.unit_class
    assert uc("geoJSON.units.brigade.127-motorized-rifle") == {"echelon": "brigade", "arm": "mech_infantry"}
    assert uc("geoJSON.units.division.3-motorized-rifle") == {"echelon": "division", "arm": "mech_infantry"}
    assert uc("geoJSON.units.regiment.1-tank") == {"echelon": "regiment", "arm": "armor"}
    assert uc("geoJSON.units.brigade.103-airborne") == {"echelon": "brigade", "arm": "airborne"}
    assert uc("geoJSON.units.battalion.7-reconnaissance") == {"echelon": "battalion", "arm": "recon"}
    assert uc("geoJSON.units.army.41-combined-arms") == {"echelon": "army"}              # no single arm: the generic unit
    assert uc("geoJSON.units.brigade.nevskyi") == {"echelon": "brigade"}
    assert uc("geoJSON.units.belarus.general") == {} and uc("geoJSON.status.attack_direction") == {} and uc("") == {}


def test_a_unit_carries_its_class_and_both_c2s_draw_it_by_arm_and_size():
    from layers.vendors.tak import tak_layer
    from layers.vendors.tak.systematic import sitaware_layer
    doc = {"id": NOW, "map": {"features": [_point("units.brigade.127-motorized-rifle", "127th Motorized Rifle Brigade"),
                                           _point("units.army.41-combined-arms", "41st Army")]}}
    tracks = {t["callsign"]: t for _, t in ds.map_tracks(doc, NOW).values()}
    brigade, army = tracks["127th Motorized Rifle Brigade"], tracks["41st Army"]
    topic = tak_layer.TOPIC_ROOT + "/land/deepstate/frontline/hostile/unit/x/tracks/v1"
    rel = topic[len(tak_layer.TOPIC_ROOT) + 1:]
    cot = {t["callsign"]: v[0](t) for t in (brigade, army) for p, v in tak_layer._TOPIC_COT.items() if p == "land/**/hostile/unit/**"}
    assert cot == {"127th Motorized Rifle Brigade": "a-h-G-U-C-I-Z", "41st Army": "a-h-G-U-C"}
    sidc = sitaware_layer._TOPIC_SIDC["land/**/hostile/unit/**"]
    assert sidc(brigade) == "SHGPUCIZ--*H***" and sidc(army) == "SHGPUC----*K***"
    assert sidc({"callsign": "x"}) == "SHGPUC----*****"                                  # no class: as before


def test_the_russian_held_territory_is_announced_as_rings_and_not_drawn():
    doc = _doc(_poly("status.occupied"), _poly("territories.crimea"), _poly("territories.ordlo"), _poly("status.dismissed"),
               _poly("status.unknown"), _poly("territories.estonia"), _point("units.brigade.1-tank", "1st Tank Brigade"))
    rings = ds.occupied_rings(doc)
    assert len(rings) == 3 and all(ring[0] == [30, 48] for ring in rings)           # occupied, ordlo, crimea only
    assert not [t for _, t in ds.map_tracks(doc, NOW).values() if t.get("geometry")]
