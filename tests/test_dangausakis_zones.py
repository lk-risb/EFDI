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
    assert "<polygon>" in xml
    stroke = int(re.search(r'strokeColor value="(-?\d+)"', xml).group(1)) & 0xFFFFFFFF
    assert stroke == 0xFFFF8C00
    assert "fillColor" in xml and "strokeWeight" in xml
    plain = dict(track)
    del plain["shape_color"]
    assert "strokeColor" not in tak_layer.track_to_cot(plain, "a-n-G-I-R")


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
