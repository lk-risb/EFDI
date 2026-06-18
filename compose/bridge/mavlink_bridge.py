#!/usr/bin/env python3
"""mavlink_bridge.py — MAVLink telemetry → Zenoh bridge.

Listens on UDP (or TCP) for MAVLink v1/v2 frames from ArduPilot/PX4 UAVs
or GCS software, decodes position and attitude, and publishes each vehicle
as a JSON track to the EFDI Zenoh fabric.

Supported messages:
  HEARTBEAT (0)            — vehicle type; primes per-sysid state
  GLOBAL_POSITION_INT (33) — WGS-84 lat/lon/alt, ground velocity
  VFR_HUD (74)             — groundspeed and heading (fallback)

Config (compose/.env):
  MAVLINK_PORT=14550   UDP port (14550 = MAVLink GCS default)
  MAVLINK_TCP=         Set to 1 for TCP mode

Run:
  venv/bin/python3 mavlink_bridge.py --port 14550
  venv/bin/python3 mavlink_bridge.py --port 14550 --tcp
"""

import argparse
import json
import math
import os
import socket
import struct
import time

import zenoh

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = "1851281db70ccc0409dad4ecfc874cf5"
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

MAV_STX_V1 = 0xFE
MAV_STX_V2 = 0xFD

MSG_HEARTBEAT       = 0
MSG_GLOBAL_POSITION = 33
MSG_VFR_HUD         = 74

# X25 CRC extra byte per message ID (from MAVLink XML definitions)
_CRC_EXTRA = {0: 50, 33: 104, 74: 20}

_MAV_TYPE = {
    0: "generic", 1: "fixed-wing", 2: "quadrotor", 3: "coaxial",
    4: "helicopter", 13: "hexarotor", 14: "octorotor", 19: "vtol",
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
        _, gs, _, _, hdg, _ = struct.unpack_from("<ffffhH", payload)
        if "speed_ms" not in self.track:
            self.track["speed_ms"] = round(float(gs), 2)
        if "heading_deg" not in self.track:
            self.track["heading_deg"] = float(hdg)

    def topic(self) -> str:
        return "{}/air/mavlink/uav/civ/aircraft/tracks/v1".format(ORG)


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


def _dispatch(msgs, vehicles: dict, session: "zenoh.Session", verbose: bool):
    for sysid, msgid, payload in msgs:
        if sysid not in vehicles:
            vehicles[sysid] = _Vehicle(sysid)
        v = vehicles[sysid]

        if msgid == MSG_HEARTBEAT:
            v.apply_heartbeat(payload)
        elif msgid == MSG_GLOBAL_POSITION:
            if v.apply_global_pos(payload):
                topic = v.topic()
                session.put(topic, json.dumps(v.track).encode(),
                            encoding=zenoh.Encoding.APPLICATION_JSON)
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
    session  = zenoh.open(make_config())
    vehicles: dict[int, _Vehicle] = {}
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
                        _dispatch(parser.feed(data), vehicles, session, args.verbose)
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
                    _dispatch(parser.feed(data), vehicles, session, args.verbose)
                except socket.timeout:
                    continue
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def main():
    ap = argparse.ArgumentParser(description="MAVLink → Zenoh bridge")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MAVLINK_PORT", "14550")))
    ap.add_argument("--tcp", action="store_true",
                    default=os.environ.get("MAVLINK_TCP", "") == "1")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
