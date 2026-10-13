"""Drone Strike Map bridge: day file -> explosion markers, alerts -> red Russian regions, and how the layers draw them.
The fixtures are trimmed copies of the site's real /api/day and /api/alerts documents."""

import importlib.util
import json
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols"):
    sys.path.insert(0, str(ROOT / sub))

from layers.vendors.tak import tak_layer  # noqa: E402
from layers.vendors.tak.systematic import sitaware_layer  # noqa: E402
from protocols.vendors.random import region_geometry  # noqa: E402
from protocols.vendors.random.track_views import semantic_topic  # noqa: E402

_spec = importlib.util.spec_from_file_location("dsm_bridge", ROOT / "compose/bridges/vendors/dronestrikemap/dronestrikemap_bridge.py")
dsm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dsm)

FIXTURES = ROOT / "tests" / "fixtures" / "dronestrikemap"
DAY = json.loads((FIXTURES / "day.json").read_text())
ALERTS = json.loads((FIXTURES / "alerts.json").read_text())
NOW = time.time()


def _square(lon, lat, size=1.0):
    return [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]


def _regions():
    return [{"id": "RU-BEL", "name": "Belgorod Oblast", "rings": [_square(35, 50)]},
            {"id": "RU-MOS", "name": "Moscow Oblast", "rings": [_square(36, 55)]},
            {"id": "RU-MOW", "name": "Moscow", "rings": [_square(37, 55.5, 0.3)]}]


def _match(pattern: str, topic: str) -> bool:
    regex = pattern.replace("**", "\0").replace("*", "[^/]+").replace("\0", ".*")
    return re.match("^" + regex + "$", topic + "/x") is not None


def _cot(topic: str):
    rel = topic[len(tak_layer.TOPIC_ROOT) + 1:]
    return [v for p, (v, _s) in tak_layer._TOPIC_COT.items() if _match(p, rel)]


def _sidc(topic: str):
    rel = topic[len(tak_layer.TOPIC_ROOT) + 1:]
    return [v for p, v in sitaware_layer._TOPIC_SIDC.items() if _match(p, rel)]


def test_a_place_hit_in_russia_is_a_hostile_incident_and_one_in_ukraine_a_friendly_one():
    out = dsm.strike_tracks(DAY, NOW)
    belgorod = out["DSM-2026-10-12-RU-BELGOROD"]
    assert belgorod[0].endswith("/land/dronestrikemap/strike/hostile/incident")
    track = belgorod[1]
    assert track["callsign"] == "Belgorod hit 12 Oct (confirmed)"
    assert (track["lat_deg"], track["lon_deg"]) == (50.5977, 36.5858)
    assert "TASS (EN)" in track["remarks"] and "CC BY 4.0" in track["remarks"]
    assert out["DSM-2026-10-12-UA-DNIPRO"][0].endswith("/strike/friendly/incident")


def test_a_place_in_russian_held_ukraine_is_a_hostile_marker_named_as_occupied():
    track = dsm.strike_tracks(DAY, NOW)["DSM-2026-10-12-RU-STAROBILSK"][1]
    assert "occupied" in track["callsign"] and "(reported)" in track["callsign"]


def test_countries_can_be_limited_and_a_place_without_a_position_is_skipped():
    assert all(uid.split("-")[4] == "RU" for uid in dsm.strike_tracks(DAY, NOW, {"RU"}))
    broken = {"date": "2026-10-12", "strikes": [{"country": "RU", "place": "X", "lat": None, "lon": 30}, {"country": "RU", "lat": 50, "lon": 30}]}
    assert dsm.strike_tracks(broken, NOW) == {}


def test_both_c2s_draw_a_marker_as_the_2525b_explosion_with_the_side_of_its_country():
    for uid, (prefix, track) in dsm.strike_tracks(DAY, NOW).items():
        topic = semantic_topic(prefix, track)
        hostile = "/hostile/" in topic
        assert _cot(topic) == (["a-h-G-I-E"] if hostile else ["a-f-G-I-E"]), topic
        assert _sidc(topic) == (["SHGPIE----*****"] if hostile else ["SFGPIE----*****"]), topic
        xml = tak_layer.track_to_cot(track, _cot(topic)[0], stale_s=tak_layer._track_stale_s(track, 120))
        assert xml and 'type="a-{}-G-I-E"'.format("h" if hostile else "f") in xml and track["callsign"] in xml


def test_a_russian_region_in_alert_becomes_a_red_zone_and_occupied_ukraine_or_a_stale_alert_does_not():
    index = region_geometry.region_index(_regions())
    zones = dsm.alert_zone_tracks(ALERTS, index, NOW)
    assert list(zones) == ["DSM-ALERT-RU-MOSCOW-OBLAST"]
    prefix, track = zones["DSM-ALERT-RU-MOSCOW-OBLAST"]
    assert prefix.endswith("/land/dronestrikemap/airzone/neutral/zone")
    assert track["shape_color"] == "red" and track["geometry"]["type"] == "Polygon"
    assert "not an official warning" in track["remarks"]
    assert _cot(semantic_topic(prefix, track)) == ["a-n-G-I-R"]       # drawn by the existing zone path


def test_an_alert_for_a_region_with_no_outline_is_left_out():
    assert dsm.alert_zone_tracks(ALERTS, region_geometry.region_index([]), NOW) == {}


def test_kyiv_days_are_counted_back_from_today():
    days = dsm._kyiv_days(time.mktime((2026, 10, 13, 12, 0, 0, 0, 0, 0)), 3)
    assert days == ["2026-10-13", "2026-10-12", "2026-10-11"]


def test_region_names_match_the_way_sources_spell_them():
    index = region_geometry.region_index([
        {"id": "a", "name": "Kaliningrad", "rings": []}, {"id": "b", "name": "Republic of Mordovia", "rings": []},
        {"id": "c", "name": "Moscow", "rings": []}, {"id": "d", "name": "Moscow Oblast", "rings": []}])
    match = region_geometry.match_region
    assert match(index, "Kaliningrad Oblast")["id"] == "a" and match(index, "Mordovia")["id"] == "b"
    assert match(index, "Moscow")["id"] == "c" and match(index, "Moscow Oblast")["id"] == "d"
    assert match(index, "Crimea (occupied)") is None


def test_regions_are_downloaded_once_cached_and_the_old_copy_serves_when_the_site_is_down(tmp_path):
    geojson = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"shapeName": "Brest", "shapeISO": "BY-BR"},
               "geometry": {"type": "Polygon", "coordinates": [_square(23, 52)]}}]}
    calls = []

    def read(url, timeout=60):
        calls.append(url)
        return json.dumps([{"simplifiedGeometryGeoJSON": "https://example.test/blr.geojson"}]).encode() if "api/current" in url \
            else json.dumps(geojson).encode()

    first = region_geometry.regions("BLR", str(tmp_path), read=read)
    assert [r["name"] for r in first] == ["Brest"] and len(calls) == 2
    region_geometry.regions("BLR", str(tmp_path), read=read)
    assert len(calls) == 2                                           # fresh cache: no request
    old = time.time() - 40 * 86400
    import os
    os.utime(tmp_path / "BLR-ADM1.geojson", (old, old))

    def down(url, timeout=60):
        raise OSError("no network")

    assert [r["name"] for r in region_geometry.regions("BLR", str(tmp_path), read=down)] == ["Brest"]
    assert region_geometry.regions("RUS", str(tmp_path), read=down) == []
