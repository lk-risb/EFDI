#!/usr/bin/env python3
"""Focused unit tests for the SitaWare HQ pull-based NVG feed."""

import base64
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))
sys.path.insert(0, str(ROOT / "compose" / "layers"))

# The feed server moved to nvg_layer: it writes the fabric OUT to SitaWare, so
# it is the C2 egress. SitaWare polling it is a transport detail. nvg_bridge is
# now the ingress that reads SitaWare's NVG export back into Zenoh.
from nvg_layer import (  # noqa: E402
    NVGFeedCache,
    NVGFeedServer,
    basic_authorized,
)
from nvg_layer import NVG_NS  # noqa: E402
from nvg_layer import _TOPIC_SIDC  # noqa: E402
from nvg_layer import _resolve_sidc  # noqa: E402
from nvg_layer import track_to_nvg_item  # noqa: E402
from cot_layer import _is_unfused_sensor_track  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class NVGFeedCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = NVGFeedCache(
            stale_s=30,
            max_tracks=2,
            clock=self.clock,
            wall_clock=self.clock,
        )

    @staticmethod
    def track(uid: str, lat: float = 54.6872):
        return {
            "_ts": 1_721_000_000,
            "_src": "test",
            "uid": uid,
            "callsign": uid,
            "lat_deg": lat,
            "lon_deg": 25.2797,
            "alt_m": 1000,
            "heading_deg": 90,
            "speed_ms": 12,
        }

    def test_document_contains_aggregated_points(self):
        first = self.track("one")
        first.update(registration="TEST-REG", icao24="abcdef", squawk="7000")
        self.cache.upsert(first, "SFAPMF----*****")
        self.cache.upsert(self.track("two", 55.0), "SFGPU-----*****")
        body, count = self.cache.document()

        root = ET.fromstring(body)
        points = root.findall("{%s}point" % NVG_NS)
        self.assertEqual(count, 2)
        self.assertEqual(NVG_NS, "https://tide.act.nato.int/schemas/2012/10/nvg")
        self.assertEqual(root.attrib["version"], "2.0.2")
        self.assertEqual(
            {point.attrib["uri"] for point in points},
            {"urn:efdi:EFDI-ICAO-ABCDEF", "urn:efdi:EFDI-UID-TWO"},
        )
        self.assertEqual(points[0].attrib["symbol"], "2525b:SFAPMF----*****")
        self.assertEqual(
            points[0].attrib["modifiers"],
            "T:TEST-REG;H:SRC test | ICAO abcdef | REG TEST-REG | SQ 7000;"
            "W:2024-07-14T23-33-20.000Z;"
            "X:3281 ft | 1000 m | reported;"
            "Y:54.68720, 25.27970;Z:23 kt | 43 km/h;P:7000",
        )
        self.assertEqual(points[0].attrib["x"], "25.2797")
        self.assertEqual(points[0].attrib["z"], "1000.0")
        self.assertEqual(points[0].attrib["speed"], "43.2")
        self.assertEqual(points[0].attrib["course"], "90.0")
        self.assertIsNotNone(points[0].find("{%s}textInfo" % NVG_NS))
        self.assertEqual(
            points[0].find("{%s}TimeStamp" % NVG_NS).text,
            "2024-07-14T23:33:20.000Z",
        )
        self.assertEqual(
            points[0].find("{%s}TimeSpan/{%s}end" % (NVG_NS, NVG_NS)).text,
            "1970-01-01T00:02:10.000Z",
        )
        extended = points[0].find("{%s}ExtendedData" % NVG_NS)
        values = {
            item.attrib["key"]: item.text
            for item in extended.findall("{%s}SimpleData" % NVG_NS)
        }
        self.assertEqual(values["Data source"], "test")
        self.assertEqual(values["Registration"], "TEST-REG")
        self.assertEqual(values["ICAO address"], "ABCDEF")
        self.assertEqual(values["Squawk / Mode 3"], "7000")
        self.assertEqual(values["Primary"], "3281 ft / 1000 m (reported)")
        self.assertEqual(values["EFDI track ID"], "EFDI-ICAO-ABCDEF")
        self.assertIn("IDENTITY", values)
        self.assertIn("KINEMATICS", values)
        self.assertIn("ALTITUDE DETAIL", values)
        self.assertIn("SYSTEM", values)
        self.assertNotIn("efdi_uid", values)

    def test_stale_items_are_removed(self):
        self.cache.upsert(self.track("stale"), "SFAPMF----*****")
        self.clock.now += 31
        body, count = self.cache.document()
        self.assertEqual(count, 0)
        self.assertEqual(ET.fromstring(body).findall("{%s}point" % NVG_NS), [])

    def test_oldest_item_is_evicted_at_capacity(self):
        self.cache.upsert(self.track("one"), "SFAPMF----*****")
        self.clock.now += 1
        self.cache.upsert(self.track("two"), "SFAPMF----*****")
        self.clock.now += 1
        self.cache.upsert(self.track("three"), "SFAPMF----*****")
        body, count = self.cache.document()
        ids = {item.attrib["uri"] for item in ET.fromstring(body).findall("{%s}point" % NVG_NS)}
        self.assertEqual(count, 2)
        self.assertEqual(ids, {"urn:efdi:EFDI-UID-TWO", "urn:efdi:EFDI-UID-THREE"})

    def test_item_specific_stale_window(self):
        self.cache.upsert(self.track("weather"), "SNGPI-----*****", stale_s=3600)
        self.clock.now += 31
        _, count = self.cache.document()
        self.assertEqual(count, 1)

    def test_offline_tombstone_removes_cached_sensor_immediately(self):
        track = self.track("offline-sensor")
        track.update(_src="dronuradaras.lt", sensor_id="DRONU-OFFLINE")
        self.cache.upsert(track, "SNGPES----*****")
        self.assertEqual(self.cache.document()[1], 1)

        removed = self.cache.remove(track)

        self.assertEqual(removed, "EFDI-SENS-DRONU-OFFLINE")
        self.assertEqual(self.cache.document()[1], 0)

    def test_feed_covers_enabled_ground_and_weather_sources(self):
        self.assertIn("land/**/neutral/station/**", _TOPIC_SIDC)
        self.assertIn("land/**/neutral/sensor/**", _TOPIC_SIDC)
        self.assertIn("land/**/neutral/radar/**", _TOPIC_SIDC)
        self.assertIn("env/weather/station/**", _TOPIC_SIDC)
        self.assertEqual(_TOPIC_SIDC["land/**/neutral/station/**"], "SNGPES----*****")
        self.assertEqual(_TOPIC_SIDC["env/weather/station/**"], "SNGPESE---*****")
        self.assertNotEqual(
            _TOPIC_SIDC["env/weather/station/**"],
            _TOPIC_SIDC["land/**/neutral/sensor/**"],
        )

    def test_air_and_sea_affiliation_matches_cot_hostile_classification(self):
        civil_air = _TOPIC_SIDC["air/**/civ/aircraft/**"]
        military_air = _TOPIC_SIDC["air/**/mil/aircraft/**"]
        civil_sea = _TOPIC_SIDC["sea/**/civ/vessel/**"]

        self.assertEqual(
            _resolve_sidc(civil_air, {"icao24": "151d4f"}), "SHAPCF----*****"
        )
        self.assertEqual(
            _resolve_sidc(military_air, {"icao24": "140001"}), "SHAPMF----*****"
        )
        self.assertEqual(
            _resolve_sidc(civil_air, {"icao24": "4ca123"}), "SNAPCF----*****"
        )
        self.assertEqual(
            _resolve_sidc(civil_air, {"emitter_category_str": "C2"}),
            "SNGPEV----*****",
        )
        self.assertEqual(
            _resolve_sidc(civil_sea, {"mmsi": "273123456"}), "SHSPXF----*****"
        )
        self.assertEqual(
            _resolve_sidc(civil_sea, {"mmsi": "257123456"}), "SNSPXF----*****"
        )

    def test_upstream_control_characters_are_removed_from_xml(self):
        track = self.track("control\x00label")
        track["comment"] = "valid\x01comment"
        self.cache.upsert(track, "SNGPES----*****")
        body, count = self.cache.document()

        point = ET.fromstring(body).find("{%s}point" % NVG_NS)
        self.assertEqual(count, 1)
        self.assertEqual(point.attrib["label"], "controllabel")
        fields = point.findall(
            "{%s}ExtendedData/{%s}SimpleData" % (NVG_NS, NVG_NS)
        )
        rows = [(item.attrib["key"], item.text) for item in fields]
        self.assertEqual(len(rows), len(set(rows)))
        self.assertFalse(any("_" in key for key, _ in rows))
        values = dict(rows)
        self.assertEqual(values["Comment"], "validcomment")

    def test_asterix_radar_attributes_reuse_the_tak_stat_card_sections(self):
        track = self.track("radar")
        track.update({
            "track_num": 42,
            "sac": 12,
            "sic": 34,
            "radar_id": "RAD-12-34",
            "range_nm": 25.5,
            "azimuth_deg": 91.2,
            "rssi_db": -8.5,
            "track_sigma_x_nm": 0.05,
            "track_sigma_y_nm": 0.08,
        })
        _, xml = track_to_nvg_item(track, "SNAPMF----*****", "2525b")
        point = ET.fromstring(xml).find("{%s}point" % NVG_NS)
        values = {
            item.attrib["key"]: item.text
            for item in point.findall(
                "{%s}ExtendedData/{%s}SimpleData" % (NVG_NS, NVG_NS)
            )
        }
        self.assertIn("RADAR", values)
        self.assertEqual(values["Radar ID"], "RAD-12-34")
        self.assertEqual(values["SAC / SIC"], "12/34")
        self.assertIn("25.5 nm", values["Range / azimuth"])
        self.assertEqual(values["Signal strength"], "-8.5 dBFS")
        self.assertIn("±0.080 nm", values["Position accuracy"])

    def test_weather_has_distinct_symbol_label_and_condition_card(self):
        track = {
            "_ts": 1_721_000_000,
            "_src": "meteo-lt",
            "place_name": "Vilnius",
            "place_code": "vilnius",
            "lat_deg": 54.687,
            "lon_deg": 25.283,
            "temperature_c": 23.8,
            "apparent_temperature_c": 24.1,
            "relative_humidity_pct": 54,
            "wind_speed_ms": 3.2,
            "wind_direction_deg": 270,
            "pressure_hpa": 1012.5,
            "cloud_cover_pct": 80,
            "condition_code": "cloudy",
        }
        _, xml = track_to_nvg_item(
            track,
            _TOPIC_SIDC["env/weather/station/**"],
            "2525b",
        )
        point = ET.fromstring(xml).find("{%s}point" % NVG_NS)
        self.assertEqual(point.attrib["label"], "Vilnius")
        self.assertEqual(point.attrib["symbol"], "2525b:SNGPESE---*****")
        values = {
            item.attrib["key"]: item.text
            for item in point.findall(
                "{%s}ExtendedData/{%s}SimpleData" % (NVG_NS, NVG_NS)
            )
        }
        self.assertIn("WEATHER", values)
        self.assertIn("CONDITIONS", values)
        self.assertEqual(values["Station"], "VILNIUS")
        self.assertEqual(values["Temperature"], "23.8 °C (feels 24.1 °C)")
        self.assertEqual(values["Humidity"], "54%")
        self.assertEqual(values["Pressure"], "1012.5 hPa")


class AircraftEnrichmentTests(unittest.TestCase):
    def test_partner_adsb_preserves_altitudes_and_detailed_data(self):
        track = {
            "_ts": 1_721_000_000,
            "_src": "partner-adsb",
            "icao24": "abcdef",
            "callsign": "TST123",
            "registration": "LY-ABC",
            "aircraft_type": "A320",
            "aircraft_description": "AIRBUS A-320",
            "pos_source": "adsb_icao",
            "lat_deg": 54.7,
            "lon_deg": 25.3,
            "alt_baro_ft": 35_000,
            "alt_geom_ft": 35_100,
            "ground_speed_kts": 450,
            "ias_kt": 260,
            "tas_kt": 440,
            "mach": 0.78,
            "track_deg": 91.5,
            "turn_rate_dps": 0.25,
            "roll_deg": 2.5,
            "mag_heading_deg": 89.0,
            "true_heading_deg": 94.0,
            "baro_vr_fpm": -640,
            "geo_vr_fpm": -576,
            "squawk": "7000",
            "emergency": "none",
            "emitter_category": "A3",
            "nav_qnh_hpa": 1013.2,
            "selected_alt_ft": 30_000,
            "fms_selected_alt_ft": 29_000,
            "selected_heading_deg": 90.0,
            "nav_modes": "autopilot, vnav",
            "nic": 8,
            "rc_m": 185,
            "nac_p": 9,
            "nac_v": 2,
            "rssi_dbfs": -7.7,
        }

        self.assertEqual(track["alt_baro_ft"], 35_000)
        self.assertEqual(track["alt_geom_ft"], 35_100)
        self.assertEqual(track["baro_vr_fpm"], -640)
        self.assertEqual(track["geo_vr_fpm"], -576)
        self.assertEqual(track["selected_alt_ft"], 30_000)
        self.assertEqual(track["fms_selected_alt_ft"], 29_000)
        self.assertEqual(track["nav_modes"], "autopilot, vnav")

        _, xml = track_to_nvg_item(track, "SNAPCF----*****", "2525b")
        point = ET.fromstring(xml).find("{%s}point" % NVG_NS)
        self.assertEqual(point.attrib["z"], "10698.5")
        self.assertIn(
            "X:GEO 35100 ft | 10698 m | BARO FL350 | 35000 ft",
            point.attrib["modifiers"],
        )
        fields = point.findall(
            "{%s}ExtendedData/{%s}SimpleData" % (NVG_NS, NVG_NS)
        )
        rows = [(item.attrib["key"], item.text) for item in fields]
        self.assertEqual(len(rows), len(set(rows)))
        self.assertFalse(any("_" in key for key, _ in rows))
        values = dict(rows)
        self.assertEqual(
            [(key, value) for key, value in rows if key == "Signal strength"],
            [("Signal strength", "-7.7 dBFS")],
        )
        self.assertEqual(values["Barometric"], "FL350 / 35000 ft / 10668 m")
        self.assertEqual(values["Geometric WGS84"], "35100 ft / 10698 m")
        self.assertEqual(
            values["Barometric vertical rate"],
            "-640 ft/min / -3.3 m/s",
        )
        self.assertEqual(values["Selected MCP/FCU"], "30000 ft / 9144 m")
        self.assertEqual(values["Autopilot modes"], "autopilot, vnav")
        self.assertEqual(values["Position source"], "adsb_icao")
        self.assertIn("ADS-B QUALITY", values)

    def test_missing_partner_adsb_measurements_remain_absent(self):
        track = {
            "_src": "partner-adsb",
            "icao24": "abcdef",
            "lat_deg": 54.7,
            "lon_deg": 25.3,
        }
        self.assertNotIn("alt_baro_ft", track)
        self.assertNotIn("alt_geom_ft", track)
        self.assertNotIn("ground_speed_kts", track)
        self.assertNotIn("track_deg", track)


class BasicAuthTests(unittest.TestCase):
    def test_exact_basic_credentials_are_required(self):
        token = base64.b64encode(b"feed-user:correct horse battery staple").decode()
        self.assertTrue(
            basic_authorized("Basic " + token, "feed-user", "correct horse battery staple")
        )
        self.assertFalse(basic_authorized("Basic " + token, "feed-user", "wrong"))
        self.assertFalse(basic_authorized(None, "feed-user", "correct horse battery staple"))


class HTTPFeedTests(unittest.TestCase):
    def setUp(self):
        self.cache = NVGFeedCache(stale_s=30, max_tracks=10)
        self.cache.upsert(NVGFeedCacheTests.track("http"), "SFAPMF----*****")
        self.server = NVGFeedServer(
            ("127.0.0.1", 0),
            self.cache,
            "/nvg",
            "feed-user",
            "feed-password",
            False,
            False,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="/nvg", method="GET", authenticated=True):
        headers = {}
        if authenticated:
            token = base64.b64encode(b"feed-user:feed-password").decode()
            headers["Authorization"] = "Basic " + token
        return urllib.request.urlopen(
            urllib.request.Request(self.base + path, headers=headers, method=method), timeout=2
        )

    def test_authentication_and_nvg_response(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(authenticated=False)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

        with self.request() as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/xml")
            points = ET.fromstring(response.read()).findall("{%s}point" % NVG_NS)
        self.assertEqual([point.attrib["uri"] for point in points], ["urn:efdi:EFDI-UID-HTTP"])

        with self.request(path="/healthz") as response:
            health = json.load(response)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["tracks"], 1)
        self.assertEqual(health["feed_requests"]["successful_requests"], 1)
        self.assertEqual(health["feed_requests"]["unauthorized_requests"], 1)
        self.assertIsNotNone(health["feed_requests"]["last_successful_request"])
        self.assertIsNotNone(health["feed_requests"]["last_unauthorized_request"])
        self.assertIsInstance(
            health["feed_requests"]["seconds_since_last_success"], float
        )

        with self.request(path="/healthz") as response:
            second_health = json.load(response)
        self.assertEqual(second_health["feed_requests"]["successful_requests"], 1)
        self.assertEqual(second_health["feed_requests"]["unauthorized_requests"], 1)

    def test_writes_are_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(method="POST")
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(caught.exception.headers["Allow"], "GET, HEAD")
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
