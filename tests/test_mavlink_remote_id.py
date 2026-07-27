#!/usr/bin/env python3
"""MAVLink Open Drone ID interoperability tests."""

from __future__ import annotations

import os
from pathlib import Path
import struct
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
sys.path.insert(0, os.fspath(COMPOSE))
sys.path.insert(0, os.fspath(COMPOSE / "protocols"))
sys.path.insert(0, os.fspath(COMPOSE / "layers"))

from protocols.random import mavlink as mav  # noqa: E402
from cot_layer import _unknown_air_type  # noqa: E402
from nvg_layer import _unknown_air_sidc  # noqa: E402


IDENTITY = bytes(range(1, 21))


def mavlink_v2(msgid: int, payload: bytes, sysid: int = 42) -> bytes:
    header = bytes(
        (
            len(payload),
            0,
            0,
            7,
            sysid,
            1,
            msgid & 0xFF,
            (msgid >> 8) & 0xFF,
            (msgid >> 16) & 0xFF,
        )
    )
    crc = mav._crc_msg(header + payload, mav._CRC_EXTRA[msgid])
    return bytes((mav.MAV_STX_V2,)) + header + payload + struct.pack("<H", crc)


def basic_payload() -> bytes:
    payload = bytearray(44)
    payload[2:22] = IDENTITY
    payload[22] = 1
    payload[23] = 2
    payload[24:44] = b"LT-REMOTE-123\x00\x00\x00\x00\x00\x00\x00"
    return bytes(payload)


def location_payload() -> bytes:
    payload = bytearray(59)
    struct.pack_into("<ii", payload, 0, int(54.6872e7), int(25.2797e7))
    struct.pack_into("<ffff", payload, 8, 120.0, 145.5, 80.0, 1234.5)
    struct.pack_into("<HHh", payload, 24, 9125, 1530, -125)
    payload[30] = 0
    payload[31] = 0
    payload[32:52] = IDENTITY
    payload[52] = 2
    payload[53] = 1
    payload[54:59] = bytes((11, 4, 4, 3, 2))
    return bytes(payload)


class FakeSession:
    def __init__(self):
        self.messages = []

    def put(self, topic, payload, encoding=None):
        self.messages.append((topic, payload, encoding))


class MAVLinkRemoteIDTests(unittest.TestCase):
    def test_parser_validates_official_remote_id_crc_extras(self):
        parser = mav._MAVParser()
        messages = parser.feed(
            mavlink_v2(mav.MSG_ODID_BASIC_ID, basic_payload())
            + mavlink_v2(mav.MSG_ODID_LOCATION, location_payload())
        )
        self.assertEqual([message[1] for message in messages], [12900, 12901])

    def test_remote_id_family_aggregates_and_publishes_map_track(self):
        parser = mav._MAVParser()
        messages = parser.feed(
            mavlink_v2(mav.MSG_ODID_BASIC_ID, basic_payload())
            + mavlink_v2(mav.MSG_ODID_LOCATION, location_payload())
        )
        session = FakeSession()
        remote_ids = {}
        mav._dispatch(messages, {}, remote_ids, session, False)
        # Tiered publish: JSON on /v1, per-protocol protobuf on /v2, and the
        # SAPIENT interop view on /sapient.
        by_topic = {topic: payload for topic, payload, _e in session.messages}
        _views = ("/proto/tracks/v1", "/sapient/tracks/v1", "/raw/tracks/v1")
        topic = next(t for t in by_topic
                     if t.endswith("/tracks/v1") and not t.endswith(_views))
        payload = by_topic[topic]
        base = topic[: -len("/tracks/v1")]
        self.assertIn(base + "/proto/tracks/v1", by_topic)
        self.assertIn(base + "/sapient/tracks/v1", by_topic)
        self.assertGreater(len(by_topic[base + "/proto/tracks/v1"]), 0)
        track = __import__("json").loads(payload)
        self.assertIn("/air/mavlink/telemetry/unknown/uav/", topic)  # {type}/{id} follow
        self.assertEqual(track["callsign"], "LT-REMOTE-123")
        self.assertEqual(track["remote_id_ua_type"], "uav helicopter or multirotor")
        self.assertEqual(track["remote_id_status"], "airborne")
        self.assertAlmostEqual(track["speed_ms"], 15.3)
        self.assertAlmostEqual(track["vertical_rate_ms"], -1.25)
        self.assertAlmostEqual(track["heading_deg"], 91.25)
        self.assertAlmostEqual(track["geo_alt_m"], 145.5)
        self.assertEqual(_unknown_air_type(track), "a-u-A-M-F-Q")
        self.assertEqual(_unknown_air_sidc(track), "SUAPMFQ---*****")

    def test_invalid_remote_id_position_is_not_published(self):
        payload = bytearray(location_payload())
        struct.pack_into("<ii", payload, 0, 0, 0)
        session = FakeSession()
        mav._dispatch(
            [(42, mav.MSG_ODID_LOCATION, bytes(payload))], {}, {}, session, False
        )
        self.assertEqual(session.messages, [])


if __name__ == "__main__":
    unittest.main()
