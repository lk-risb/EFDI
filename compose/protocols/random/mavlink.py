#!/usr/bin/env python3
"""MAVLink telemetry and OPEN_DRONE_ID protocol → Zenoh.

Listens on UDP (or TCP) for MAVLink v1/v2 frames from ArduPilot/PX4 UAVs
or GCS software, decodes position and attitude, and publishes each vehicle
as a JSON track to the EFDI Zenoh fabric.

Supported messages:
  HEARTBEAT (0)            — vehicle type; primes per-sysid state
  GLOBAL_POSITION_INT (33) — WGS-84 lat/lon/alt, ground velocity
  VFR_HUD (74)             — groundspeed and heading (fallback)
  OPEN_DRONE_ID_* (12900-12905) — ASTM F3411 / ASD-STAN Remote ID

Config (compose/.env):
  MAVLINK_PORT=14550   UDP port (14550 = MAVLink GCS default)
  MAVLINK_TCP=         Set to 1 for TCP mode

Run:
  venv/bin/python3 protocols/random/mavlink.py --port 14550
  venv/bin/python3 protocols/random/mavlink.py --port 14550 --tcp
"""

import argparse
import json
import math
import os
import socket
import struct
import time

import zenoh
from zenoh_auth import apply_zenoh_auth
from namespace_prefix import topic_root
from protocols.random.mavlink_pb2 import MavlinkTrack
from protocols.protobuf_codec import publish_dual

ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

MAV_STX_V1 = 0xFE
MAV_STX_V2 = 0xFD

MSG_HEARTBEAT       = 0
MSG_GLOBAL_POSITION = 33
MSG_VFR_HUD         = 74
MSG_ODID_BASIC_ID   = 12900
MSG_ODID_LOCATION   = 12901
MSG_ODID_SELF_ID    = 12903
MSG_ODID_SYSTEM     = 12904
MSG_ODID_OPERATOR_ID = 12905

# X25 CRC extra byte per message ID (from MAVLink XML definitions)
_CRC_EXTRA = {
    0: 50,
    33: 104,
    74: 20,
    12900: 114,
    12901: 254,
    12903: 249,
    12904: 77,
    12905: 49,
}

_MAV_TYPE = {
    0: "generic", 1: "fixed-wing", 2: "quadrotor", 3: "coaxial",
    4: "helicopter", 13: "hexarotor", 14: "octorotor", 19: "vtol",
}

_ODID_UA_TYPE = {
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

_ODID_ID_TYPE = {
    0: "none",
    1: "serial_number",
    2: "caa_registration",
    3: "utm_uuid",
    4: "specific_session",
}

_ODID_STATUS = {
    0: "undeclared",
    1: "ground",
    2: "airborne",
    3: "emergency",
    4: "remote_id_system_failure",
}


def _crc_accumulate(b: int, crc: int) -> int:
    tmp = b ^ (crc & 0xff)
    tmp ^= (tmp << 4) & 0xff
    return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xffff


def _crc_msg(header_and_payload: bytes, crc_extra: int) -> int:
    crc = 0xffff
    for b in header_and_payload:
        crc = _crc_accumulate(b, crc)
    return _crc_accumulate(crc_extra, crc)


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


class _Vehicle:
    def __init__(self, sysid: int):
        self.sysid = sysid
        self.track = {
            "_src":     "MAVLink",
            "uid":      "mav-sysid-{}".format(sysid),
            "callsign": "UAV-{:03d}".format(sysid),
        }

    def apply_heartbeat(self, payload: bytes):
        if len(payload) >= 5:
            mav_type = payload[4]
            self.track["mav_type"] = _MAV_TYPE.get(mav_type, str(mav_type))

    def apply_global_pos(self, payload: bytes) -> bool:
        # <IiiiihhhH: time_boot_ms, lat, lon, alt, relative_alt, vx, vy, vz, hdg
        if len(payload) < 28:
            return False
        _, lat, lon, alt, _, vx, vy, _, hdg = struct.unpack_from("<IiiiihhhH", payload)
        self.track["_ts"]     = time.time()
        self.track["lat_deg"] = round(lat / 1e7, 6)
        self.track["lon_deg"] = round(lon / 1e7, 6)
        self.track["alt_m"]   = round(alt / 1000, 1)
        speed = math.sqrt((vx / 100) ** 2 + (vy / 100) ** 2)
        self.track["speed_ms"] = round(speed, 2)
        if hdg != 65535:
            self.track["heading_deg"] = round(hdg / 100, 1)
        return True

    def apply_vfr_hud(self, payload: bytes):
        # <ffffhH: airspeed, groundspeed, alt, climb, heading, throttle
        if len(payload) < 20:
            return
        ias, gs, alt, climb, hdg, _ = struct.unpack_from("<ffffhH", payload)
        if "speed_ms" not in self.track:
            self.track["speed_ms"] = round(float(gs), 2)
        if float(ias) > 0:
            self.track["airspeed_ms"] = round(float(ias), 2)
        if "heading_deg" not in self.track:
            self.track["heading_deg"] = float(hdg)
        self.track["vertical_rate_ms"] = round(float(climb), 2)

    def topic(self) -> str:
        return "{}/air/mavlink/uav/civ/aircraft/tracks/v1".format(TOPIC_ROOT)


def _bounded_text(value: bytes, limit: int) -> str | None:
    value = value.split(b"\x00", 1)[0]
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = " ".join(text.split())
    return text[:limit] if text else None


def _remote_id_key(sysid: int, payload: bytes, offset: int) -> str:
    identity = payload[offset:offset + 20]
    return identity.hex() if any(identity) else "mavlink-sysid-{}".format(sysid)


class _RemoteID:
    """Aggregate the MAVLink Open Drone ID message family by receiver identity."""

    def __init__(self, key: str):
        self.key = key
        self.track = {
            "_src": "Open Drone ID",
            "uid": "remote-id-{}".format(key),
            "callsign": "RID-{}".format(key[-8:]),
            "remote_id_receiver_key": key,
            "remote_id_ua_type": "uav",
        }

    def apply_basic_id(self, payload: bytes) -> None:
        if len(payload) < 44:
            return
        id_type = payload[22]
        ua_type = payload[23]
        uas_id = _bounded_text(payload[24:44], 20)
        self.track["remote_id_id_type"] = _ODID_ID_TYPE.get(id_type, str(id_type))
        self.track["remote_id_ua_type"] = _ODID_UA_TYPE.get(ua_type, "uav type {}".format(ua_type))
        if uas_id:
            self.track["remote_id_uas_id"] = uas_id
            self.track["callsign"] = uas_id

    def apply_location(self, payload: bytes) -> bool:
        if len(payload) < 59:
            return False
        latitude, longitude = struct.unpack_from("<ii", payload, 0)
        if latitude == 0 and longitude == 0:
            return False
        lat_deg = latitude / 1e7
        lon_deg = longitude / 1e7
        if not (-90 <= lat_deg <= 90 and -180 <= lon_deg <= 180):
            return False
        altitude_barometric, altitude_geodetic, height, rid_timestamp = struct.unpack_from(
            "<ffff", payload, 8
        )
        direction, speed_horizontal, speed_vertical = struct.unpack_from("<HHh", payload, 24)
        status = payload[52]
        self.track.update(
            {
                "_ts": time.time(),
                "lat_deg": round(lat_deg, 7),
                "lon_deg": round(lon_deg, 7),
                "speed_ms": round(speed_horizontal / 100.0, 2),
                "vertical_rate_ms": round(speed_vertical / 100.0, 2),
                "remote_id_status": _ODID_STATUS.get(status, str(status)),
                "remote_id_height_reference": "ground" if payload[53] == 1 else "takeoff",
                "remote_id_horizontal_accuracy": payload[54],
                "remote_id_vertical_accuracy": payload[55],
                "remote_id_barometer_accuracy": payload[56],
                "remote_id_speed_accuracy": payload[57],
                "remote_id_timestamp_accuracy": payload[58],
            }
        )
        if direction != 36100:
            self.track["heading_deg"] = round(direction / 100.0, 2)
        if speed_horizontal == 25500:
            self.track.pop("speed_ms", None)
        if speed_vertical == 6300:
            self.track.pop("vertical_rate_ms", None)
        if altitude_barometric != -1000.0 and math.isfinite(altitude_barometric):
            self.track["baro_alt_m"] = round(altitude_barometric, 2)
        if altitude_geodetic != -1000.0 and math.isfinite(altitude_geodetic):
            self.track["geo_alt_m"] = round(altitude_geodetic, 2)
        if height != -1000.0 and math.isfinite(height):
            self.track["height_m"] = round(height, 2)
        if rid_timestamp != 65535.0 and math.isfinite(rid_timestamp):
            self.track["remote_id_seconds_after_hour"] = round(rid_timestamp, 3)
        self.track["on_ground"] = status == 1
        self.track["emergency"] = status == 3
        return True

    def apply_self_id(self, payload: bytes) -> None:
        if len(payload) < 46:
            return
        description = _bounded_text(payload[23:46], 23)
        if description:
            self.track["remote_id_description"] = description
        self.track["remote_id_description_type"] = payload[22]

    def apply_system(self, payload: bytes) -> None:
        if len(payload) < 54:
            return
        operator_lat, operator_lon = struct.unpack_from("<ii", payload, 0)
        area_ceiling, area_floor, operator_altitude = struct.unpack_from("<fff", payload, 8)
        timestamp = struct.unpack_from("<I", payload, 20)[0]
        area_count, area_radius = struct.unpack_from("<HH", payload, 24)
        if operator_lat or operator_lon:
            self.track["remote_id_operator_lat_deg"] = round(operator_lat / 1e7, 7)
            self.track["remote_id_operator_lon_deg"] = round(operator_lon / 1e7, 7)
        if operator_altitude != -1000.0 and math.isfinite(operator_altitude):
            self.track["remote_id_operator_alt_m"] = round(operator_altitude, 2)
        if area_ceiling != -1000.0 and math.isfinite(area_ceiling):
            self.track["remote_id_area_ceiling_m"] = round(area_ceiling, 2)
        if area_floor != -1000.0 and math.isfinite(area_floor):
            self.track["remote_id_area_floor_m"] = round(area_floor, 2)
        self.track["remote_id_area_count"] = area_count
        self.track["remote_id_area_radius_m"] = area_radius
        self.track["remote_id_operator_location_type"] = payload[50]
        self.track["remote_id_classification_type"] = payload[51]
        self.track["remote_id_category_eu"] = payload[52]
        self.track["remote_id_class_eu"] = payload[53]
        self.track["remote_id_system_timestamp"] = timestamp

    def apply_operator_id(self, payload: bytes) -> None:
        if len(payload) < 43:
            return
        operator_id = _bounded_text(payload[23:43], 20)
        if operator_id:
            self.track["remote_id_operator_id"] = operator_id
        self.track["remote_id_operator_id_type"] = payload[22]

    def topic(self) -> str:
        return "{}/air/mavlink/remote-id/unknown/uav/tracks/v1".format(TOPIC_ROOT)


MAX_REMOTE_ID_TRACKS = 20_000


class _MAVParser:
    """Stateful byte-stream parser for MAVLink v1 and v2."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        self._buf += data
        out = []
        while self._buf:
            stx = self._buf[0]

            if stx == MAV_STX_V1:
                if len(self._buf) < 6:
                    break
                plen  = self._buf[1]
                total = 6 + plen + 2
                if len(self._buf) < total:
                    break
                frame   = self._buf[:total]
                msgid   = frame[5]
                sysid   = frame[3]
                payload = bytes(frame[6:6 + plen])
                if msgid in _CRC_EXTRA:
                    calc = _crc_msg(bytes(frame[1:6 + plen]), _CRC_EXTRA[msgid])
                    recv = frame[-2] | (frame[-1] << 8)
                    if calc == recv:
                        out.append((sysid, msgid, payload))
                del self._buf[:total]

            elif stx == MAV_STX_V2:
                if len(self._buf) < 10:
                    break
                plen   = self._buf[1]
                incompat = self._buf[2]
                msgid  = self._buf[7] | (self._buf[8] << 8) | (self._buf[9] << 16)
                sysid  = self._buf[5]
                sig    = 13 if (incompat & 0x01) else 0
                total  = 10 + plen + 2 + sig
                if len(self._buf) < total:
                    break
                frame   = self._buf[:total]
                payload = bytes(frame[10:10 + plen])
                if msgid in _CRC_EXTRA:
                    calc = _crc_msg(bytes(frame[1:10 + plen]), _CRC_EXTRA[msgid])
                    recv = frame[10 + plen] | (frame[10 + plen + 1] << 8)
                    if calc == recv:
                        out.append((sysid, msgid, payload))
                del self._buf[:total]

            else:
                del self._buf[0]

        return out


def _dispatch(msgs, vehicles: dict, remote_ids: dict, session: "zenoh.Session", verbose: bool):
    for sysid, msgid, payload in msgs:
        remote_offset = {
            MSG_ODID_BASIC_ID: 2,
            MSG_ODID_LOCATION: 32,
            MSG_ODID_SELF_ID: 2,
            MSG_ODID_SYSTEM: 30,
            MSG_ODID_OPERATOR_ID: 2,
        }.get(msgid)
        if remote_offset is not None:
            key = _remote_id_key(sysid, payload, remote_offset)
            rid = remote_ids.get(key)
            if rid is None:
                if len(remote_ids) >= MAX_REMOTE_ID_TRACKS:
                    remote_ids.pop(next(iter(remote_ids)))
                rid = remote_ids[key] = _RemoteID(key)
            publish = False
            if msgid == MSG_ODID_BASIC_ID:
                rid.apply_basic_id(payload)
            elif msgid == MSG_ODID_LOCATION:
                publish = rid.apply_location(payload)
            elif msgid == MSG_ODID_SELF_ID:
                rid.apply_self_id(payload)
            elif msgid == MSG_ODID_SYSTEM:
                rid.apply_system(payload)
            elif msgid == MSG_ODID_OPERATOR_ID:
                rid.apply_operator_id(payload)
            if publish:
                publish_dual(session, rid.topic(), rid.track, MavlinkTrack, zenoh)
                if verbose:
                    print(
                        "Remote ID {} lat={} lon={}".format(
                            rid.track.get("callsign"),
                            rid.track.get("lat_deg"),
                            rid.track.get("lon_deg"),
                        ),
                        flush=True,
                    )
            continue

        if sysid not in vehicles:
            vehicles[sysid] = _Vehicle(sysid)
        v = vehicles[sysid]

        if msgid == MSG_HEARTBEAT:
            v.apply_heartbeat(payload)
        elif msgid == MSG_GLOBAL_POSITION:
            if v.apply_global_pos(payload):
                topic = v.topic()
                publish_dual(session, topic, v.track, MavlinkTrack, zenoh)
                if verbose:
                    t = v.track
                    print("MAV sysid={} {} lat={} lon={} alt={:.0f}m spd={:.1f}m/s".format(
                        sysid, t.get("callsign"),
                        round(t.get("lat_deg", 0), 4),
                        round(t.get("lon_deg", 0), 4),
                        t.get("alt_m", 0), t.get("speed_ms", 0)), flush=True)
        elif msgid == MSG_VFR_HUD:
            v.apply_vfr_hud(payload)


def run(args):
    if args.zenoh_raw:
        return run_zenoh_raw(args)
    session  = zenoh.open(make_config())
    vehicles: dict[int, _Vehicle] = {}
    remote_ids: dict[str, _RemoteID] = {}
    parser   = _MAVParser()
    print("MAVLink bridge started  mode={} port={}".format(
        "TCP" if args.tcp else "UDP", args.port), flush=True)

    try:
        if args.tcp:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", args.port))
            srv.listen(5)
            print("Waiting for TCP connection…", flush=True)
            while True:
                conn, addr = srv.accept()
                print("MAVLink TCP connected from {}:{}".format(*addr), flush=True)
                conn.settimeout(30)
                try:
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        _dispatch(parser.feed(data), vehicles, remote_ids, session, args.verbose)
                except (OSError, socket.timeout):
                    pass
                finally:
                    conn.close()
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", args.port))
            sock.settimeout(1)
            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    _dispatch(parser.feed(data), vehicles, remote_ids, session, args.verbose)
                except socket.timeout:
                    continue
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def run_zenoh_raw(args):
    """Decode MAVLink bytes already received by a separate Zenoh bridge."""
    session = zenoh.open(make_config())
    vehicles: dict[int, _Vehicle] = {}
    remote_ids: dict[str, _RemoteID] = {}
    parser = _MAVParser()
    topic = args.raw_topic or TOPIC_ROOT + "/raw/mavlink/**"

    def on_sample(sample):
        try:
            payload = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
            _dispatch(parser.feed(payload), vehicles, remote_ids, session, args.verbose)
        except Exception as exc:
            print("MAVLink raw decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(topic, on_sample)
    print("MAVLink Zenoh raw translator subscribed to {}".format(topic), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="MAVLink → Zenoh bridge")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MAVLINK_PORT", "14550")))
    ap.add_argument("--tcp", action="store_true",
                    default=os.environ.get("MAVLINK_TCP", "") == "1")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--zenoh-raw", action="store_true",
                    help="decode bytes from .../raw/mavlink/** instead of opening a socket")
    ap.add_argument("--raw-topic", default=os.environ.get("MAVLINK_RAW_TOPIC", ""))
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
