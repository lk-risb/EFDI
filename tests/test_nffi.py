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
    OUTPUT_TOPICS,
    _echelon,
    _output_topic,
    make_handler,
    parse_nffi,
)


# Shape matches the real STANAG 5527 / NFFI 1.4 schema (NC3A's own XSD,
# namespace urn:nato:fft:protocols:nffi14) — see docs/references/nffi/NFFI.md
# for where this was fetched from and how it was confirmed. The previous
# version of this fixture used a "urn:nato:nffi:2.0" namespace and PascalCase
# tags (UnitInfo/Latitude/Heading/...) that were never checked against any
# real schema and do not exist in any confirmed NFFI edition.
NFFI_DOCUMENT = b"""\
<NFFIMessage xmlns="urn:nato:fft:protocols:nffi14">
  <track>
    <positionalData secClassification="NATO RESTRICTED" secPolicyName="NATO">
      <trackSource>
        <sourceSystem>
          <system>ALPHA-C2</system>
        </sourceSystem>
        <transponderId>blue-17</transponderId>
      </trackSource>
      <dateTime>20260906081500</dateTime>
      <coordinates>
        <latitude>54.6872</latitude>
        <longitude>25.2797</longitude>
        <altitude>123.4</altitude>
      </coordinates>
      <bearing>271.2</bearing>
      <speed>30.6</speed>
    </positionalData>
    <identificationData>
      <unitSymbol>SFGPUCI---*****</unitSymbol>
      <unitShortName>ALPHA 17</unitShortName>
    </identificationData>
    <operStatusData>
      <alert>false</alert>
    </operStatusData>
  </track>
</NFFIMessage>
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
        track = tracks[0]
        self.assertEqual(track["sensor_id"], "ALPHA-C2-blue-17")
        self.assertEqual(track["callsign"], "ALPHA 17")
        self.assertEqual(track["lat_deg"], 54.6872)
        self.assertEqual(track["lon_deg"], 25.2797)
        self.assertEqual(track["geo_alt_m"], 123.4)
        self.assertAlmostEqual(track["speed_ms"], 30.6 / 3.6, places=2)  # schema unit: km/h
        self.assertEqual(track["heading_deg"], 271.2)
        self.assertEqual(track["affiliation"], "friendly")
        self.assertEqual(track["nffi_affiliation"], "FRIEND")
        self.assertEqual(track["unit_type"], "SFGPUCI---*****")
        self.assertNotIn("nffi_echelon", track)  # "**" is an unspecified placeholder, not a real echelon code
        self.assertFalse(track["emergency"])
        self.assertEqual(track["nffi_sec_classification"], "NATO RESTRICTED")
        self.assertEqual(track["nffi_sec_policy"], "NATO")
        self.assertEqual(track["classification"], "NATO RESTRICTED")

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
        # unitSymbol "SFGPUCI---*****": battle dimension 'G' -> land
        self.assertTrue(topic.startswith(OUTPUT_TOPICS["land"]))
        self.assertEqual(track["_src"], "nffi")
        self.assertEqual(track["callsign"], "ALPHA 17")
        self.assertIn("encoding", kwargs)

    def test_output_topic_routes_by_sidc_battle_dimension(self):
        self.assertEqual(_output_topic("SFGPUCI---*****"), OUTPUT_TOPICS["land"])
        self.assertEqual(_output_topic("SFAPMFFA--*****"), OUTPUT_TOPICS["air"])
        self.assertEqual(_output_topic("SFSPCL----*****"), OUTPUT_TOPICS["sea"])
        self.assertEqual(_output_topic("SFUPS-----*****"), OUTPUT_TOPICS["sea"])
        self.assertEqual(_output_topic("SFPPS-----*****"), OUTPUT_TOPICS["space"])
        self.assertEqual(_output_topic("SFXPS-----*****"), OUTPUT_TOPICS["land"])  # unrecognized -> default
        self.assertEqual(_output_topic(None), OUTPUT_TOPICS["land"])  # unitSymbol absent -> default

    def test_echelon_decoded_from_sidc_symbol_modifier(self):
        # Positions 11-12 (0-based index 10:12) hold the echelon code, e.g.
        # "-A" for Team/Crew — the leading '-' is part of the code itself,
        # distinct from the dashes filling out an unused function ID.
        self.assertEqual(_echelon("SFGPUCI----A***"), "Team/Crew")
        self.assertEqual(_echelon("SFGPUCI----F***"), "Battalion/Squadron")
        self.assertEqual(_echelon("SFGPUCI----M***"), "Region")
        self.assertIsNone(_echelon("SFGPUCI---*****"))  # "**" placeholder -> undecoded
        self.assertIsNone(_echelon("SFGPUCI-----***"))  # "--" placeholder -> undecoded
        self.assertIsNone(_echelon(None))
        self.assertIsNone(_echelon(""))

    def test_rejects_empty_oversized_and_malformed_documents(self):
        session = Session()
        handler = make_handler(session)

        handler(Sample(b""))
        handler(Sample(b"x" * (MAX_NFFI_XML + 1)))
        handler(Sample(b"<NFFI><UnitInfo>"))

        self.assertEqual(session.publications, [])


if __name__ == "__main__":
    unittest.main()
