#!/usr/bin/env python3
"""Focused unit tests for tak_alert_layer.py's acoustic-detection GeoChat alert
(dronuradaras.lt sensors on land/**) — mirrors the existing squawk/distress
handlers' fire-once/clear-on-cooldown shape."""

import json
import pathlib
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))
sys.path.insert(0, str(ROOT / "compose" / "layers"))

import tak_alert_layer  # noqa: E402


class FakeSample:
    def __init__(self, key_expr: str, payload: dict | bytes):
        self.key_expr = key_expr
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def _acoustic_track(age_s: float | None, **overrides) -> dict:
    track = {
        "sensor_type": "acoustic",
        "sensor_id": "MAINLINE-DRONU-ABCD1234",
        "sensor_name": "SITE-1",
        "lat_deg": 54.7, "lon_deg": 25.3,
    }
    if age_s is not None:
        track["last_detection_ts"] = time.time() - age_s
    track.update(overrides)
    return track


class AcousticHandlerTests(unittest.TestCase):
    def setUp(self):
        tak_alert_layer._alerted.clear()
        self.sender = mock.Mock()

    def test_fires_once_on_active_detection(self):
        handler = tak_alert_layer.make_acoustic_handler(self.sender, verbose=False)
        sample = FakeSample("efdi/land/mainline/acoustic/neutral/sensor/x/tracks/v1",
                             _acoustic_track(10))
        handler(sample)
        handler(sample)  # same still-hot state — must not re-alert
        self.sender.send.assert_called_once()
        sent_xml = self.sender.send.call_args[0][0]
        self.assertIn("DRONE DETECTED", sent_xml)
        self.assertIn("SITE-1", sent_xml)

    def test_no_alert_when_no_detection_on_record(self):
        handler = tak_alert_layer.make_acoustic_handler(self.sender, verbose=False)
        handler(FakeSample("efdi/land/mainline/acoustic/neutral/sensor/x/tracks/v1",
                            _acoustic_track(None)))
        self.sender.send.assert_not_called()

    def test_no_alert_once_detection_cools_down_past_hot_window(self):
        handler = tak_alert_layer.make_acoustic_handler(self.sender, verbose=False)
        handler(FakeSample("efdi/land/mainline/acoustic/neutral/sensor/x/tracks/v1",
                            _acoustic_track(tak_alert_layer._ACOUSTIC_ALERT_HOT_S + 5)))
        self.sender.send.assert_not_called()

    def test_ignores_non_acoustic_land_tracks(self):
        handler = tak_alert_layer.make_acoustic_handler(self.sender, verbose=False)
        handler(FakeSample("efdi/land/sitaware/civ/vehicle/x/tracks/v1",
                            {"sensor_type": None, "lat_deg": 1, "lon_deg": 1}))
        self.sender.send.assert_not_called()

    def test_realerts_on_a_new_detection_after_cooldown(self):
        handler = tak_alert_layer.make_acoustic_handler(self.sender, verbose=False)
        key = "efdi/land/mainline/acoustic/neutral/sensor/x/tracks/v1"
        handler(FakeSample(key, _acoustic_track(10)))
        handler(FakeSample(key, _acoustic_track(tak_alert_layer._ACOUSTIC_ALERT_HOT_S + 5)))
        handler(FakeSample(key, _acoustic_track(5)))
        self.assertEqual(self.sender.send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
