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
from protocols.protobuf_codec import (  # noqa: E402
    dual_topic,
    publish_dual,
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
    def test_v1_is_swapped_for_v2(self):
        self.assertEqual(dual_topic("root/air/tracks/v1"), "root/air/tracks/v2")

    def test_topic_without_v1_suffix_still_gets_a_distinct_sibling(self):
        self.assertEqual(dual_topic("root/air/tracks"), "root/air/tracks/v2")


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

    def test_values_the_contract_cannot_hold_are_skipped(self):
        track = dict(TRACK, nested={"a": 1}, unmodelled="x")
        message = wrapped_track_message(MavlinkTrack, track)
        self.assertAlmostEqual(message.track.lat_deg, 54.6872)


class PublishDualTests(unittest.TestCase):
    def test_publishes_json_and_protobuf_with_correct_encodings(self):
        session = RecordingSession()
        publish_dual(session, "root/air/mavlink/tracks/v1", dict(TRACK), MavlinkTrack, zenoh)

        self.assertEqual(len(session.puts), 2)
        (json_topic, json_payload, json_encoding), (pb_topic, pb_payload, pb_encoding) = session.puts

        self.assertEqual(json_topic, "root/air/mavlink/tracks/v1")
        self.assertEqual(pb_topic, "root/air/mavlink/tracks/v2")
        self.assertEqual(json_encoding, zenoh.Encoding.APPLICATION_JSON)
        self.assertEqual(pb_encoding, zenoh.Encoding.APPLICATION_PROTOBUF)

        self.assertEqual(json.loads(json_payload.decode())["callsign"], "UAV-1")
        decoded = MavlinkTrack()
        decoded.ParseFromString(pb_payload)
        self.assertAlmostEqual(decoded.track.lat_deg, 54.6872)

    def test_flat_contract_uses_reflection_instead_of_the_wrapper(self):
        """NormalizedTrack has no inner `track` field, so it takes the flat path."""
        session = RecordingSession()
        publish_dual(session, "root/fused/tracks/v1", dict(TRACK), NormalizedTrack, zenoh)

        self.assertEqual(len(session.puts), 2)
        decoded = NormalizedTrack()
        decoded.ParseFromString(session.puts[1][1])
        self.assertAlmostEqual(decoded.lat_deg, 54.6872)

    def test_protobuf_failure_never_blocks_the_json_leg(self):
        """A track missing the mandatory position must not stop JSON delivery —
        JSON is the live path until consumers migrate to /v2."""
        session = RecordingSession()
        publish_dual(session, "root/air/mavlink/tracks/v1", {"callsign": "NO-POS"}, MavlinkTrack, zenoh)

        self.assertEqual(len(session.puts), 1)
        self.assertEqual(session.puts[0][0], "root/air/mavlink/tracks/v1")
        self.assertEqual(json.loads(session.puts[0][1].decode())["callsign"], "NO-POS")


if __name__ == "__main__":
    unittest.main()
