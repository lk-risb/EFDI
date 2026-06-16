#!/usr/bin/env python3
"""cot_bridge.py — Zenoh EFDI track topics → TAK Server / ATAK CoT bridge.

Subscribes to all EFDI track topics and forwards position updates as
Cursor-on-Target (CoT) XML to a TAK Server (FreeTAKServer) over TCP.
All connected ATAK / iTAK / TAKX / WinTAK devices see the tracks automatically
through the server — no per-device configuration, no multicast required.

Default: TCP → localhost:8087  (FreeTAKServer running in the same compose stack)
Override: --host <ip> --port <port>

For direct UDP multicast (same L2 only): --udp --host 239.2.3.1 --port 6969

Zenoh topics consumed:
  <ORG>/air/civ/tracks/v1   → CoT a-f-A   (friendly air)
  <ORG>/air/mil/tracks/v1   → CoT a-n-A   (neutral military air)
  <ORG>/land/civ/tracks/v1  → CoT a-f-G-U-C (friendly ground vehicle)
  <ORG>/sea/civ/tracks/v1   → CoT a-f-S-W-C (friendly surface vessel)
  <ORG>/aprs/tracks/v1      → CoT a-u-G   (unknown ground, unclassified APRS)

Run:
    venv/bin/python3 cot_bridge.py                        # TCP → FTS localhost
    venv/bin/python3 cot_bridge.py --host 100.64.59.10   # TCP → remote FTS
    venv/bin/python3 cot_bridge.py --udp --host 239.2.3.1 --port 6969  # UDP multicast
"""

import argparse
import json
import os
import queue
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = "1851281db70ccc0409dad4ecfc874cf5"
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)

COT_STALE_S    = 90
RECONNECT_S    = 5
SEND_TIMEOUT_S = 10

_TOPIC_COT = {
    "air/civ/tracks/v1":  "a-f-A",
    "air/mil/tracks/v1":  "a-n-A",
    "land/civ/tracks/v1": "a-f-G-U-C",
    "sea/civ/tracks/v1":  "a-f-S-W-C",
    "aprs/tracks/v1":     "a-u-G",
}


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([ROUTER]))
    conf.insert_json5("transport/link/tls", json.dumps({
        "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
        "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
        "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
        "enable_mtls": True,
        "verify_name_on_connect": True,
    }))
    return conf


# ---------------------------------------------------------------------------
# CoT XML builder
# ---------------------------------------------------------------------------

def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _uid(track: dict) -> str:
    src = track.get("_src", "efdi")
    for key in ("icao24", "mmsi", "sensor_id"):
        v = track.get(key)
        if v:
            return "EFDI-{}-{}".format(src, str(v).upper())
    cs = (track.get("callsign") or "").strip()
    if cs:
        return "EFDI-{}-{}".format(src, cs)
    return "EFDI-{}-{:.5f}-{:.5f}".format(src, track.get("lat_deg", 0), track.get("lon_deg", 0))


def _callsign(track: dict, uid: str) -> str:
    for key in ("callsign", "registration", "mmsi"):
        v = track.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return uid[-12:]


def _hae(track: dict) -> float:
    for key, scale in (
        ("geo_alt_m",   1.0),
        ("alt_geom_ft", 0.3048),
        ("baro_alt_m",  1.0),
        ("alt_baro_ft", 0.3048),
        ("alt_m",       1.0),
    ):
        v = track.get(key)
        if v is not None and float(v) != 0:
            return round(float(v) * scale, 1)
    return 9999999.0


def _speed_ms(track: dict) -> float:
    if track.get("speed_ms") is not None:
        return round(float(track["speed_ms"]), 2)
    if track.get("ground_speed_kts") is not None:
        return round(float(track["ground_speed_kts"]) * 0.514444, 2)
    return 0.0


def _course(track: dict) -> float:
    for key in ("heading_deg", "track_deg"):
        v = track.get(key)
        if v is not None:
            return round(float(v), 1)
    return 0.0


def track_to_cot(track: dict, cot_type: str) -> str | None:
    lat = track.get("lat_deg")
    lon = track.get("lon_deg")
    if lat is None or lon is None:
        return None

    now   = float(track.get("_ts", time.time()))
    stale = now + COT_STALE_S
    uid   = _uid(track)
    cs    = _callsign(track, uid)

    event = ET.Element("event", {
        "version": "2.0",
        "uid":     uid,
        "type":    cot_type,
        "how":     "m-g",
        "time":    _ts(now),
        "start":   _ts(now),
        "stale":   _ts(stale),
    })
    ET.SubElement(event, "point", {
        "lat": str(round(float(lat), 6)),
        "lon": str(round(float(lon), 6)),
        "hae": str(_hae(track)),
        "ce":  "9999999.0",
        "le":  "9999999.0",
    })
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": cs})
    ET.SubElement(detail, "track", {
        "speed":  str(_speed_ms(track)),
        "course": str(_course(track)),
    })
    remarks = ["src:{}".format(track.get("_src", "?"))]
    for key in ("icao24", "mmsi", "registration", "aircraft_type", "squawk", "is_military"):
        v = track.get(key)
        if v not in (None, "", False):
            remarks.append("{}:{}".format(key, v))
    ET.SubElement(detail, "remarks").text = " | ".join(remarks)

    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(event, encoding="unicode")


# ---------------------------------------------------------------------------
# TCP sender — persistent connection with auto-reconnect
# ---------------------------------------------------------------------------

class TcpSender:
    """Thread-safe TCP writer with reconnect. Drops messages when disconnected."""

    def __init__(self, host: str, port: int):
        self.host  = host
        self.port  = port
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        try:
            s = socket.create_connection((self.host, self.port), timeout=SEND_TIMEOUT_S)
            s.settimeout(SEND_TIMEOUT_S)
            with self._lock:
                self._sock = s
            print("TAK TCP connected → {}:{}".format(self.host, self.port), flush=True)
        except OSError as exc:
            print("TAK TCP connect failed ({}:{}) — {}, retry in {}s".format(
                self.host, self.port, exc, RECONNECT_S), flush=True)
            self._sock = None
            threading.Timer(RECONNECT_S, self._connect).start()

    def send(self, xml: str):
        data = (xml + "\n").encode("utf-8")
        with self._lock:
            sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall(data)
        except OSError:
            with self._lock:
                self._sock = None
            threading.Timer(RECONNECT_S, self._connect).start()

    def close(self):
        with self._lock:
            if self._sock:
                self._sock.close()
                self._sock = None


# ---------------------------------------------------------------------------
# UDP sender (multicast / unicast fallback)
# ---------------------------------------------------------------------------

class UdpSender:
    def __init__(self, addr: str, port: int):
        self.dest = (addr, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        is_mcast = addr.startswith("224.") or addr.startswith("239.")
        if is_mcast:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 32))
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    def send(self, xml: str):
        try:
            self._sock.sendto(xml.encode("utf-8"), self.dest)
        except OSError as exc:
            print("CoT UDP send error:", exc, flush=True)

    def close(self):
        self._sock.close()


# ---------------------------------------------------------------------------
# Zenoh → CoT callbacks
# ---------------------------------------------------------------------------

def make_handler(cot_type: str, sender, verbose: bool):
    def handler(sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        xml = track_to_cot(track, cot_type)
        if xml is None:
            return
        sender.send(xml)
        if verbose:
            cs = track.get("callsign") or track.get("registration") or track.get("mmsi") or "?"
            print("CoT {} {}".format(cot_type, cs), flush=True)
    return handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    if args.udp:
        sender = UdpSender(args.host, args.port)
        print("CoT → UDP {}:{}".format(args.host, args.port), flush=True)
    else:
        sender = TcpSender(args.host, args.port)
        print("CoT → TCP {}:{} (TAK Server)".format(args.host, args.port), flush=True)

    session = zenoh.open(make_config())
    subs = []
    for suffix, cot_type in _TOPIC_COT.items():
        key = "{}/{}".format(ORG, suffix)
        subs.append(session.declare_subscriber(key, make_handler(cot_type, sender, args.verbose)))
        print("SUB {} → {}".format(key, cot_type), flush=True)

    print("Bridge running — Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for sub in subs:
            sub.undeclare()
        session.close()
        sender.close()


def main():
    ap = argparse.ArgumentParser(description="Zenoh tracks → TAK Server / ATAK CoT bridge")
    ap.add_argument("--host", default=os.environ.get("TAK_HOST", "127.0.0.1"),
                    help="TAK Server host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TAK_PORT", "8087")),
                    help="TAK Server port (default: 8087)")
    ap.add_argument("--udp", action="store_true",
                    help="Use UDP instead of TCP (for direct multicast/unicast, no TAK Server)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each CoT message sent")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
