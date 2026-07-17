"""Tests for the authorized Oro navigacija UTM source bridge."""

import pathlib
import sys
import unittest
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))

from utm_ans_bridge import TOPIC_UAV, _records, normalize, run  # noqa: E402


class UtmAnsBridgeTests(unittest.TestCase):
    def test_rejects_plaintext_feed_urls(self):
        with self.assertRaises(SystemExit):
            run(SimpleNamespace(url="http://utm.example/feed", interval=15, token="", verify_tls=True, once=True))

    def test_normalizes_declared_flight_without_claiming_remote_id(self):
        track = normalize({
            "flightId": "LT-TEST-42",
            "position": {"latitude": 54.6872, "longitude": 25.2797, "altitude": 120},
            "speed_kts": 20,
            "course": 91,
            "flight_status": "active",
            "registration": "LY-UAV1",
        }, now=1_700_000_000)

        self.assertIsNotNone(track)
        self.assertEqual(track["_src"], "utm_ans")
        self.assertEqual(track["source_kind"], "declared_utm_flight")
        self.assertFalse(track["utm_remote_id_observed"])
        self.assertEqual(track["utm_flight_id"], "LT-TEST-42")
        self.assertEqual(track["speed_ms"], 10.29)
        self.assertEqual(track["heading_deg"], 91)
        self.assertIn("/air/utm_ans/utm/unknown/uav/tracks/v1", TOPIC_UAV)

    def test_extracts_geojson_feature_collection(self):
        records = _records({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [25.2797, 54.6872, 80]},
                "properties": {"id": "feature-1", "status": "planned"},
            }],
        })

        self.assertEqual(len(records), 1)
        track = normalize(records[0], now=1_700_000_000)
        self.assertIsNotNone(track)
        self.assertEqual(track["utm_flight_id"], "feature-1")
        self.assertEqual(track["utm_status"], "planned")

    def test_rejects_missing_or_invalid_position(self):
        self.assertIsNone(normalize({"id": "missing-position"}))
        self.assertIsNone(normalize({"id": "outside", "lat": 91, "lon": 25}))
        self.assertIsNone(normalize({"id": "nan", "lat": "nan", "lon": 25}))


if __name__ == "__main__":
    unittest.main()
