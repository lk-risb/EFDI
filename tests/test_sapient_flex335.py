#!/usr/bin/env python3
"""Focused tests for the public BSI Flex 335 v2 SAPIENT adapter."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import struct
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "compose" / "protocols"))
sys.path.insert(0, os.fspath(ROOT / "compose"))

import json  # noqa: E402

import zenoh  # noqa: E402

from sapient_flex335 import (  # noqa: E402
    SapientDecoder,
    _publish,
    iter_frames,
    registration_ack,
    topic_for_frame,
    topic_for_track,
)


NODE_ID = "11111111-2222-3333-4444-555555555555"
BRIDGE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field_bytes(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def field_text(number: int, value: str) -> bytes:
    return field_bytes(number, value.encode())


def field_double(number: int, value: float) -> bytes:
    return varint((number << 3) | 1) + struct.pack("<d", value)


def field_float(number: int, value: float) -> bytes:
    return varint((number << 3) | 5) + struct.pack("<f", value)


def envelope(content_number: int, content: bytes, seconds: int = 1_700_000_000) -> bytes:
    timestamp = field_varint(1, seconds) + field_varint(2, 250_000_000)
    return field_bytes(1, timestamp) + field_text(2, NODE_ID) + field_bytes(content_number, content)


def location(lat: float, lon: float, altitude: float = 100.0) -> bytes:
    return b"".join(
        (
            field_double(1, lon),
            field_double(2, lat),
            field_double(3, altitude),
            field_varint(7, 1),
            field_varint(8, 1),
        )
    )


def registration() -> bytes:
    node_definition = field_varint(1, 6)
    enu_units = field_varint(1, 2) + field_varint(2, 1)
    velocity_type = field_bytes(4, enu_units)
    detection_definition = field_bytes(6, velocity_type)
    mode_definition = field_bytes(10, detection_definition)
    return b"".join(
        (
            field_bytes(1, node_definition),
            field_text(2, "BSI_Flex_335_v2.0"),
            field_text(4, "Acoustic North"),
            field_bytes(7, mode_definition),
        )
    )


class SapientDecoderTests(unittest.TestCase):
    def setUp(self):
        self.decoder = SapientDecoder()
        self.decoder.decode(envelope(4, registration()))

    def test_registration_tracks_node_type_and_velocity_units(self):
        node = self.decoder.nodes[NODE_ID]
        self.assertEqual(node.name, "Acoustic North")
        self.assertEqual(node.node_types, ["acoustic"])
        self.assertEqual(node.horizontal_speed_units, 2)
        self.assertEqual(node.vertical_speed_units, 1)

    def test_absolute_detection_normalizes_for_tak_and_sitaware(self):
        classification = field_text(1, "UAV") + field_float(2, 0.92)
        velocity = field_double(1, 36.0) + field_double(2, 0.0) + field_double(3, 2.5)
        signal = field_float(1, -21.5) + field_float(3, 2_450_000_000.0)
        detection = b"".join(
            (
                field_text(1, "01HREPORT"),
                field_text(2, "01HOBJECT"),
                field_bytes(6, location(54.6872, 25.2797, 145.0)),
                field_float(7, 0.97),
                field_bytes(11, classification),
                field_bytes(14, signal),
                field_bytes(19, velocity),
                field_text(23, "RID-DRONE-7"),
            )
        )
        event = self.decoder.decode(envelope(7, detection))
        self.assertIsNone(event.warning)
        self.assertEqual(event.track["sapient_class"], "uav")
        self.assertEqual(event.track["callsign"], "RID-DRONE-7")
        self.assertAlmostEqual(event.track["speed_ms"], 10.0, places=3)
        self.assertAlmostEqual(event.track["heading_deg"], 90.0, places=2)
        self.assertEqual(event.track["vertical_rate_ms"], 2.5)
        self.assertEqual(event.track["sapient_signals"][0]["amplitude"], -21.5)
        self.assertEqual(
            topic_for_track("LTU/CISB/partner", event.track),
            "LTU/CISB/partner/air/sapient/flex335/unknown/uav/tracks/v1",
        )

    def test_range_bearing_uses_status_sensor_location(self):
        status = (
            field_text(1, "01HSTATUS")
            + field_varint(2, 1)
            + field_bytes(7, location(54.0, 25.0, 120.0))
        )
        status_event = self.decoder.decode(envelope(6, status))
        self.assertEqual(status_event.track["sensor_status"], "ok")
        self.assertEqual(
            topic_for_track("root/pod", status_event.track),
            "root/pod/land/sapient/flex335/neutral/sensor/status/v1",
        )

        range_bearing = (
            field_double(1, 0.0)
            + field_double(2, 90.0)
            + field_double(3, 1000.0)
            + field_varint(7, 1)
            + field_varint(8, 1)
        )
        detection = (
            field_text(1, "01HRANGE")
            + field_text(2, "01HRELATIVE")
            + field_bytes(5, range_bearing)
        )
        event = self.decoder.decode(envelope(7, detection))
        self.assertAlmostEqual(event.track["lat_deg"], 54.0, places=4)
        self.assertGreater(event.track["lon_deg"], 25.0)
        self.assertEqual(event.track["sapient_range_m"], 1000.0)

    def test_lost_detection_reuses_last_position_and_deletes(self):
        first = (
            field_text(1, "01HFIRST")
            + field_text(2, "01HLOST")
            + field_bytes(6, location(54.5, 25.5))
        )
        self.decoder.decode(envelope(7, first))
        lost = field_text(1, "01HLAST") + field_text(2, "01HLOST") + field_text(4, "lost")
        event = self.decoder.decode(envelope(7, lost))
        self.assertTrue(event.track["_delete"])
        self.assertEqual(event.track["lat_deg"], 54.5)
        self.assertEqual(event.track["lon_deg"], 25.5)

    def test_range_bearing_without_status_is_not_fabricated(self):
        decoder = SapientDecoder()
        range_bearing = (
            field_double(2, 90.0)
            + field_double(3, 1000.0)
            + field_varint(7, 1)
            + field_varint(8, 1)
        )
        detection = field_text(2, "01HOBJECT") + field_bytes(5, range_bearing)
        event = decoder.decode(envelope(7, detection))
        self.assertIsNone(event.track)
        self.assertIn("before sensor location", event.warning)


class SapientFramingTests(unittest.TestCase):
    def test_flex_335_uses_little_endian_frame_length(self):
        left, right = socket.socketpair()
        try:
            payload = envelope(4, registration())
            left.sendall(struct.pack("<I", len(payload)) + payload)
            self.assertEqual(next(iter_frames(right)), payload)
        finally:
            left.close()
            right.close()

    def test_registration_ack_is_a_decodable_framed_message(self):
        framed = registration_ack(BRIDGE_ID, NODE_ID)
        length = struct.unpack("<I", framed[:4])[0]
        self.assertEqual(length, len(framed) - 4)
        event = SapientDecoder().decode(framed[4:])
        self.assertEqual(event.kind, "registration_ack")
        self.assertEqual(event.node_id, BRIDGE_ID)

    def test_rejects_truncated_protobuf(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            SapientDecoder().decode(b"\x3a\x05abc")


class RecordingSession:
    """Minimal stand-in for a Zenoh session that records published samples."""

    def __init__(self):
        self.puts = []

    def put(self, topic, payload, encoding=None):
        self.puts.append((topic, payload, encoding))


class NativeProtobufEgressTests(unittest.TestCase):
    """Fabric egress must carry the original BSI Flex 335 v2 bytes, with the
    flattened JSON view published alongside during the transition."""

    def test_topic_for_frame_pairs_v1_with_v2(self):
        self.assertEqual(topic_for_frame("root/air/tracks/v1"), "root/air/tracks/v2")
        self.assertEqual(
            topic_for_frame("root/land/sapient/flex335/neutral/sensor/status/v1"),
            "root/land/sapient/flex335/neutral/sensor/status/v2",
        )
        # No /v1 suffix to swap — still gets a distinct protobuf topic.
        self.assertEqual(topic_for_frame("root/other"), "root/other/v2")

    def test_publish_emits_json_and_verbatim_protobuf(self):
        detection = b"".join(
            (
                field_text(1, "01HREPORT"),
                field_text(2, "01HOBJECT"),
                field_bytes(6, location(54.6872, 25.2797, 145.0)),
                field_float(7, 0.97),
                field_text(23, "RID-DRONE-7"),
            )
        )
        frame = envelope(7, detection)
        session = RecordingSession()
        _publish(session, SapientDecoder(), frame, verbose=False)

        self.assertEqual(len(session.puts), 2)
        (json_topic, json_payload, json_encoding), (pb_topic, pb_payload, pb_encoding) = session.puts

        self.assertTrue(json_topic.endswith("/v1"))
        self.assertEqual(pb_topic, topic_for_frame(json_topic))

        # The protobuf sample is the untouched SapientMessage — no re-encode,
        # so nothing the decoder does not model can be lost on the way out.
        self.assertEqual(pb_payload, frame)
        json.loads(json_payload.decode("utf-8"))

        self.assertEqual(json_encoding, zenoh.Encoding.APPLICATION_JSON)
        self.assertEqual(pb_encoding, zenoh.Encoding.APPLICATION_PROTOBUF)


if __name__ == "__main__":
    unittest.main()
