"""Focused tests for the Zenoh-native partner translation layers."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "protocols"))

from protocols.random.cap import parse_cap  # noqa: E402
from protocols.random.geojson_features import normalize as normalize_geojson  # noqa: E402
from protocols.random.mission_route import normalize as normalize_route  # noqa: E402
from protocols.random.sensor_health import normalize as normalize_health  # noqa: E402
from protocols.random.spectrum_observation import normalize as normalize_spectrum  # noqa: E402


class TranslationLayerTests(unittest.TestCase):
    def test_cap_circle_and_lifetime(self):
        xml = b"""<alert xmlns='urn:oasis:names:tc:emergency:cap:1.2'>
          <identifier>LT-TEST-1</identifier><status>Actual</status>
          <sent>2026-07-17T10:00:00Z</sent>
          <info><event>UAV restriction</event><severity>Severe</severity>
            <effective>2026-07-17T10:00:00Z</effective><expires>2026-07-18T10:00:00Z</expires>
            <area><areaDesc>test</areaDesc><circle>54.6872,25.2797 5</circle></area>
          </info></alert>"""
        records = parse_cap(xml, now=1784283000)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["cap_identifier"], "LT-TEST-1")
        self.assertEqual(records[0]["radius_km"], 5.0)

    def test_geojson_polygon_centroid(self):
        record = normalize_geojson({
            "type": "Feature", "id": "zone-1",
            "geometry": {"type": "Polygon", "coordinates": [[[24, 54], [25, 54], [25, 55], [24, 54]]]},
            "properties": {"name": "test zone"},
        }, now=100)
        self.assertEqual(record["uid"], "GEO-zone-1")
        self.assertEqual(record["geometry_type"], "Polygon")
        self.assertAlmostEqual(record["lat_deg"], 54.25, places=5)

    def test_other_normalizers_preserve_provenance(self):
        spectrum = normalize_spectrum({"uid": "r1", "frequency_hz": 433920000, "lat": 54, "lon": 25}, now=1)
        self.assertEqual(spectrum["source_kind"], "spectrum_observation")
        health = normalize_health({"sensor_id": "s1", "status": "OK", "lat": 54, "lon": 25}, now=1)
        self.assertEqual(health["health_status"], "ok")
        route = normalize_route({"id": "m1", "type": "LineString",
                                 "coordinates": [[25, 54], [26, 55]], "route_type": "corridor"}, now=1)
        self.assertEqual(route["uid"], "ROUTE-m1")


if __name__ == "__main__":
    unittest.main()
