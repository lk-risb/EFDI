#!/usr/bin/env python3
"""Focused unit tests for intcore_layer.py (EFDI tracks -> INT-CORE TopicApi)."""

import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))
sys.path.insert(0, str(ROOT / "compose" / "layers"))

import intcore_layer  # noqa: E402


class FakeSample:
    def __init__(self, key_expr: str, payload: dict | bytes):
        self.key_expr = key_expr
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class SaveItemTests(unittest.TestCase):
    def setUp(self):
        intcore_layer._URL = "https://intcore.example"
        intcore_layer._API_KEY = "test-key"
        intcore_layer._TOPIC_ID = "9fce4e97-d1dd-435e-192c-08df149237a4"
        intcore_layer._DATA_SOURCE_ID = "efdi"

    def test_save_item_posts_expected_body_and_headers(self):
        captured = {}

        class FakeResponse:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, context=None, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            intcore_layer.save_item("<nvg/>", verbose=False)

        self.assertEqual(captured["url"], "https://intcore.example/topicapi/Topic/SaveItems")
        self.assertEqual(captured["headers"].get("Apikey"), "test-key")
        self.assertEqual(captured["body"], {
            "topicId": "9fce4e97-d1dd-435e-192c-08df149237a4",
            "content": "<nvg/>",
            "dataSourceId": "efdi",
        })

    def test_save_item_swallows_http_error_without_raising(self):
        def fake_urlopen(req, context=None, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            intcore_layer.save_item("<nvg/>", verbose=False)  # must not raise


class MakeHandlerTests(unittest.TestCase):
    def setUp(self):
        self.posted = []
        self.patcher = mock.patch.object(intcore_layer, "save_item",
                                          side_effect=lambda content, verbose: self.posted.append(content))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _track(self, **overrides):
        track = {"lat_deg": 56.9, "lon_deg": 24.1, "icao24": "abc123", "_src": "test"}
        track.update(overrides)
        return track

    def test_forwards_a_plain_track(self):
        handler = intcore_layer.make_handler("SFGPU-----*****", verbose=False)
        handler(FakeSample("EFDI/land/nffi/c2/friendly/unit/tracks/v1", self._track()))
        self.assertEqual(len(self.posted), 1)
        self.assertIn("<nvg", self.posted[0])

    def test_skips_its_own_intcore_ingress_to_avoid_a_loop(self):
        handler = intcore_layer.make_handler("SFGPU-----*****", verbose=False)
        handler(FakeSample(
            "EFDI/land/intcore/c2/friendly/unit/tracks/v1",
            self._track(_ingress="intcore"),
        ))
        self.assertEqual(self.posted, [])

    def test_skips_deleted_tracks(self):
        handler = intcore_layer.make_handler("SFGPU-----*****", verbose=False)
        handler(FakeSample("EFDI/land/nffi/c2/friendly/unit/tracks/v1",
                            self._track(_delete=True)))
        self.assertEqual(self.posted, [])

    def test_skips_non_json_views(self):
        handler = intcore_layer.make_handler("SFGPU-----*****", verbose=False)
        handler(FakeSample("EFDI/land/nffi/c2/friendly/unit/sapient/tracks/v1",
                            self._track()))
        self.assertEqual(self.posted, [])

    def test_skips_unfused_raw_sensor_tracks(self):
        handler = intcore_layer.make_handler("SUAPMF----*****", verbose=False)
        handler(FakeSample(
            "EFDI/air/radar/c2/unknown/aircraft/tracks/v1",
            self._track(_src="ASTERIX CAT-48"),
        ))
        self.assertEqual(self.posted, [])


if __name__ == "__main__":
    unittest.main()
