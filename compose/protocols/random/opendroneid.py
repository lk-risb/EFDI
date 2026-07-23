#!/usr/bin/env python3
"""ASTM F3411 / ASD-STAN Open Drone ID message translation.

The protocol carries 25-byte Basic ID, Location, Authentication, Self ID,
System, and Operator ID messages. Bluetooth 5 and Wi-Fi transports may bundle
up to nine of those messages in one Message Pack. Receiver and detection nodes
publish raw bytes through Zenoh; this process subscribes to those publications
and emits normalized records for the existing C2 output layers.

The field layout and scaling follow the Apache-2.0 OpenDroneID reference core:
https://github.com/opendroneid/opendroneid-core-c
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import struct
import sys
import threading
import time

import zenoh
from zenoh_auth import apply_zenoh_auth
from namespace_prefix import topic_root
from protocols.random.opendroneid_pb2 import OpenDroneIdTrack
from protocols.protobuf_codec import publish_dual


MESSAGE_SIZE = 25
MAX_PACK_MESSAGES = 9
MAX_TRACKS = 20_000
MAX_RAW_PAYLOAD = 3 + MESSAGE_SIZE * MAX_PACK_MESSAGES
MAX_INGRESS_ENVELOPE = 4096

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
DEFAULT_INPUT_TOPIC = "{}/raw/opendroneid/**".format(TOPIC_ROOT)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_TRANSPORT = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

TYPE_BASIC_ID = 0
TYPE_LOCATION = 1
TYPE_AUTH = 2
TYPE_SELF_ID = 3
TYPE_SYSTEM = 4
TYPE_OPERATOR_ID = 5
TYPE_PACK = 15

ID_TYPES = {
    0: "none",
    1: "serial_number",
    2: "caa_registration",
    3: "utm_uuid",
    4: "specific_session",
}

UA_TYPES = {
    0: "uav",
    1: "uav aeroplane",
    2: "uav helicopter or multirotor",
    3: "uav gyroplane",
    4: "uav hybrid lift",
    5: "uav ornithopter",
    6: "uav glider",
    7: "uav kite",
    8: "uav free balloon",
    9: "uav captive balloon",
    10: "uav airship",
    11: "uav parachute",
    12: "uav rocket",
    13: "uav tethered powered aircraft",
    14: "ground obstacle",
    15: "uav other",
}

STATUSES = {
    0: "undeclared",
    1: "ground",
    2: "airborne",
    3: "emergency",
    4: "remote_id_system_failure",
}


def _text(raw: bytes, limit: int) -> str | None:
    raw = raw.split(b"\x00", 1)[0]
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def _identifier(raw: bytes, id_type: int) -> str | None:
    if not any(raw):
        return None
    if id_type in (1, 2):
        return _text(raw, 20)
    return raw.hex()


def _altitude(encoded: int) -> float | None:
    if encoded == 0:
        return None
    value = encoded * 0.5 - 1000.0
    return round(value, 2) if math.isfinite(value) else None


def _lat_lon(encoded: int, latitude: bool) -> float | None:
    value = encoded / 10_000_000.0
    limit = 90 if latitude else 180
    return round(value, 7) if -limit <= value <= limit else None


def decode_message(message: bytes) -> tuple[int, dict] | None:
    """Decode one exact 25-byte Open Drone ID message."""
    if len(message) != MESSAGE_SIZE:
        return None
    message_type = message[0] >> 4
    version = message[0] & 0x0F

    if message_type == TYPE_BASIC_ID:
        id_type = message[1] >> 4
        ua_type = message[1] & 0x0F
        uas_id = _identifier(message[2:22], id_type)
        result = {
            "remote_id_protocol_version": version,
            "remote_id_id_type": ID_TYPES.get(id_type, str(id_type)),
            "remote_id_ua_type": UA_TYPES.get(ua_type, "uav type {}".format(ua_type)),
        }
        if uas_id:
            result["remote_id_uas_id"] = uas_id
        return message_type, result

    if message_type == TYPE_LOCATION:
        flags = message[1]
        status = flags >> 4
        speed_multiplier = flags & 0x01
        direction = message[2] + (180 if flags & 0x02 else 0)
        speed_raw = message[3]
        vertical_raw = struct.unpack_from("<b", message, 4)[0]
        latitude_raw, longitude_raw = struct.unpack_from("<ii", message, 5)
        latitude = _lat_lon(latitude_raw, True)
        longitude = _lat_lon(longitude_raw, False)
        if latitude is None or longitude is None or (latitude_raw == 0 and longitude_raw == 0):
            return None

        result = {
            "remote_id_protocol_version": version,
            "remote_id_status": STATUSES.get(status, str(status)),
            "remote_id_height_reference": "ground" if flags & 0x04 else "takeoff",
            "lat_deg": latitude,
            "lon_deg": longitude,
            "remote_id_horizontal_accuracy": message[19] & 0x0F,
            "remote_id_vertical_accuracy": message[19] >> 4,
            "remote_id_speed_accuracy": message[20] & 0x0F,
            "remote_id_barometer_accuracy": message[20] >> 4,
            "remote_id_timestamp_accuracy": message[23] & 0x0F,
            "on_ground": status == 1,
            "emergency": status == 3,
        }
        if direction != 361:
            result["heading_deg"] = float(direction)
        if speed_raw != 255:
            speed = speed_raw * (0.75 if speed_multiplier else 0.25)
            if speed_multiplier:
                speed += 63.75
            result["speed_ms"] = round(speed, 2)
        if vertical_raw != 126:
            result["vertical_rate_ms"] = round(vertical_raw * 0.5, 2)

        for key, offset in (("baro_alt_m", 13), ("geo_alt_m", 15), ("height_m", 17)):
            altitude = _altitude(struct.unpack_from("<H", message, offset)[0])
            if altitude is not None:
                result[key] = altitude
        timestamp = struct.unpack_from("<H", message, 21)[0]
        if timestamp != 0xFFFF:
            result["remote_id_seconds_after_hour"] = round(timestamp / 10.0, 1)
        return message_type, result

    if message_type == TYPE_AUTH:
        auth_type = message[1] >> 4
        page = message[1] & 0x0F
        result = {
            "remote_id_protocol_version": version,
            "remote_id_auth_type": auth_type,
            "remote_id_auth_page": page,
        }
        if page == 0:
            last_page = message[2]
            length = message[3]
            if last_page > 15 or length > 17 + last_page * 23:
                return None
            result.update(
                {
                    "remote_id_auth_last_page": last_page,
                    "remote_id_auth_length": length,
                    "remote_id_auth_timestamp": struct.unpack_from("<I", message, 4)[0],
                }
            )
        return message_type, result

    if message_type == TYPE_SELF_ID:
        result = {
            "remote_id_protocol_version": version,
            "remote_id_description_type": message[1],
        }
        description = _text(message[2:25], 23)
        if description:
            result["remote_id_description"] = description
        return message_type, result

    if message_type == TYPE_SYSTEM:
        flags = message[1]
        operator_lat_raw, operator_lon_raw = struct.unpack_from("<ii", message, 2)
        result = {
            "remote_id_protocol_version": version,
            "remote_id_operator_location_type": flags & 0x03,
            "remote_id_classification_type": (flags >> 2) & 0x07,
            "remote_id_area_count": struct.unpack_from("<H", message, 10)[0],
            "remote_id_area_radius_m": message[12] * 10,
            "remote_id_category_eu": message[17] >> 4,
            "remote_id_class_eu": message[17] & 0x0F,
            "remote_id_system_timestamp": struct.unpack_from("<I", message, 20)[0],
        }
        if operator_lat_raw or operator_lon_raw:
            operator_lat = _lat_lon(operator_lat_raw, True)
            operator_lon = _lat_lon(operator_lon_raw, False)
            if operator_lat is not None and operator_lon is not None:
                result["remote_id_operator_lat_deg"] = operator_lat
                result["remote_id_operator_lon_deg"] = operator_lon
        for key, offset in (
            ("remote_id_area_ceiling_m", 13),
            ("remote_id_area_floor_m", 15),
            ("remote_id_operator_alt_m", 18),
        ):
            altitude = _altitude(struct.unpack_from("<H", message, offset)[0])
            if altitude is not None:
                result[key] = altitude
        return message_type, result

    if message_type == TYPE_OPERATOR_ID:
        result = {
            "remote_id_protocol_version": version,
            "remote_id_operator_id_type": message[1],
        }
        operator_id = _text(message[2:22], 20)
        if operator_id:
            result["remote_id_operator_id"] = operator_id
        return message_type, result

    return None


def decode_payload(payload: bytes) -> list[tuple[int, dict]]:
    """Decode one message or one bounded Message Pack."""
    if len(payload) == MESSAGE_SIZE:
        decoded = decode_message(payload)
        return [decoded] if decoded else []
    if len(payload) < 3 or payload[0] >> 4 != TYPE_PACK:
        return []
    message_size = payload[1]
    count = payload[2]
    required = 3 + message_size * count
    if message_size != MESSAGE_SIZE or not 1 <= count <= MAX_PACK_MESSAGES or len(payload) != required:
        return []
    decoded_messages = []
    for index in range(count):
        start = 3 + index * MESSAGE_SIZE
        decoded = decode_message(payload[start:start + MESSAGE_SIZE])
        if decoded is None:
            return []
        decoded_messages.append(decoded)
    return decoded_messages


class RemoteIDTracker:
    """Aggregate independently broadcast message types into normalized tracks."""

    def __init__(self, friendly_ids=(), max_tracks: int = MAX_TRACKS):
        self._friendly_ids = {value.casefold() for value in friendly_ids if value}
        self._max_tracks = max_tracks
        self._states: dict[str, dict] = {}

    @staticmethod
    def _uid(identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]
        return "remote-id-{}".format(digest)

    def _build_track(self, source_id: str, state: dict, now: float) -> dict:
        uas_id = state.get("remote_id_uas_id")
        identity = uas_id or source_id
        affiliation = "friendly" if uas_id and uas_id.casefold() in self._friendly_ids else "unknown"
        track = {
            key: value for key, value in state.items()
            if not key.startswith("_")
        }
        track.update(
            {
                "_src": "Open Drone ID",
                "_ts": now,
                "uid": self._uid(identity),
                "callsign": uas_id or "RID-{}".format(source_id.replace(":", "")[-8:]),
                "classification": state.get("remote_id_ua_type", "uav"),
                "remote_id_transmitter": source_id[:128],
                "affiliation": affiliation,
            }
        )
        return track

    def ingest(
        self,
        source_id: str,
        payload: bytes,
        *,
        transport: str,
        rssi_dbm: int | None = None,
        receiver_id: str | None = None,
        now: float | None = None,
    ) -> list[dict]:
        now = time.time() if now is None else now
        source_id = source_id[:128]
        decoded = decode_payload(payload)
        if not decoded:
            return []
        if source_id not in self._states and len(self._states) >= self._max_tracks:
            oldest = min(self._states, key=lambda key: self._states[key].get("_last_seen", 0))
            self._states.pop(oldest, None)
        state = self._states.setdefault(source_id, {})
        previous_uid = state.get("_published_uid")
        previous_identity = state.get("remote_id_uas_id")
        saw_location = False
        for message_type, fields in decoded:
            state.update(fields)
            saw_location = saw_location or message_type == TYPE_LOCATION
        state["_last_seen"] = now
        state["remote_id_transport"] = transport[:32]
        if receiver_id:
            state["remote_id_receiver"] = receiver_id[:128]
        if rssi_dbm is not None and -200 <= rssi_dbm <= 100:
            state["remote_id_rssi_dbm"] = int(rssi_dbm)
        if saw_location:
            state["_last_location"] = now

        identity_changed = previous_identity != state.get("remote_id_uas_id")
        if "lat_deg" not in state or not (saw_location or identity_changed):
            return []
        track = self._build_track(source_id, state, now)
        updates = []
        if previous_uid and previous_uid != track["uid"]:
            old_track = dict(state.get("_last_track", track))
            old_track.update({"uid": previous_uid, "_ts": now, "_delete": True})
            updates.append(old_track)
        state["_published_uid"] = track["uid"]
        state["_last_track"] = dict(track)
        updates.append(track)
        return updates

    def expire(self, stale_seconds: float, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        tombstones = []
        for source_id, state in list(self._states.items()):
            last_position = state.get("_last_location", state.get("_last_seen", now))
            if now - last_position <= stale_seconds:
                continue
            if state.get("_published_uid") and state.get("_last_track"):
                tombstone = dict(state["_last_track"])
                tombstone.update({"_ts": now, "_delete": True})
                tombstones.append(tombstone)
            self._states.pop(source_id, None)
        return tombstones


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5(
            "transport/link/tls",
            json.dumps(
                {
                    "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
                    "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
                    "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
                    "enable_mtls": True,
                    "verify_name_on_connect": True,
                }
            ),
        )
    return conf


def decode_ingress(key_expr: str, payload: bytes):
    """Validate one raw Zenoh publication and return its receiver metadata."""
    if not payload or len(payload) > MAX_INGRESS_ENVELOPE:
        return None
    parts = str(key_expr).rstrip("/").split("/")
    if len(parts) < 2:
        return None
    receiver_id, source_id = parts[-2], parts[-1]
    if not _SAFE_ID.fullmatch(receiver_id) or not _SAFE_ID.fullmatch(source_id):
        return None
    transport = "zenoh"
    rssi_dbm = None
    raw = payload

    if payload.lstrip().startswith(b"{"):
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(envelope, dict) or set(envelope) - {
            "payload_b64", "payload_hex", "source_id", "transport", "rssi_dbm"
        }:
            return None
        encodings = [name for name in ("payload_b64", "payload_hex") if name in envelope]
        if len(encodings) != 1 or not isinstance(envelope[encodings[0]], str):
            return None
        try:
            if encodings[0] == "payload_b64":
                raw = base64.b64decode(envelope["payload_b64"], validate=True)
            else:
                raw = bytes.fromhex(envelope["payload_hex"])
        except (ValueError, binascii.Error):
            return None
        if "source_id" in envelope:
            if not isinstance(envelope["source_id"], str) or not _SAFE_ID.fullmatch(envelope["source_id"]):
                return None
            source_id = envelope["source_id"]
        if "transport" in envelope:
            if not isinstance(envelope["transport"], str) or not _SAFE_TRANSPORT.fullmatch(envelope["transport"]):
                return None
            transport = envelope["transport"]
        if "rssi_dbm" in envelope:
            value = envelope["rssi_dbm"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not -200 <= value <= 100:
                return None
            rssi_dbm = int(value)

    if len(raw) > MAX_RAW_PAYLOAD or not decode_payload(raw):
        return None
    return receiver_id, source_id, raw, transport, rssi_dbm


def _publish_track(session, track: dict) -> None:
    affiliation = track.get("affiliation", "unknown")
    topic = "{}/air/opendroneid/passive_rf/{}/uav".format(
        TOPIC_ROOT, affiliation
    )
    publish_dual(session, topic, track, OpenDroneIdTrack, zenoh)


def make_handler(tracker: RemoteIDTracker, session, verbose: bool = False, lock=None):
    state_lock = lock or threading.Lock()

    def on_sample(sample) -> None:
        decoded = decode_ingress(str(sample.key_expr), bytes(sample.payload))
        if decoded is None:
            if verbose:
                print("OpenDroneID ignored invalid raw publication:", sample.key_expr, flush=True)
            return
        receiver_id, source_id, raw, transport, rssi_dbm = decoded
        with state_lock:
            updates = tracker.ingest(
                source_id,
                raw,
                transport=transport,
                rssi_dbm=rssi_dbm,
                receiver_id=receiver_id,
            )
        for track in updates:
            _publish_track(session, track)
            if verbose:
                print("OpenDroneID", track.get("callsign"), track.get("lat_deg"), track.get("lon_deg"), flush=True)

    return on_sample


def run(args) -> None:
    friendly_ids = [value.strip() for value in args.friendly_ids.split(",") if value.strip()]
    tracker = RemoteIDTracker(friendly_ids=friendly_ids)
    lock = threading.Lock()
    session = zenoh.open(make_config())
    subscriber = session.declare_subscriber(
        args.input_topic,
        make_handler(tracker, session, args.verbose, lock),
    )
    print("OpenDroneID raw Zenoh input:", args.input_topic, flush=True)
    print("OpenDroneID normalized output: {}/air/opendroneid/passive_rf/**".format(TOPIC_ROOT), flush=True)
    try:
        while True:
            time.sleep(5)
            with lock:
                tombstones = tracker.expire(args.stale_seconds)
            for tombstone in tombstones:
                _publish_track(session, tombstone)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Raw Open Drone ID Zenoh translator")
    parser.add_argument("--decode-hex", help="decode one message or Message Pack and exit")
    parser.add_argument(
        "--input-topic",
        default=os.environ.get("OPENDRONEID_INPUT_TOPIC") or DEFAULT_INPUT_TOPIC,
    )
    parser.add_argument("--friendly-ids", default=os.environ.get("OPENDRONEID_FRIENDLY_IDS", ""))
    parser.add_argument(
        "--stale-seconds",
        type=float,
        default=float(os.environ.get("OPENDRONEID_STALE_S", "30")),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not 5 <= args.stale_seconds <= 3600:
        parser.error("stale seconds must be between 5 and 3600")
    if args.decode_hex is not None:
        value = args.decode_hex if args.decode_hex != "-" else sys.stdin.read().strip()
        try:
            payload = bytes.fromhex(value)
        except ValueError as exc:
            parser.error("invalid hex payload: {}".format(exc))
        decoded = decode_payload(payload)
        if not decoded:
            raise SystemExit("invalid Open Drone ID payload")
        print(json.dumps([fields for _, fields in decoded], indent=2, sort_keys=True))
        return
    run(args)


if __name__ == "__main__":
    main()
