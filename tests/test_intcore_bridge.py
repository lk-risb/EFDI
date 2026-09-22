#!/usr/bin/env python3
"""Focused unit tests for intcore_bridge.py (INT-CORE Dissemination -> Zenoh).

The HttpPostAdt's real body shape was confirmed live against a running
INT-CORE instance: raw NVG XML, no envelope, no dataSourceId anywhere on the
wire. That means this bridge cannot self-filter its own echoed items by
content — loop prevention has to be a Subscription-side exclusion on
INT-CORE's end, not something these tests can exercise. What's tested here
is what the bridge actually controls: NVG parsing, SIDC-to-topic mapping,
and the HTTP handler's path/auth/size checks."""

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))
sys.path.insert(0, str(ROOT / "compose" / "layers"))

import intcore_bridge  # noqa: E402

NVG_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<nvg xmlns="https://tide.act.nato.int/schemas/2012/10/nvg" version="2.0.2">'
    '<point uri="urn:efdi:FOO-1" symbol="app6c:SFGPU-----*****" label="FOO-1" '
    'x="24.1" y="56.9"/></nvg>'
)

# Byte-identical to a real item captured live off INT-CORE's KML->NVG 2.0
# transform (confirmed by the INTCORE peer session via a temporary
# FileSystem capture target, 374 bytes). No symbol attribute (this
# transform doesn't carry one), point nested inside a <g> group rather than
# a direct child of <nvg>, and a bogus encoding="utf-16" declaration on
# content that's actually UTF-8 (harmless here — the body arrives as bytes
# already decoded UTF-8 by _handle_dissemination before this ever reaches
# the XML parser, so the declared encoding is never consulted).
REAL_VENDOR_NVG_XML = (
    '<?xml version="1.0" encoding="utf-16"?>\n'
    '<nvg version="2.0.2" xmlns="https://tide.act.nato.int/schemas/2012/10/nvg" '
    'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:dcterms="http://purl.org/dc/terms/">\n'
    '  <g uri="generatedURI" label="">\n'
    '    <point uri="generatedURI" label="EFDIDeliveryTest6" x="25.34" y="54.74" />\n'
    '  </g>\n'
    '</nvg>'
)


class NvgParseTests(unittest.TestCase):
    def test_parses_position_uid_label_sidc(self):
        track, sidc = intcore_bridge.nvg_item_to_track(NVG_XML)
        self.assertAlmostEqual(track["lat_deg"], 56.9)
        self.assertAlmostEqual(track["lon_deg"], 24.1)
        self.assertEqual(track["uid"], "FOO-1")
        self.assertEqual(track["callsign"], "FOO-1")
        self.assertEqual(track["_ingress"], "intcore")
        self.assertEqual(sidc, "SFGPU-----*****")

    def test_rejects_garbage(self):
        self.assertIsNone(intcore_bridge.nvg_item_to_track("not xml"))

    def test_sidc_to_topic_land_friendly_unit(self):
        topic = intcore_bridge.sidc_to_topic("SFGPU-----*****")
        self.assertTrue(topic.endswith("/land/intcore/c2/friendly/unit"))

    def test_non_sidc_symbol_scheme_is_dropped_not_misrouted(self):
        # Regression: IntCoreKMLToNVG20.xslt's CreateSymbol/ExtractSymbolCode
        # templates prove `symbol` isn't always `app6x:<SIDC>` — a KML
        # Placemark styled with a flag or a custom icon comes out as
        # `flag:<code>` or `icon:<href-or-code>`. Before the scheme
        # allowlist, treating that code as a real SIDC misrouted it:
        # sidc[1]='S'/sidc[2]='A' landed on real affiliation/dimension chars
        # by coincidence, so a "USA" flag item came out hostile/aircraft.
        # Now any non-SIDC scheme falls back to "" — same safe unknown/unit
        # routing already used when the transform omits `symbol` entirely.
        track, sidc = intcore_bridge.nvg_item_to_track(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<nvg xmlns="https://tide.act.nato.int/schemas/2012/10/nvg" version="2.0.2">'
            '<point uri="urn:efdi:FLAG-1" symbol="flag:USA" label="FLAG-1" '
            'x="24.1" y="56.9"/></nvg>'
        )
        self.assertEqual(sidc, "")
        self.assertEqual(
            intcore_bridge.sidc_to_topic(sidc),
            intcore_bridge.TOPIC_ROOT + "/land/intcore/c2/unknown/unit",
        )

    def test_app6_scheme_symbol_still_parses(self):
        # Case-insensitive scheme check ("APP6C" as well as "app6c") must
        # not itself start rejecting the real, already-tested SIDC path.
        track, sidc = intcore_bridge.nvg_item_to_track(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<nvg xmlns="https://tide.act.nato.int/schemas/2012/10/nvg" version="2.0.2">'
            '<point uri="urn:efdi:BAR-1" symbol="APP6C:SFGPU-----*****" label="BAR-1" '
            'x="24.1" y="56.9"/></nvg>'
        )
        self.assertEqual(sidc, "SFGPU-----*****")

    def test_parses_real_vendor_nvg_with_point_nested_under_g(self):
        # Regression: root.find(NVG_NS + "point") only matched a direct
        # child and silently returned None against this real shape — every
        # hand-built fixture in this file (point as a direct <nvg> child)
        # kept passing while live vendor traffic was dropped outright.
        result = intcore_bridge.nvg_item_to_track(REAL_VENDOR_NVG_XML)
        self.assertIsNotNone(result)
        track, sidc = result
        self.assertAlmostEqual(track["lat_deg"], 54.74)
        self.assertAlmostEqual(track["lon_deg"], 25.34)
        self.assertEqual(track["callsign"], "EFDIDeliveryTest6")
        self.assertEqual(sidc, "")  # this transform emits no symbol attribute
        # No crash, sensible fallback rather than a KeyError/IndexError.
        self.assertEqual(intcore_bridge.sidc_to_topic(sidc),
                          intcore_bridge.TOPIC_ROOT + "/land/intcore/c2/unknown/unit")


class HandleDisseminationTests(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()

    def test_forwards_a_raw_nvg_xml_body(self):
        intcore_bridge._handle_dissemination(self.session, NVG_XML.encode(), verbose=False)
        self.session.put.assert_called_once()

    def test_forwards_the_real_vendor_payload_byte_for_byte(self):
        intcore_bridge._handle_dissemination(
            self.session, REAL_VENDOR_NVG_XML.encode("utf-8"), verbose=False)
        self.session.put.assert_called_once()

    def test_drops_non_xml_body(self):
        intcore_bridge._handle_dissemination(self.session, b"plain text, not xml",
                                              verbose=False)
        self.session.put.assert_not_called()

    def test_drops_unparseable_xml(self):
        intcore_bridge._handle_dissemination(self.session, b"<not-nvg/>", verbose=False)
        self.session.put.assert_not_called()


class HttpHandlerTests(unittest.TestCase):
    """Exercises make_handler_class()'s do_POST() logic directly, without
    binding a real socket — BaseHTTPRequestHandler's __init__ normally does
    the HTTP handshake itself, so the request/response plumbing is faked."""

    def _make_request(self, path, headers, body, token=""):
        intcore_bridge._TOKEN = token
        handler_class = intcore_bridge.make_handler_class(
            self.session, verbose=False, path="/intcore/dissemination")
        handler = handler_class.__new__(handler_class)
        handler.path = path
        handler.headers = headers
        handler.rfile = mock.Mock()
        handler.rfile.read.return_value = body
        handler.responses = []
        handler.send_response = lambda code: handler.responses.append(code)
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        return handler

    def setUp(self):
        self.session = mock.Mock()

    def tearDown(self):
        intcore_bridge._TOKEN = ""

    def test_wrong_path_is_404(self):
        handler = self._make_request("/wrong-path", {"Content-Length": "10"}, NVG_XML.encode())
        handler.do_POST()
        self.assertEqual(handler.responses, [404])
        self.session.put.assert_not_called()

    def test_missing_token_is_401_when_token_configured(self):
        handler = self._make_request("/intcore/dissemination", {"Content-Length": "10"},
                                      NVG_XML.encode(), token="secret")
        handler.do_POST()
        self.assertEqual(handler.responses, [401])
        self.session.put.assert_not_called()

    def test_correct_token_is_accepted(self):
        headers = {"Content-Length": str(len(NVG_XML)), "X-EFDI-Token": "secret"}
        handler = self._make_request("/intcore/dissemination", headers,
                                      NVG_XML.encode(), token="secret")
        handler.do_POST()
        self.assertEqual(handler.responses, [204])
        self.session.put.assert_called_once()

    def test_oversized_body_rejected(self):
        headers = {"Content-Length": str(intcore_bridge.MAX_BODY + 1)}
        handler = self._make_request("/intcore/dissemination", headers, b"")
        handler.do_POST()
        self.assertEqual(handler.responses, [400])
        self.session.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
