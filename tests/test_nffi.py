#!/usr/bin/env python3
"""NFFI Zenoh protocol translation tests."""

import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "protocols"))

from protocols.random.nffi import (  # noqa: E402
    MAX_NFFI_XML,
    OUTPUT_TOPIC,
    make_handler,
    parse_nffi,
)


NFFI_DOCUMENT = b"""\
<NFFI xmlns="urn:nato:nffi:2.0">
  <UnitInfo>
    <UnitID>blue-17</UnitID>
    <Name>ALPHA 17</Name>
    <Affiliation>FRIEND</Affiliation>
    <Latitude>54.6872</Latitude>
    <Longitude>25.2797</Longitude>
    <Altitude>123.4</Altitude>
    <Speed>8.5</Speed>
    <Heading>271.2</Heading>
  </UnitInfo>
</NFFI>
"""


class Session:
    def __init__(self):
        self.publications = []

    def put(self, topic, payload, **kwargs):
        self.publications.append((topic, payload, kwargs))


class Sample:
    key_expr = "LTU/CISB/partner/raw/nffi/c2-source"

    def __init__(self, payload):
        self.payload = payload


class NffiProtocolTests(unittest.TestCase):
    def test_parses_namespaced_document_once(self):
        tracks = parse_nffi(NFFI_DOCUMENT)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["sensor_id"], "blue-17")
        self.assertEqual(tracks[0]["callsign"], "ALPHA 17")
        self.assertEqual(tracks[0]["lat_deg"], 54.6872)
        self.assertEqual(tracks[0]["lon_deg"], 25.2797)
        self.assertEqual(tracks[0]["geo_alt_m"], 123.4)
        self.assertEqual(tracks[0]["speed_ms"], 8.5)
        self.assertEqual(tracks[0]["heading_deg"], 271.2)
        self.assertEqual(tracks[0]["nffi_affil"], "FRIEND")

    def test_raw_zenoh_xml_is_published_as_normalized_json(self):
        session = Session()
        make_handler(session)(Sample(NFFI_DOCUMENT))

        # JSON /v1 + per-protocol /v2 + SAPIENT /sapient
        self.assertGreaterEqual(len(session.publications), 2)
        _views = ("/proto/tracks/v1", "/sapient/tracks/v1", "/raw/tracks/v1")
        topic, payload, kwargs = next(
            x for x in session.publications
            if x[0].endswith("/tracks/v1") and not x[0].endswith(_views)
        )
        track = json.loads(payload)
        self.assertTrue(topic.startswith(OUTPUT_TOPIC))
        self.assertEqual(track["_src"], "nffi")
        self.assertEqual(track["callsign"], "ALPHA 17")
        self.assertIn("encoding", kwargs)

    def test_rejects_empty_oversized_and_malformed_documents(self):
        session = Session()
        handler = make_handler(session)

        handler(Sample(b""))
        handler(Sample(b"x" * (MAX_NFFI_XML + 1)))
        handler(Sample(b"<NFFI><UnitInfo>"))

        self.assertEqual(session.publications, [])


if __name__ == "__main__":
    unittest.main()
