#!/usr/bin/env python3
"""Focused tests for secure TAK-user CoT ingress."""

import json
import pathlib
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "bridge"))
sys.path.insert(0, str(ROOT / "compose" / "bridge" / "layers"))

from cot_receiver_bridge import _parse_cot, _should_publish, _topic  # noqa: E402
from cot_layer import make_handler  # noqa: E402


TAK_USER_COT = """<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="ANDROID-123" type="a-f-G-U-C" how="m-g"
       time="2026-07-15T11:00:00Z" start="2026-07-15T11:00:00Z"
       stale="2026-07-15T11:02:00Z">
  <point lat="54.6872" lon="25.2797" hae="105.5" ce="4.2" le="7.5"/>
  <detail>
    <contact callsign="ALPHA-1" endpoint="*:-1:stcp"/>
    <track speed="1.5" course="270"/>
    <takv device="S21" platform="ATAK-CIV" os="Android 14" version="5.4"/>
    <__group name="Cyan" role="Team Lead"/>
    <status battery="87"/>
    <precisionlocation geopointsrc="GPS" altsrc="GPS"/>
    <remarks>Patrol lead</remarks>
  </detail>
</event>"""


class CotReceiverTests(unittest.TestCase):
    def test_tak_ground_user_metadata_and_topic(self):
        track = _parse_cot(TAK_USER_COT)

        self.assertIsNotNone(track)
        self.assertTrue(track["tak_user"])
        self.assertEqual(track["callsign"], "ALPHA-1")
        self.assertEqual(track["team"], "Cyan")
        self.assertEqual(track["role"], "Team Lead")
        self.assertEqual(track["tak_platform"], "ATAK-CIV")
        self.assertEqual(track["battery_pct"], 87)
        self.assertEqual(track["ce_m"], 4.2)
        self.assertIn("/land/radar/cot/friendly/unit/tracks/v1", _topic(track))
        self.assertTrue(_should_publish(track, tak_users_only=True))

    def test_tak_user_filter_rejects_non_user_map_items(self):
        marker = TAK_USER_COT.replace(
            '<takv device="S21" platform="ATAK-CIV" os="Android 14" version="5.4"/>',
            "",
        ).replace('<__group name="Cyan" role="Team Lead"/>', "")
        track = _parse_cot(marker)

        self.assertFalse(track["tak_user"])
        self.assertFalse(_should_publish(track, tak_users_only=True))
        self.assertTrue(_should_publish(track, tak_users_only=False))

    def test_efdi_round_trip_uid_is_always_rejected(self):
        track = _parse_cot(TAK_USER_COT.replace("ANDROID-123", "EFDI-UID-OWN"))

        self.assertFalse(_should_publish(track, tak_users_only=False))

    def test_tak_ingress_is_not_sent_back_to_tcp_server(self):
        class Sender:
            drop_tak_ingress = True

            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(message)

        class Sample:
            key_expr = "test/land/radar/cot/friendly/unit/tracks/v1"

            def __init__(self, track):
                self.payload = json.dumps(track).encode()

        track = _parse_cot(TAK_USER_COT)
        track["_ingress"] = "tak_server"
        sender = Sender()

        make_handler("a-f-G-U-C", sender, verbose=False)(Sample(track))

        self.assertEqual(sender.messages, [])

    def test_sitaware_vehicle_type_is_preserved_for_tak(self):
        class Sender:
            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(message)

        class Sample:
            key_expr = "test/land/radar/cot/friendly/unit/tracks/v1"

            def __init__(self, track):
                self.payload = json.dumps(track).encode()

        expected_type = "a-f-G-E-V-A-T"
        track = _parse_cot(TAK_USER_COT.replace("a-f-G-U-C", expected_type))
        track["_src"] = "sitaware_cot_rx"
        track["_ingress"] = "cot_source"
        sender = Sender()

        make_handler("a-f-G-U-C", sender, verbose=False)(Sample(track))

        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(ET.fromstring(sender.messages[0]).attrib["type"], expected_type)

    def test_adsb_surface_service_vehicle_is_not_fixed_wing(self):
        class Sender:
            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(message)

        class Sample:
            key_expr = "test/air/fused/adsb/civ/aircraft/tracks/v1"

            def __init__(self):
                self.payload = json.dumps({
                    "_src": "airplaneslive",
                    "_ts": 1_721_000_000,
                    "icao24": "abcdef",
                    "callsign": "OPS1",
                    "lat_deg": 54.6,
                    "lon_deg": 25.2,
                    "emitter_category_str": "C2",
                }).encode()

        sender = Sender()

        make_handler("a-n-A-C-F", sender, verbose=False)(Sample())

        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(ET.fromstring(sender.messages[0]).attrib["type"], "a-n-G-E-V")


if __name__ == "__main__":
    unittest.main()
