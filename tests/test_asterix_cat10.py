#!/usr/bin/env python3
"""Wire fixtures for EUROCONTROL ASTERIX CAT-010 Edition 1.1."""

import math
import os
from pathlib import Path
import struct
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "compose"))
sys.path.insert(0, os.fspath(ROOT / "compose" / "control"))
sys.path.insert(0, os.fspath(ROOT / "compose" / "protocols"))

from protocols.vendors.asterix.cat import decode_cat010_record  # noqa: E402


def fspec(*frns: int) -> bytes:
    octets = [0] * (max(frns) // 7 + 1)
    for frn in frns:
        octets[frn // 7] |= 1 << (7 - (frn % 7))
    for index in range(len(octets) - 1):
        octets[index] |= 1
    return bytes(octets)


def wgs32(degrees: float) -> bytes:
    return struct.pack(">i", round(degrees * (2**31) / 180.0))


class AsterixCat10Tests(unittest.TestCase):
    def test_ground_vehicle_target_uses_edition_1_1_uap(self):
        # FRNs: SAC/SIC, type, descriptor, time, WGS-84, polar velocity,
        # track number, vehicle fleet identification.
        record = b"".join(
            (
                fspec(0, 1, 2, 3, 4, 7, 9, 15),
                bytes((0, 9)),
                bytes((1,)),
                bytes((0b01100001, 0b00000100)),  # PSR + FX; target=ground vehicle
                (3600 * 128).to_bytes(3, "big"),
                wgs32(55.1234567),
                wgs32(24.7654321),
                struct.pack(">HH", 1000, round(90 * 65536 / 360)),
                struct.pack(">H", 77),
                bytes((3,)),  # fire fleet
            )
        )
        track, end = decode_cat010_record(record, 0)
        self.assertEqual(end, len(record))
        self.assertEqual(track["msg_type"], "target_report")
        self.assertEqual(track["target_type"], "ground_vehicle")
        self.assertEqual(track["vehicle_fleet"], "fire")
        self.assertEqual(track["track_num"], 77)
        self.assertAlmostEqual(track["lat_deg"], 55.1234567, places=6)
        self.assertAlmostEqual(track["lon_deg"], 24.7654321, places=6)
        self.assertAlmostEqual(track["heading_deg"], 90.0, places=2)
        self.assertTrue(math.isfinite(track["speed_ms"]))

    def test_cartesian_position_requires_explicit_airport_origin(self):
        record = b"".join(
            (
                fspec(0, 1, 2, 3, 6),
                bytes((0, 4)),
                bytes((1,)),
                bytes((0b00100000,)),  # Mode-S MLAT, no extent
                (20 * 128).to_bytes(3, "big"),
                struct.pack(">hh", 1000, 2000),
            )
        )
        without_origin, _ = decode_cat010_record(record, 0)
        self.assertNotIn("lat_deg", without_origin)
        with_origin, _ = decode_cat010_record(record, 0, 55.0, 24.0)
        self.assertAlmostEqual(with_origin["lat_deg"], 55.0 + 2000 / 111320, places=6)
        self.assertGreater(with_origin["lon_deg"], 24.0)

    def test_track_termination_is_a_tombstone(self):
        record = b"".join(
            (
                fspec(0, 1, 3, 4, 9, 10),
                bytes((0, 2)),
                bytes((1,)),
                (5 * 128).to_bytes(3, "big"),
                wgs32(54.7),
                wgs32(25.3),
                struct.pack(">H", 12),
                bytes((0x40,)),  # I010/170 TRE: last report
            )
        )
        track, _ = decode_cat010_record(record, 0)
        self.assertTrue(track["_delete"])


if __name__ == "__main__":
    unittest.main()
