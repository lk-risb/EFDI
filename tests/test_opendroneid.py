#!/usr/bin/env python3
"""Open Drone ID Zenoh protocol translation tests."""

import pathlib
import base64
import json
import struct
import sys
import unittest
import xml.etree.ElementTree as ET


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "protocols"))
sys.path.insert(0, str(ROOT / "compose" / "layers"))

from protocols.random.opendroneid import (  # noqa: E402
    RemoteIDTracker,
    TYPE_BASIC_ID,
    TYPE_LOCATION,
    decode_ingress,
    decode_message,
    decode_payload,
    make_handler as make_opendroneid_handler,
)
from cot_layer import _unknown_air_type, make_handler  # noqa: E402
from nvg_layer import _unknown_air_sidc, track_to_nvg_item  # noqa: E402


def basic_id(uas_id="DJI-MAVIC-3E-001"):
    message = bytearray(25)
    message[0] = 0x02  # Basic ID, protocol version 2
    message[1] = 0x12  # serial-number ID, multirotor
    encoded = uas_id.encode("ascii")[:20]
    message[2:2 + len(encoded)] = encoded
    return bytes(message)


def location():
    message = bytearray(25)
    message[0] = 0x12  # Location, protocol version 2
    message[1] = 0x20  # airborne, takeoff-height reference, low speed multiplier
    message[2] = 91
    message[3] = 40  # 10 m/s
    struct.pack_into("<b", message, 4, -4)  # -2 m/s
    struct.pack_into("<ii", message, 5, 546872000, 252797000)
    struct.pack_into("<HHH", message, 13, 2200, 2210, 2100)
    message[19] = 0x6C
    message[20] = 0x53
    struct.pack_into("<H", message, 21, 1234)
    message[23] = 5
    return bytes(message)


def message_pack():
    return bytes((0xF2, 25, 2)) + basic_id() + location()


class OpenDroneIDProtocolTests(unittest.TestCase):
    def test_decodes_basic_id_and_scaled_location(self):
        message_type, identity = decode_message(basic_id())
        self.assertEqual(message_type, TYPE_BASIC_ID)
        self.assertEqual(identity["remote_id_uas_id"], "DJI-MAVIC-3E-001")
        self.assertIn("multirotor", identity["remote_id_ua_type"])

        message_type, position = decode_message(location())
        self.assertEqual(message_type, TYPE_LOCATION)
        self.assertEqual(position["lat_deg"], 54.6872)
        self.assertEqual(position["lon_deg"], 25.2797)
        self.assertEqual(position["heading_deg"], 91.0)
        self.assertEqual(position["speed_ms"], 10.0)
        self.assertEqual(position["vertical_rate_ms"], -2.0)
        self.assertEqual(position["baro_alt_m"], 100.0)
        self.assertEqual(position["geo_alt_m"], 105.0)
        self.assertEqual(position["height_m"], 50.0)
        self.assertEqual(position["remote_id_status"], "airborne")

    def test_message_pack_aggregates_to_friendly_c2_track(self):
        tracker = RemoteIDTracker(friendly_ids=["DJI-MAVIC-3E-001"])
        updates = tracker.ingest(
            "aa:bb:cc:dd:ee:ff",
            message_pack(),
            transport="bluetooth-le",
            rssi_dbm=-52,
            now=1000.0,
        )
        self.assertEqual(len(updates), 1)
        track = updates[0]
        self.assertEqual(track["affiliation"], "friendly")
        self.assertEqual(track["callsign"], "DJI-MAVIC-3E-001")
        self.assertEqual(track["_src"], "Open Drone ID")
        self.assertEqual(track["remote_id_rssi_dbm"], -52)
        self.assertEqual(track["lat_deg"], 54.6872)

        tombstones = tracker.expire(30, now=1031.0)
        self.assertEqual(len(tombstones), 1)
        self.assertTrue(tombstones[0]["_delete"])

    def test_unknown_track_maps_to_uav_in_tak_and_sitaware(self):
        track = {"remote_id_ua_type": "uav helicopter or multirotor"}
        self.assertEqual(_unknown_air_type(track), "a-u-A-M-F-Q")
        self.assertEqual(_unknown_air_sidc(track), "SUAPMFQ---*****")

    def test_normalized_track_becomes_tak_cot_event(self):
        class Sender:
            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(message)

        class Sample:
            key_expr = "test/air/opendroneid/astm-f3411/unknown/uav/tracks/v1"

            def __init__(self, track):
                self.payload = json.dumps(track).encode()

        tracker = RemoteIDTracker()
        track = tracker.ingest(
            "aa:bb:cc:dd:ee:ff",
            message_pack(),
            transport="wifi-beacon",
            now=1000.0,
        )[0]
        sender = Sender()
        make_handler(_unknown_air_type, sender, verbose=False)(Sample(track))

        self.assertEqual(len(sender.messages), 1)
        event = ET.fromstring(sender.messages[0])
        self.assertEqual(event.attrib["type"], "a-u-A-M-F-Q")
        self.assertEqual(event.find("detail/contact").attrib["callsign"], "DJI-MAVIC-3E-001")

        _, nvg_xml = track_to_nvg_item(track, _unknown_air_sidc(track))
        nvg = ET.fromstring(nvg_xml)
        point = next(element for element in nvg if element.tag.endswith("point"))
        self.assertEqual(point.attrib["symbol"], "app6c:SUAPMFQ---*****")
        self.assertEqual(point.attrib["label"], "DJI-MAVIC-3E-001")

    def test_rejects_truncated_or_recursive_pack(self):
        self.assertEqual(decode_payload(bytes((0xF2, 25, 2)) + basic_id()), [])
        recursive = bytes((0xF2, 25, 1)) + bytes((0xF2, 25, 1)) + bytes(22)
        self.assertEqual(decode_payload(recursive), [])

    def test_raw_zenoh_binary_is_translated_to_normalized_topic(self):
        class Sample:
            key_expr = "LTU/CISB/partner/raw/opendroneid/sensor-7/aa:bb:cc:dd:ee:ff"
            payload = message_pack()

        class Session:
            def __init__(self):
                self.publications = []
                self.protobuf = []

            def put(self, topic, payload, **_kwargs):
                # Dual publish: /v1 carries JSON, /v2 the protobuf sibling.
                if topic.endswith("/v2"):
                    self.protobuf.append((topic, payload))
                else:
                    self.publications.append((topic, json.loads(payload)))

        session = Session()
        make_opendroneid_handler(RemoteIDTracker(), session)(Sample())

        self.assertEqual(len(session.publications), 1)
        self.assertEqual(len(session.protobuf), 1)
        self.assertGreater(len(session.protobuf[0][1]), 0)
        topic, track = session.publications[0]
        self.assertEqual(session.protobuf[0][0], topic[: -len("/v1")] + "/v2")
        self.assertTrue(topic.endswith("/air/opendroneid/astm-f3411/unknown/uav/tracks/v1"))
        self.assertEqual(track["remote_id_receiver"], "sensor-7")
        self.assertEqual(track["remote_id_transmitter"], "aa:bb:cc:dd:ee:ff")

    def test_raw_zenoh_json_envelope_preserves_receiver_metadata(self):
        envelope = json.dumps(
            {
                "payload_b64": base64.b64encode(message_pack()).decode(),
                "source_id": "uas-radio-42",
                "transport": "vendor-rid",
                "rssi_dbm": -61,
            }
        ).encode()
        decoded = decode_ingress(
            "LTU/CISB/partner/raw/opendroneid/receiver-west/transmitter-token",
            envelope,
        )
        self.assertIsNotNone(decoded)
        receiver_id, source_id, raw, transport, rssi = decoded
        self.assertEqual(receiver_id, "receiver-west")
        self.assertEqual(source_id, "uas-radio-42")
        self.assertEqual(raw, message_pack())
        self.assertEqual(transport, "vendor-rid")
        self.assertEqual(rssi, -61)

    def test_raw_zenoh_ingress_is_bounded_and_strict(self):
        key = "LTU/CISB/partner/raw/opendroneid/rx/source"
        self.assertIsNone(decode_ingress(key, b"x" * 4097))
        self.assertIsNone(
            decode_ingress(
                key,
                json.dumps(
                    {
                        "payload_b64": base64.b64encode(message_pack()).decode(),
                        "payload_hex": message_pack().hex(),
                    }
                ).encode(),
            )
        )
if __name__ == "__main__":
    unittest.main()
