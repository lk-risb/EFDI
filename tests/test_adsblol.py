#!/usr/bin/env python3
"""ADSB.lol bridge normalization tests."""

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))

from adsblol_bridge import normalize  # noqa: E402
from track_fusion_bridge import TrackFuser  # noqa: E402


class AdsbLolBridgeTests(unittest.TestCase):
    def test_normalizes_readsb_aircraft_record(self):
        track = normalize({
            "hex": "4CA123",
            "flight": "EIN42 ",
            "r": "EI-ABC",
            "t": "A320",
            "lat": 54.6872,
            "lon": 25.2797,
            "alt_baro": 12000,
            "alt_geom": 12375,
            "gs": 312.5,
            "track": 91.2,
            "baro_rate": -640,
            "squawk": "1234",
            "category": "A3",
            "dbFlags": 1,
            "seen_pos": 0.2,
            "rssi": -18.5,
        })

        self.assertIsNotNone(track)
        self.assertEqual(track["_src"], "adsblol")
        self.assertEqual(track["icao24"], "4ca123")
        self.assertEqual(track["callsign"], "EIN42")
        self.assertEqual(track["alt_baro_ft"], 12000)
        self.assertEqual(track["ground_speed_kts"], 312.5)
        self.assertEqual(track["track_deg"], 91.2)
        self.assertTrue(track["is_military"])

    def test_ground_and_invalid_positions(self):
        ground = normalize({"hex": "abc123", "lat": 55, "lon": 24, "alt_baro": "ground"})
        self.assertTrue(ground["on_ground"])
        self.assertNotIn("alt_baro_ft", ground)
        self.assertIsNone(normalize({"lat": 91, "lon": 24}))
        self.assertIsNone(normalize({"lat": "nan", "lon": 24}))

    def test_military_category_survives_fallback_and_radar_fusion(self):
        class Session:
            def __init__(self):
                self.publications = []

            def put(self, topic, payload, **_kwargs):
                self.publications.append((topic, payload))

        class Sample:
            def __init__(self, topic, payload):
                self.key_expr = topic
                self.payload = __import__("json").dumps(payload).encode()

        session = Session()
        fuser = TrackFuser(session, verbose=False)
        adsb = {
            "_src": "adsblol",
            "_ts": 1000,
            "icao24": "4ca123",
            "lat_deg": 54.68,
            "lon_deg": 25.27,
            "is_military": True,
        }
        fuser.on_adsb(Sample("test/air/adsblol/adsb/mil/aircraft/tracks/v1", adsb))
        self.assertIn("/air/fused/mil/", session.publications[-1][0])

        radar = {
            "_src": "ASTERIX CAT-48",
            "_ts": 1001,
            "icao24": "4ca123",
            "lat_deg": 54.681,
            "lon_deg": 25.271,
            "radar_id": "site-1",
        }
        fuser.on_radar(Sample("test/air/asterix/cat48/unknown/aircraft/tracks/v1", radar))
        self.assertIn("/air/fused/mil/", session.publications[-1][0])


if __name__ == "__main__":
    unittest.main()
