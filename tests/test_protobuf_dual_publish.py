#!/usr/bin/env python3
"""Tests for the JSON->Protobuf dual-publish seam shared by the bridges.

Every publisher keeps emitting its existing JSON sample on the /v1 topic while
the protobuf sibling goes to /v2. These tests pin the two behaviours that are
easy to get wrong: picking the wrapper vs flat encoding, and never letting a
protobuf failure take down the JSON leg.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "compose" / "protocols"))
sys.path.insert(0, os.fspath(ROOT / "compose"))

import zenoh  # noqa: E402

from protocols.random.mavlink_pb2 import MavlinkTrack  # noqa: E402
from protocols.random.normalized_track_pb2 import NormalizedTrack  # noqa: E402
from protocols.random.raw_envelope_pb2 import RawEnvelope  # noqa: E402
from protocols.sapient_encode import (  # noqa: E402
    publish_sapient,
    track_to_sapient,
)
from sapient_msg.bsi_flex_335_v2_0.sapient_message_pb2 import SapientMessage  # noqa: E402
from protocols.protobuf_codec import (  # noqa: E402
    asterix_data_block,
    dual_topic,
    native_topic,
    publish_dual,
    publish_native,
    wrapped_track_message,
)


TRACK = {
    "_ts": 1_700_000_000.0,
    "_src": "mavlink",
    "lat_deg": 54.6872,
    "lon_deg": 25.2797,
    "callsign": "UAV-1",
    "speed_ms": 12.5,
    "system_id": 7,
    "vehicle_type": "quadrotor",
}


class RecordingSession:
    def __init__(self):
        self.puts = []

    def put(self, topic, payload, encoding=None):
        self.puts.append((topic, payload, encoding))


class DualTopicTests(unittest.TestCase):
    def test_format_segment_is_rewritten_in_place(self):
        self.assertEqual(dual_topic("root/air/x/id/json"), "root/air/x/id/proto")

    def test_topic_without_a_format_segment_gains_one(self):
        self.assertEqual(dual_topic("root/air/x/id"), "root/air/x/id/proto")


class WrappedTrackMessageTests(unittest.TestCase):
    def test_inner_normalized_track_is_populated(self):
        """Regression: reflection alone left `track` empty, so the /v2 sample
        carried protocol extras and no position."""
        message = wrapped_track_message(MavlinkTrack, TRACK)
        self.assertAlmostEqual(message.track.lat_deg, 54.6872)
        self.assertAlmostEqual(message.track.lon_deg, 25.2797)
        self.assertEqual(message.track.callsign, "UAV-1")

    def test_protocol_specific_fields_sit_alongside_the_track(self):
        message = wrapped_track_message(MavlinkTrack, TRACK)
        self.assertEqual(message.system_id, 7)
        self.assertEqual(message.vehicle_type, "quadrotor")

    def test_repeated_fields_are_extended_not_assigned(self):
        """Regression: a repeated field rejects `msg.f = [...]`. Assigning it
        raised out of the builder, and publish_dual's guard then dropped the
        ENTIRE protobuf sample — one list field silently cost the whole /v2
        message, not just that field."""
        from protocols.vendors.sapient.flex335_pb2 import SapientFlex335Track

        track = dict(TRACK, sapient_node_types=["acoustic", "radar"], sensor_id="S1")
        message = wrapped_track_message(SapientFlex335Track, track)

        self.assertEqual(list(message.sapient_node_types), ["acoustic", "radar"])
        self.assertEqual(message.sensor_id, "S1")
        # The nested track must survive alongside the repeated field.
        self.assertAlmostEqual(message.track.lat_deg, 54.6872)

    def test_scalar_string_is_not_treated_as_a_repeated_sequence(self):
        """A str is iterable — extending a repeated field with one would splay
        it into characters, so scalars must never take the repeated path."""
        from protocols.vendors.sapient.flex335_pb2 import SapientFlex335Track

        message = wrapped_track_message(
            SapientFlex335Track, dict(TRACK, sapient_node_types="acoustic")
        )
        self.assertEqual(list(message.sapient_node_types), [])

    def test_values_the_contract_cannot_hold_are_skipped(self):
        track = dict(TRACK, nested={"a": 1}, unmodelled="x")
        message = wrapped_track_message(MavlinkTrack, track)
        self.assertAlmostEqual(message.track.lat_deg, 54.6872)


class PublishDualTests(unittest.TestCase):
    def test_publishes_json_and_protobuf_with_correct_encodings(self):
        session = RecordingSession()
        publish_dual(session, "root/air/mavlink/json", dict(TRACK), MavlinkTrack, zenoh)

        by_topic = {topic: (payload, encoding) for topic, payload, encoding in session.puts}
        # The key now carries {type}/{id}: root/air/mavlink/quadrotor/uav-1/...
        json_topic = next(k for k in by_topic if k.endswith("/json"))
        pb_topic = next(k for k in by_topic if k.endswith("/proto"))
        self.assertTrue(json_topic.startswith("root/air/mavlink/quadrotor/uav-1"))
        json_payload, json_encoding = by_topic[json_topic]
        pb_payload, pb_encoding = by_topic[pb_topic]
        self.assertEqual(json_encoding, zenoh.Encoding.APPLICATION_JSON)
        self.assertEqual(pb_encoding, zenoh.Encoding.APPLICATION_PROTOBUF)

        self.assertEqual(json.loads(json_payload.decode())["callsign"], "UAV-1")
        decoded = MavlinkTrack()
        decoded.ParseFromString(pb_payload)
        self.assertAlmostEqual(decoded.track.lat_deg, 54.6872)

    def test_flat_contract_uses_reflection_instead_of_the_wrapper(self):
        """NormalizedTrack has no inner `track` field, so it takes the flat path."""
        session = RecordingSession()
        publish_dual(session, "root/fused/json", dict(TRACK), NormalizedTrack, zenoh)

        by_topic = {topic: payload for topic, payload, _e in session.puts}
        decoded = NormalizedTrack()
        decoded.ParseFromString(next(v for k, v in by_topic.items() if k.endswith("/proto")))
        self.assertAlmostEqual(decoded.lat_deg, 54.6872)

    def test_protobuf_failure_never_blocks_the_json_leg(self):
        """A track missing the mandatory position must not stop JSON delivery —
        JSON is the live path until consumers migrate to /v2."""
        session = RecordingSession()
        publish_dual(session, "root/air/mavlink/json", {"callsign": "NO-POS"}, MavlinkTrack, zenoh)

        self.assertEqual(len(session.puts), 1)
        self.assertTrue(session.puts[0][0].endswith("/json"))
        self.assertEqual(json.loads(session.puts[0][1].decode())["callsign"], "NO-POS")


class NativeFrameEgressTests(unittest.TestCase):
    """Verbatim source bytes ride a RawEnvelope on a /native sibling, so a
    consumer can recover fields the decoder does not model at all."""

    def test_native_topic_rewrites_the_format_segment(self):
        self.assertEqual(
            native_topic("root/air/asterix/x/aircraft/b738/ly-abc/json"),
            "root/air/asterix/x/aircraft/b738/ly-abc/raw",
        )
        self.assertEqual(native_topic("root/other"), "root/other/raw")

    def test_asterix_data_block_rebuilds_a_standalone_block(self):
        record = bytes(range(10))
        block = asterix_data_block(48, record)
        self.assertEqual(block[0], 48)                                  # CAT byte
        self.assertEqual(int.from_bytes(block[1:3], "big"), len(block))  # total length
        self.assertEqual(block[3:], record)

    def test_asterix_data_block_rejects_oversized_records(self):
        with self.assertRaises(ValueError):
            asterix_data_block(48, b"\x00" * 0xFFFF)

    def test_publish_native_carries_bytes_unchanged(self):
        session = RecordingSession()
        payload = asterix_data_block(48, bytes(range(20)))
        publish_native(session, "root/air/asterix/raw", payload,
                       "asterix", zenoh, profile="cat048")

        self.assertEqual(len(session.puts), 1)
        topic, data, encoding = session.puts[0]
        self.assertEqual(encoding, zenoh.Encoding.APPLICATION_PROTOBUF)

        envelope = RawEnvelope()
        envelope.ParseFromString(data)
        self.assertEqual(envelope.payload, payload)   # byte-identical
        self.assertEqual(envelope.protocol, "asterix")
        self.assertEqual(envelope.profile, "cat048")


class SapientEgressTests(unittest.TestCase):
    """Every track also leaves as a real BSI Flex 335 v2 SapientMessage, so a
    fabric consumer can speak SAPIENT alone instead of every source protocol."""

    def test_track_becomes_a_valid_detection_report(self):
        message = track_to_sapient(dict(TRACK, target_type="aircraft"))
        self.assertIsNotNone(message)
        self.assertEqual(message.WhichOneof("content"), "detection_report")

        report = message.detection_report
        # Schema: x is longitude, y is latitude — swapping them is the easy bug.
        self.assertAlmostEqual(report.location.y, 54.6872)
        self.assertAlmostEqual(report.location.x, 25.2797)
        self.assertEqual(report.classification[0].type, "Air Vehicle")
        self.assertEqual(len(report.report_id), 26)   # ULID, not UUID
        self.assertEqual(len(report.object_id), 26)

    def test_serialises_against_the_official_schema(self):
        raw = track_to_sapient(dict(TRACK)).SerializeToString()
        decoded = SapientMessage()
        decoded.ParseFromString(raw)
        self.assertAlmostEqual(decoded.detection_report.location.y, 54.6872)
        self.assertEqual(
            SapientMessage.DESCRIPTOR.full_name,
            "sapient_msg.bsi_flex_335_v2_0.SapientMessage",
        )

    def test_speed_and_heading_become_an_east_north_vector(self):
        # 90 degrees is due east: all of the speed lands on east_rate.
        east = track_to_sapient(dict(TRACK, speed_ms=100.0, heading_deg=90.0))
        self.assertAlmostEqual(east.detection_report.enu_velocity.east_rate, 100.0, places=6)
        self.assertAlmostEqual(east.detection_report.enu_velocity.north_rate, 0.0, places=6)
        # 0 degrees is due north.
        north = track_to_sapient(dict(TRACK, speed_ms=100.0, heading_deg=0.0))
        self.assertAlmostEqual(north.detection_report.enu_velocity.north_rate, 100.0, places=6)

    def test_object_id_is_stable_for_the_same_object(self):
        """A moving object must keep one object_id across reports, or every
        update looks like a brand-new contact to the consumer."""
        first = track_to_sapient(dict(TRACK, uid="OBJ-1")).detection_report.object_id
        second = track_to_sapient(dict(TRACK, uid="OBJ-1", lat_deg=54.70)).detection_report.object_id
        self.assertEqual(first, second)
        other = track_to_sapient(dict(TRACK, uid="OBJ-2")).detection_report.object_id
        self.assertNotEqual(first, other)

    def test_track_without_a_position_is_refused(self):
        """location sits in a mandatory oneof — better to publish nothing than
        a structurally invalid SAPIENT message."""
        self.assertIsNone(track_to_sapient({"_ts": 1.0, "callsign": "GHOST"}))

    def test_publish_dual_emits_the_sapient_tier_alongside_the_others(self):
        session = RecordingSession()
        publish_dual(session, "root/air/mavlink/json", dict(TRACK), MavlinkTrack, zenoh)

        topics = [topic for topic, _payload, _encoding in session.puts]
        self.assertTrue(any(t.endswith("/json") for t in topics))
        self.assertTrue(any(t.endswith("/proto") for t in topics))
        self.assertTrue(any(t.endswith("/sapient") for t in topics))

    def test_sapient_failure_never_blocks_the_other_tiers(self):
        session = RecordingSession()
        publish_sapient(session, "root/air/x/json", {"no": "position"}, zenoh)
        self.assertEqual(session.puts, [])


if __name__ == "__main__":
    unittest.main()
