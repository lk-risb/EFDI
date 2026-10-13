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


def test_the_side_is_the_striker_a_place_hit_in_russia_is_a_friendly_incident_and_one_in_ukraine_a_hostile_one():
    out = dsm.strike_tracks(DAY, NOW)
    belgorod = out["DSM-2026-10-12-RU-BELGOROD"]
    assert belgorod[0].endswith("/land/dronestrikemap/strike/friendly/incident")
    track = belgorod[1]
    assert track["callsign"] == "Belgorod hit 12 Oct (confirmed)"
    assert (track["lat_deg"], track["lon_deg"]) == (50.5977, 36.5858)
    assert "TASS (EN)" in track["remarks"] and "CC BY 4.0" in track["remarks"]
    assert "Ukrainian strike" in track["remarks"]
    dnipro = out["DSM-2026-10-12-UA-DNIPRO"]
    assert dnipro[0].endswith("/strike/hostile/incident") and "Russian strike" in dnipro[1]["remarks"]


def test_the_remarks_quote_the_article_titles_so_a_misread_story_can_be_spotted():
    # the site marked Kaliningrad "confirmed" from two pieces about how NATO would respond to an attack
    track = dsm.strike_tracks(DAY, NOW)["DSM-2026-10-12-RU-KALININGRAD"][1]
    assert "How would NATO defeat" in track["remarks"] and "Meduza (EN)" in track["remarks"]


def test_a_strike_matching_the_drop_regex_is_left_out(monkeypatch):
    monkeypatch.setattr(dsm, "_DROP", re.compile(r"NATO defeat|Kaliningrad", re.I))
    out = dsm.strike_tracks(DAY, NOW)
    assert "DSM-2026-10-12-RU-KALININGRAD" not in out and "DSM-2026-10-12-RU-BELGOROD" in out


def test_a_place_in_russian_held_ukraine_is_a_ukrainian_strike_named_as_occupied():
    prefix, track = dsm.strike_tracks(DAY, NOW)["DSM-2026-10-12-RU-STAROBILSK"]
    assert prefix.endswith("/friendly/incident")
    assert "occupied" in track["callsign"] and "(reported)" in track["callsign"]


def test_countries_can_be_limited_and_a_place_without_a_position_is_skipped():
    assert all(uid.split("-")[4] == "RU" for uid in dsm.strike_tracks(DAY, NOW, {"RU"}))
    broken = {"date": "2026-10-12", "strikes": [{"country": "RU", "place": "X", "lat": None, "lon": 30}, {"country": "RU", "lat": 50, "lon": 30}]}
    assert dsm.strike_tracks(broken, NOW) == {}


def test_both_c2s_draw_a_marker_as_the_2525b_explosion_with_the_side_of_its_country():
    for uid, (prefix, track) in dsm.strike_tracks(DAY, NOW).items():
        topic = semantic_topic(prefix, track)
        hostile = "/hostile/" in topic
        assert hostile == (track["country"] == "UA"), topic       # a Russian strike lands in Ukraine
        assert _cot(topic) == (["a-h-G-I-E"] if hostile else ["a-f-G-I-E"]), topic
        assert _sidc(topic) == (["SHGPIE----*****"] if hostile else ["SFGPIE----*****"]), topic
        xml = tak_layer.track_to_cot(track, _cot(topic)[0], stale_s=tak_layer._track_stale_s(track, 120))
        assert xml and 'type="a-{}-G-I-E"'.format("h" if hostile else "f") in xml and track["callsign"] in xml


def _strikes(*regions_and_places):
    day = {"date": "2026-10-12", "strikes": [
        {"country": "RU", "place": place, "region": region, "lat": 50.0, "lon": 36.0, "confidence": "confirmed"}
        for region, place in regions_and_places]}
    return dsm.strike_tracks(day, NOW)


def test_a_russian_region_in_alert_is_red_one_with_a_place_hit_is_yellow_and_the_rest_are_not_drawn_here():
    index = region_geometry.region_index(_regions())
    strikes = _strikes(("Belgorod Oblast", "Belgorod"), ("Moscow Oblast", "Zhukovsky"))
    zones = dsm.region_zone_tracks(ALERTS, strikes, index, NOW)
    assert sorted(zones) == ["DSM-REGION-RU-BEL", "DSM-REGION-RU-MOS"]
    prefix, belgorod = zones["DSM-REGION-RU-BEL"]
    assert prefix.endswith("/land/dronestrikemap/airzone/neutral/zone")
    assert (belgorod["shape_color"], belgorod["callsign"]) == ("yellow", "RU Belgorod Oblast YELLOW")
    assert "Belgorod" in belgorod["remarks"] and "not an official warning" in belgorod["remarks"]
    moscow = zones["DSM-REGION-RU-MOS"][1]                      # the fixture has an active alert in Moscow Oblast
    assert moscow["shape_color"] == "red" and "Air-raid alert since" in moscow["remarks"] and "Zhukovsky" in moscow["remarks"]
    assert moscow["geometry"]["type"] == "Polygon"
    assert _cot(semantic_topic(prefix, belgorod)) == ["a-n-G-I-R"]       # drawn by the existing zone path


def test_occupied_ukraine_ukrainian_regions_and_stale_alerts_get_no_russian_zone():
    index = region_geometry.region_index(_regions())
    strikes = _strikes(("Luhansk Oblast (occupied)", "Starobilsk"))
    assert dsm.region_zone_tracks({"active": [a for a in ALERTS["active"] if a.get("stale") or a["country"] != "RU" or a["occupied"]]},
                                  strikes, index, NOW) == {}


def test_a_region_with_no_outline_is_left_out():
    assert dsm.region_zone_tracks(ALERTS, _strikes(("Belgorod Oblast", "Belgorod")), region_geometry.region_index([]), NOW) == {}


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


def test_the_countries_under_attack_are_those_with_a_place_hit_or_a_recent_strike_incident():
    strikes = dsm.strike_tracks(DAY, NOW)
    incidents = [{"country": "PL", "kind": "strike", "date": "2026-10-11"},
                 {"country": "RO", "kind": "shootdown", "date": "2026-10-12"},       # a drone shot down is not an attack
                 {"country": "LT", "kind": "strike", "date": "2026-09-01"}]         # too old
    assert dsm.war_countries(strikes, incidents, "2026-10-13") == ["PL", "RU", "UA"]
    assert dsm.war_countries({}, [], "2026-10-13") == []
