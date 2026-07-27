#!/usr/bin/env python3
"""Focused tests for the public BSI Flex 335 v2 SAPIENT adapter."""

from __future__ import annotations

import os
import socket
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "compose" / "protocols"))
sys.path.insert(0, os.fspath(ROOT / "compose"))

import json  # noqa: E402

import zenoh  # noqa: E402

from protocols.protobuf_codec import dual_topic  # noqa: E402
from protocols.random.raw_envelope_pb2 import RawEnvelope  # noqa: E402
from protocols.vendors.sapient.flex335_pb2 import SapientFlex335Track  # noqa: E402

from protocols.vendors.sapient.flex335 import (  # noqa: E402
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
        self.assertAlmostEqual(
            event.track["sapient_signals"][0]["centre_frequency"],
            2_450_000_000.0,
            delta=256.0,
        )
        self.assertNotIn("centre_frequency_hz", event.track["sapient_signals"][0])
        self.assertEqual(
            topic_for_track("LTU/CISB/partner", event.track),
            "LTU/CISB/partner/air/sapient/acoustic/unknown/uav",
        )

    def test_velocity_without_registered_units_is_not_mislabeled_as_metres_per_second(self):
        decoder = SapientDecoder()
        velocity = field_double(1, 36.0) + field_double(2, 0.0) + field_double(3, 2.5)
        detection = b"".join(
            (
                field_text(1, "01HREPORT"),
                field_text(2, "01HOBJECT"),
                field_bytes(6, location(54.6872, 25.2797, 145.0)),
                field_bytes(19, velocity),
            )
        )
        event = decoder.decode(envelope(7, detection))
        self.assertIsNone(event.warning)
        self.assertNotIn("speed_ms", event.track)
        self.assertNotIn("heading_deg", event.track)
        self.assertNotIn("vertical_rate_ms", event.track)

    def test_conflicting_registration_units_disable_velocity_normalization(self):
        def detection_definition(horizontal: int, vertical: int) -> bytes:
            units = field_varint(1, horizontal) + field_varint(2, vertical)
            return field_bytes(6, field_bytes(4, units))

        mode = field_bytes(10, detection_definition(1, 1))
        mode += field_bytes(10, detection_definition(2, 2))
        decoder = SapientDecoder()
        decoder.decode(envelope(4, field_bytes(7, mode)))
        node = decoder.nodes[NODE_ID]
        self.assertIsNone(node.horizontal_speed_units)
        self.assertIsNone(node.vertical_speed_units)

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
            "root/pod/land/sapient/acoustic/neutral/sensor",
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

    def test_topic_for_frame_delegates_to_native(self):
        self.assertEqual(topic_for_frame("root/air/x/id/json"), "root/air/x/id/raw/tracks/v1")
        # Even a bare object key gains the raw view and the /tracks/v1 tail.
        self.assertEqual(topic_for_frame("root/other"), "root/other/raw/tracks/v1")

    def test_publish_emits_all_three_tiers(self):
        """SAPIENT follows the same tier contract as every other protocol:
        /v1 JSON, /v2 typed EFDI message, /native the original wire bytes."""
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

        # /v1 JSON, /v2 typed, /native verbatim, plus the SAPIENT interop
        # tier that every protocol now emits.
        by_topic = {topic: (payload, encoding) for topic, payload, encoding in session.puts}
        self.assertGreaterEqual(len(by_topic), 3)
        json_topic = next(t for t in by_topic
                          if by_topic[t][1] == zenoh.Encoding.APPLICATION_JSON)

        json_payload, json_encoding = by_topic[json_topic]
        self.assertEqual(json_encoding, zenoh.Encoding.APPLICATION_JSON)
        self.assertAlmostEqual(
            json.loads(json_payload.decode("utf-8"))["lat_deg"], 54.6872, places=4
        )

        # /v2 — typed EFDI contract, not the raw frame.
        typed_payload, typed_encoding = by_topic[dual_topic(json_topic)]
        self.assertEqual(typed_encoding, zenoh.Encoding.APPLICATION_PROTOBUF)
        typed = SapientFlex335Track()
        typed.ParseFromString(typed_payload)
        self.assertAlmostEqual(typed.track.lat_deg, 54.6872, places=4)

        # /native — the untouched SapientMessage, so nothing the decoder does
        # not model is lost on the way out.
        raw_key = next((k for k in by_topic if k.endswith("/raw/tracks/v1")), None)
        self.assertIsNotNone(raw_key, f"no /raw view in {sorted(by_topic)}")
        native_payload, native_encoding = by_topic[raw_key]
        self.assertEqual(native_encoding, zenoh.Encoding.APPLICATION_PROTOBUF)
        envelope_message = RawEnvelope()
        envelope_message.ParseFromString(native_payload)
        self.assertEqual(envelope_message.payload, frame)
        self.assertEqual(envelope_message.protocol, "sapient")
        self.assertEqual(envelope_message.profile, "bsi-flex-335-v2")


if __name__ == "__main__":
    unittest.main()
