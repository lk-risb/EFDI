"""dangausakis air-alert zones: boundary parsing, simplification, zone tracks, TAK colours."""

import json
import math
import pathlib
import re
import sys
import time
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "layers" / "vendors" / "tak"))
sys.path.insert(0, str(ROOT / "compose" / "bridges" / "vendors" / "dangausakis"))

import dangausakis_bridge as bridge  # noqa: E402
import tak_alert_layer  # noqa: E402
import tak_layer  # noqa: E402

NOW = time.time()
SQUARE = [[25.0, 54.0], [26.0, 54.0], [26.0, 55.0], [25.0, 55.0], [25.0, 54.0]]
ISLET = [[27.0, 54.0], [27.01, 54.0], [27.01, 54.01], [27.0, 54.01], [27.0, 54.0]]


def _circle(n=800, r=1.0, cx=25.0, cy=54.0):
    pts = [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)] for i in range(n)]
    return pts + [pts[0]]


def _bundle(*features):
    collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": fid, "name": name, "country": fid.split(":")[0]},
         "geometry": geom} for fid, name, geom in features]}
    return "window.daMapAssetData=window.daMapAssetData||{};\nwindow.daMapAssetData.adminGeoData=" + json.dumps(collection) + ";"


def _alert(alert_id, level="red", stale=False):
    return {"alert_id": alert_id, "country": alert_id.split(":")[0], "level": level,
            "since": "2026-10-05T10:00:00Z", "source": "LT72", "stale": stale}


def test_parse_boundaries_handles_polygon_and_multipolygon():
    js = _bundle(("LT:LT-VL", "Vilniaus", {"type": "Polygon", "coordinates": [SQUARE]}),
                 ("EE:0130", "Alutaguse", {"type": "MultiPolygon", "coordinates": [[SQUARE], [ISLET]]}))
    b = bridge.parse_boundaries(js)
    assert b["LT:LT-VL"]["name"] == "Vilniaus" and len(b["LT:LT-VL"]["rings"]) == 1
    assert len(b["EE:0130"]["rings"]) == 2


def test_simplify_ring_caps_vertices_stays_closed_and_keeps_shape():
    ring = bridge.simplify_ring(_circle(), max_vertices=60)
    assert len(ring) <= 60 and ring[0] == ring[-1]
    assert all(abs(math.hypot(x - 25.0, y - 54.0) - 1.0) < 0.05 for x, y in ring)
    assert bridge.simplify_ring(SQUARE) == SQUARE        # already small: untouched


def test_zone_track_fields():
    b = bridge.parse_boundaries(_bundle(("LT:LT-VL", "Vilniaus apskritis", {"type": "Polygon", "coordinates": [SQUARE]})))
    (uid, (prefix, track)), = bridge.zone_tracks({"LT:LT-VL": _alert("LT:LT-VL")}, b, NOW).items()
    assert uid == "DA-ZONE-LT:LT-VL" and prefix.endswith("/land/dangausakis/airzone/neutral/zone")
    assert track["shape_color"] == "red" and track["callsign"] == "LT Vilniaus apskritis RED"
    assert track["geometry"]["type"] == "Polygon" and abs(track["lat_deg"] - 54.4) < 0.3
    assert "not an official warning" in track["remarks"]


def test_skips_unknown_region_stale_source_and_uncoloured_level():
    b = bridge.parse_boundaries(_bundle(("LT:LT-VL", "V", {"type": "Polygon", "coordinates": [SQUARE]})))
    alerts = {"LT:LT-XX": _alert("LT:LT-XX"), "LT:LT-VL": _alert("LT:LT-VL", stale=True)}
    assert bridge.zone_tracks(alerts, b, NOW) == {}
    assert bridge.zone_tracks({"LT:LT-VL": _alert("LT:LT-VL", level="green")}, b, NOW) == {}
    assert len(bridge.zone_tracks({"LT:LT-VL": _alert("LT:LT-VL", level="yellow")}, b, NOW)) == 1


def test_multipart_regions_split_and_islets_are_dropped():
    big2 = [[x + 5, y] for x, y in SQUARE]
    b = bridge.parse_boundaries(_bundle(
        ("EE:0130", "A", {"type": "MultiPolygon", "coordinates": [[SQUARE], [ISLET], [big2]]})))
    out = bridge.zone_tracks({"EE:0130": _alert("EE:0130")}, b, NOW)
    assert sorted(out) == ["DA-ZONE-EE:0130", "DA-ZONE-EE:0130-p1"]      # two real parts, islet dropped


def test_cot_zone_has_polygon_and_colours_only_when_requested():
    b = bridge.parse_boundaries(_bundle(("PL:slaskie", "Silezijos", {"type": "Polygon", "coordinates": [SQUARE]})))
    (_, (_, track)), = bridge.zone_tracks({"PL:slaskie": _alert("PL:slaskie", level="orange")}, b, NOW).items()
    xml = tak_layer.track_to_cot(track, "a-n-G-I-R")
    assert "<polygon>" not in xml                       # drawn from the links; a second copy of every vertex doubles the size
    stroke = int(re.search(r'strokeColor value="(-?\d+)"', xml).group(1)) & 0xFFFFFFFF
    assert stroke == 0xFFFF8C00
    assert "fillColor" in xml and "strokeWeight" in xml
    assert 'type="u-d-f"' in xml                        # WinTAK/ATAK draw only drawing-tool events
    assert xml.count("<link point=") == len(SQUARE) + (SQUARE[0] != SQUARE[-1])
    plain = dict(track)
    del plain["shape_color"]
    plain_xml = tak_layer.track_to_cot(plain, "a-n-G-I-R")
    assert "strokeColor" not in plain_xml and 'type="u-d-f"' not in plain_xml and "<polygon>" in plain_xml


def test_layers_route_zone_topic_and_alert_layer_ignores_zone_tracks():
    assert '"land/**/neutral/zone/**"' in (ROOT / "compose/layers/vendors/tak/tak_layer.py").read_text()
    assert '"land/**/neutral/zone/**"' in (ROOT / "compose/layers/vendors/tak/systematic/sitaware_layer.py").read_text()
    sender = mock.Mock()
    handler = tak_alert_layer.make_region_alert_handler(sender, verbose=False)
    sample = mock.Mock(payload=json.dumps({"uid": "DA-ZONE-LT:LT-VL", "alert_id": "LT:LT-VL", "level": "red"}).encode())
    handler(sample)
    sender.send.assert_not_called()


def test_boundaries_from_geojson_matches_bundle_parsing():
    features = [("LT:LT-VL", "Vilniaus", {"type": "Polygon", "coordinates": [SQUARE]})]
    js = _bundle(*features)
    data = json.loads(js[js.index("adminGeoData=") + len("adminGeoData="):].rstrip(";"))
    assert bridge.boundaries_from_geojson(data) == bridge.parse_boundaries(js)


def test_load_boundaries_prefers_rest_route_then_falls_back_to_bundle(monkeypatch):
    calls = []
    rest_ok = {"value": True}
    geojson = json.loads(_bundle(("LT:LT-VL", "V", {"type": "Polygon", "coordinates": [SQUARE]}))
                         .split("adminGeoData=")[1].rstrip(";"))

    class Resp:
        def __init__(self, body): self.body = body.encode()
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if req.full_url.endswith("/region-geometry"):
            if rest_ok["value"]:
                return Resp(json.dumps(geojson))
            raise bridge.urllib.error.URLError("down")
        if req.full_url == bridge.MAP_PAGE:
            return Resp('<script src="/wp-content/plugins/dangaus-akis-map-assets/assets/adminGeoData.abc123.js">')
        return Resp(_bundle(("LT:LT-VL", "V", {"type": "Polygon", "coordinates": [SQUARE]})))

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    assert "LT:LT-VL" in bridge.load_site_boundaries() and calls == [bridge.API + "region-geometry"]
    calls.clear()
    rest_ok["value"] = False
    assert "LT:LT-VL" in bridge.load_site_boundaries()
    assert calls[0].endswith("/region-geometry") and calls[1] == bridge.MAP_PAGE and "adminGeoData.abc123.js" in calls[2]


def _region(name, rings):
    return {"name": name, "rings": rings}


def test_oblast_wash_uses_the_worst_district_level_defaults_to_yellow_and_skips_oblasts_with_their_own_alert():
    big = [[x, y] for x, y in [(30, 50), (34, 50), (34, 54), (30, 54), (30, 50)]]
    bounds = {"UA-O:a": _region("Alpha Oblast", [big]), "UA-O:b": _region("Beta Oblast", [big]),
              "UA-O:c": _region("Gamma Oblast", [big])}
    current = {
        "UA-R:1": dict(_alert("UA-R:1", level="yellow"), country="UA", oblast="Alpha Oblast"),
        "UA-R:2": dict(_alert("UA-R:2", level="red"), country="UA", oblast="Alpha Oblast"),
        "UA-R:3": dict(_alert("UA-R:3", level="yellow"), country="UA", oblast="Beta Oblast"),
        "UA-O:b": dict(_alert("UA-O:b", level="red"), country="UA"),            # Beta has its own full zone
        "UA-R:4": dict(_alert("UA-R:4", level="red", stale=True), country="UA", oblast="Gamma Oblast"),
    }
    out = bridge.wash_tracks(current, bounds, {}, NOW)
    # Alpha: worst district. Beta: has its own full zone. Gamma: only a stale alert, so the yellow default.
    assert sorted(out) == ["DA-WASH-UA-O:a", "DA-WASH-UA-O:c"]
    assert out["DA-WASH-UA-O:c"][1]["shape_color"] == "yellow" and out["DA-WASH-UA-O:c"][1]["callsign"] == "UA Gamma Oblast YELLOW"
    track = out["DA-WASH-UA-O:a"][1]
    assert track["shape_color"] == "red" and track["shape_style"] == "wash"
    assert track["callsign"] == "UA Alpha Oblast RED" and track["geometry"]["type"] == "Polygon"


def test_standing_country_zones_keep_the_mainland_and_kaliningrad_but_not_far_islands():
    mainland = [[30, 45], [60, 45], [60, 60], [30, 60], [30, 45]]
    kaliningrad = [[20, 54], [23, 54], [23, 55.5], [20, 55.5], [20, 54]]
    arctic = [[55, 70], [60, 70], [60, 74], [55, 74], [55, 70]]
    tiny = [[25, 50], [25.1, 50], [25.1, 50.1], [25, 50.1], [25, 50]]
    standing = {"RU": {"name": "Russia", "tone": "danger", "rings": [tiny, arctic, kaliningrad, mainland]},
                "BY": {"name": "Belarus", "tone": "warning", "rings": [[[24, 52], [32, 52], [32, 56], [24, 56], [24, 52]]]}}
    out = bridge.wash_tracks({}, {}, standing, NOW)
    assert sorted(out) == ["DA-COUNTRY-BY", "DA-COUNTRY-RU", "DA-COUNTRY-RU-p1"]
    assert out["DA-COUNTRY-RU"][1]["shape_color"] == "red" and out["DA-COUNTRY-BY"][1]["shape_color"] == "orange"
    assert out["DA-COUNTRY-RU"][1]["callsign"] == "RU Russia (danger)"
    assert out["DA-COUNTRY-RU"][1]["shape_style"] == "country"
    assert all(t["shape_style"] == "country" for _, t in out.values())


def test_load_standing_zones_reads_tone_countries_from_the_geodata_bundle():
    page = '<script src="/wp-content/plugins/dangaus-akis-map-assets/assets/geoData.abc123.js"></script>'
    sq = [[[20, 50], [21, 50], [21, 51], [20, 51], [20, 50]]]
    bundle = "window.daMapAssetData.geoData=" + json.dumps({"type": "FeatureCollection", "features": [
        {"properties": {"name": "Lietuva", "code": "LT", "tone": "monitored"}, "geometry": {"type": "Polygon", "coordinates": sq}},
        {"properties": {"name": "Rusija", "code": "RU", "tone": "danger"}, "geometry": {"type": "Polygon", "coordinates": sq}}]}) + ";"
    with mock.patch.object(bridge, "_get", side_effect=lambda url, timeout=15: bundle if url.endswith("abc123.js") else page):
        got = bridge.load_standing_zones()
    assert list(got) == ["RU"] and got["RU"]["name"] == "Russia" and got["RU"]["tone"] == "danger"
    with mock.patch.object(bridge, "_get", return_value=None):
        assert bridge.load_standing_zones() == {}


def test_wash_zone_is_a_faint_fill_and_drawn_shapes_are_human_entered_with_a_colour_element():
    b = bridge.parse_boundaries(_bundle(("PL:slaskie", "Silezijos", {"type": "Polygon", "coordinates": [SQUARE]})))
    (_, (_, track)), = bridge.zone_tracks({"PL:slaskie": _alert("PL:slaskie", level="red")}, b, NOW).items()
    solid = tak_layer.track_to_cot(track, "a-n-G-I-R")
    wash = tak_layer.track_to_cot(dict(track, shape_style="wash"), "a-n-G-I-R")
    assert 'how="h-e"' in solid and re.search(r'<color value="-?\d+"', solid)
    alpha = lambda xml, tag: (int(re.search(tag + r' value="(-?\d+)"', xml).group(1)) & 0xFFFFFFFF) >> 24
    assert alpha(wash, "fillColor") == alpha(solid, "fillColor") == 0x33      # a red fill is 20% opaque
    assert alpha(wash, "strokeColor") < 0xFF
    assert 'strokeWeight value="1.0"' in wash and 'strokeWeight value="3.0"' in solid


def test_country_style_has_the_same_fill_but_an_outline_between_a_wash_and_a_district_zone():
    b = bridge.parse_boundaries(_bundle(("PL:slaskie", "Silezijos", {"type": "Polygon", "coordinates": [SQUARE]})))
    (_, (_, track)), = bridge.zone_tracks({"PL:slaskie": _alert("PL:slaskie", level="red")}, b, NOW).items()
    alpha = lambda xml, tag: (int(re.search(tag + r' value="(-?\d+)"', xml).group(1)) & 0xFFFFFFFF) >> 24
    wash = tak_layer.track_to_cot(dict(track, shape_style="wash"), "a-n-G-I-R")
    country = tak_layer.track_to_cot(dict(track, shape_style="country"), "a-n-G-I-R")
    zone = tak_layer.track_to_cot(track, "a-n-G-I-R")
    assert alpha(wash, "fillColor") == alpha(country, "fillColor") == alpha(zone, "fillColor") == 0x33
    assert alpha(wash, "strokeColor") < alpha(country, "strokeColor") < alpha(zone, "strokeColor")
    assert 'strokeWeight value="2.0"' in country


def test_country_zones_keep_more_vertices_than_district_zones():
    import math
    import random
    rng = random.Random(7)                       # a ragged coastline: the point count falls smoothly with tolerance
    ring = [[30 + (10 + rng.uniform(-1.5, 1.5)) * math.cos(i / 3000 * 2 * math.pi),
             50 + (8 + rng.uniform(-1.5, 1.5)) * math.sin(i / 3000 * 2 * math.pi)] for i in range(3000)]
    ring.append(ring[0])
    out = bridge.wash_tracks({}, {}, {"RU": {"name": "Russia", "tone": "danger", "rings": [ring]}}, NOW)
    n = len(out["DA-COUNTRY-RU"][1]["geometry"]["coordinates"][0])
    district = len(bridge.simplify_ring(ring))
    assert district <= bridge.ZONE_MAX_VERTICES < bridge.COUNTRY_MAX_VERTICES <= tak_layer.MAX_SHAPE_POINTS
    assert district <= n == len(bridge.simplify_ring(ring, bridge.COUNTRY_MAX_VERTICES)) <= bridge.COUNTRY_MAX_VERTICES


def test_zones_ask_for_a_long_stale_time_and_tak_layer_honours_a_sane_one_only():
    b = bridge.parse_boundaries(_bundle(("PL:slaskie", "Silezijos", {"type": "Polygon", "coordinates": [SQUARE]})))
    (_, (_, track)), = bridge.zone_tracks({"PL:slaskie": _alert("PL:slaskie", level="red")}, b, NOW).items()
    assert track["stale_s"] == bridge.ZONE_STALE_S == 1800 and bridge.ZONE_REFRESH_S < bridge.ZONE_STALE_S
    assert tak_layer._track_stale_s(track, 120) == 1800
    for bad in (None, "x", 0, -5, 99999, float("nan")):
        assert tak_layer._track_stale_s({"stale_s": bad}, 120) == 120


def test_a_zone_with_many_points_is_drawn_whole_by_tak_layer():
    import math
    ring = [[30 + math.cos(i / 700 * 6.2832), 50 + math.sin(i / 700 * 6.2832)] for i in range(700)]
    ring.append(ring[0])
    xml = tak_layer.track_to_cot({"uid": "z", "_ts": NOW, "lat_deg": 50, "lon_deg": 30, "callsign": "z",
                                  "geometry": {"type": "Polygon", "coordinates": [ring]}, "shape_color": "red"},
                                 "a-n-G-I-R")
    assert xml.count("<link point=") == 701          # closed ring, nothing cut at the old 256 limit


def test_other_countries_regions_get_a_faint_white_wash_unless_they_have_an_alert_zone():
    ring = [[24.0, 54.0], [25.0, 54.0], [25.0, 55.0], [24.0, 55.0], [24.0, 54.0]]
    bounds = {"LT:LT-VL": _region("Vilnius County", [ring]), "LT:LT-KU": _region("Kaunas County", [ring]),
              "PL:slaskie": _region("Silesia", [ring]), "UA-O:a": _region("Alpha Oblast", [ring])}
    current = {"LT:LT-KU": _alert("LT:LT-KU", level="red")}                       # Kaunas has its own red zone
    out = bridge.wash_tracks(current, bounds, {}, NOW)
    assert sorted(out) == ["DA-WASH-LT:LT-VL", "DA-WASH-PL:slaskie", "DA-WASH-UA-O:a"]
    white = out["DA-WASH-LT:LT-VL"][1]
    assert white["shape_color"] == "white" and white["shape_style"] == "wash" and white["country"] == "LT"
    assert white["callsign"] == "LT Vilnius County (monitored)"
    assert out["DA-WASH-UA-O:a"][1]["shape_color"] == "yellow"                     # Ukraine keeps its yellow default


def test_fill_opacity_rises_with_the_level_in_tak_and_sitaware():
    import re
    ring = [[24.0, 54.0], [25.0, 54.0], [25.0, 55.0], [24.0, 54.0]]
    def fill_alpha(color, style):
        track = {"uid": "Z-" + color, "callsign": "z", "lat_deg": 54.3, "lon_deg": 24.3, "shape_color": color, "shape_style": style,
                 "geometry": {"type": "Polygon", "coordinates": [ring]}}
        xml = tak_layer.track_to_cot(track, "a-n-G-I-R")
        argb = int(re.search(r'<fillColor value="(-?\d+)"', xml).group(1)) & 0xFFFFFFFF
        return argb >> 24
    assert [fill_alpha(c, "wash") for c in ("white", "yellow", "orange", "red")] == [0x1A, 0x26, 0x26, 0x33]
    assert fill_alpha("red", None) == 0x33 and fill_alpha("yellow", None) == 0x26


def test_a_zone_too_big_for_one_cot_event_is_simplified_to_fit():
    import math
    n = 12000
    ring = [[24 + math.cos(i * 2 * math.pi / n) * (1 + 0.1 * math.sin(i * 0.37)), 54 + math.sin(i * 2 * math.pi / n)] for i in range(n)]
    ring.append(ring[0])
    track = {"uid": "DA-COUNTRY-XX", "callsign": "XX", "lat_deg": 54.0, "lon_deg": 24.0, "shape_color": "red", "shape_style": "country",
             "geometry": {"type": "Polygon", "coordinates": [ring]}}
    (xml,) = tak_layer.shape_cots(track, "a-n-G-I-R")
    assert 30000 < len(xml.encode()) < tak_layer.MAX_COT_BYTES < 65536 and 500 < xml.count("<link point=") < n
    small = {**track, "geometry": {"type": "Polygon", "coordinates": [[[24, 54], [25, 54], [25, 55], [24, 54]]]}}
    assert tak_layer.shape_cots(small, "a-n-G-I-R") == [tak_layer.track_to_cot(small, "a-n-G-I-R")]
