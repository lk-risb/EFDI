#!/usr/bin/env python3
"""DJI Cloud API MQTT aircraft OSD fixtures."""

import json
import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "compose"))
sys.path.insert(0, os.fspath(ROOT / "compose" / "bridges"))
sys.path.insert(0, os.fspath(ROOT / "compose" / "layers"))

from dji_cloud_api_bridge import decode_osd  # noqa: E402
from cot_layer import _TOPIC_COT  # noqa: E402
from nato_nvg_layer import _TOPIC_SIDC  # noqa: E402


class DjiCloudApiTests(unittest.TestCase):
    def test_aircraft_osd_normalizes_official_position_fields(self):
        message = {
            "timestamp": 1_752_745_200_123,
            "data": {
                "latitude": 54.6872,
                "longitude": 25.2797,
                "height": 182.4,
                "elevation": 63.2,
                "horizontal_speed": 14.5,
                "vertical_speed": -1.2,
                "attitude_head": -10,
                "mode_code": 3,
            },
        }
        track = decode_osd(
            "thing/product/1ZNBJ5D00C00AB/osd",
            json.dumps(message).encode(),
        )
        self.assertEqual(track["callsign"], "DJI-D00C00AB")
        self.assertEqual(track["geo_alt_m"], 182.4)
        self.assertEqual(track["height_m"], 63.2)
        self.assertEqual(track["heading_deg"], 350.0)
        self.assertEqual(track["_ts"], 1_752_745_200.123)
        self.assertEqual(_TOPIC_COT["air/**/friendly/uav/**"][0], "a-f-A-M-F-Q")
        self.assertEqual(_TOPIC_SIDC["air/**/friendly/uav/**"], "SFAPMFQ---*****")

    def test_dock_or_controller_location_is_not_mislabeled_aircraft(self):
        payload = json.dumps({"data": {"latitude": 54.7, "longitude": 25.3}}).encode()
        self.assertIsNone(decode_osd("thing/product/dock-sn/osd", payload))

    def test_invalid_topic_and_coordinates_are_rejected(self):
        payload = json.dumps(
            {"data": {"latitude": 95, "longitude": 25, "height": 50}}
        ).encode()
        self.assertIsNone(decode_osd("thing/product/drone/osd", payload))
        self.assertIsNone(decode_osd("other/topic", b"{}"))


if __name__ == "__main__":
    unittest.main()
