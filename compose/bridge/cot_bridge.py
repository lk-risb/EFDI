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
import base64
import json
import os
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone

import zenoh

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = "1851281db70ccc0409dad4ecfc874cf5"
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
# Prefer the local router (plaintext, no TLS handshake over relay) when running
# inside the compose stack. Falls back to the remote router for standalone use.
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

AIR_STALE_S    = 30      # aircraft: ADS-B every 5-15s, 30s gives 2-6× margin
SEA_STALE_S    = 300     # vessels: Class B sends every 30-180s; 5 min covers worst case
LAND_STALE_S   = 120     # ground vehicles and APRS mobiles
COT_STALE_S    = AIR_STALE_S  # default (air)
GEO_STALE_S    = 86400   # 24 h for fixed infrastructure (OSM features)
RECONNECT_S    = 5
SEND_TIMEOUT_S = 10

# (topic_suffix, cot_type_or_None, stale_s)
# cot_type None means dynamic dispatch (see _aprs_cot_type)
_TOPIC_COT = {
    "air/civ/tracks/v1":   ("a-f-A",       AIR_STALE_S),
    "air/mil/tracks/v1":   ("a-n-A",       AIR_STALE_S),
    "land/civ/tracks/v1":  ("a-f-G-U-C",  LAND_STALE_S),
    "sea/civ/tracks/v1":   ("a-f-S-W-C",  SEA_STALE_S),
    "aprs/tracks/v1":      (None,          LAND_STALE_S),  # dynamic per symbol
    "radar/*/tracks/v1":   ("a-u-A",       AIR_STALE_S),
}

# APRS symbol table+code → CoT type.  Fixed infrastructure → neutral installation.
# Aircraft/balloon symbols → unknown air.  Everything else → unknown ground.
_APRS_SYM_COT = {
    "/-":  "a-n-G-I",      # house / home
    "/#":  "a-n-G-E-V-R",  # digipeater relay
    "/&":  "a-n-G-E-V-R",  # iGate relay
    "/_":  "a-n-G-I",      # weather station
    "/r":  "a-n-G-I",      # antenna / tower
    "/^":  "a-u-A",        # aircraft
    "/O":  "a-u-A",        # balloon
    "\\_": "a-n-G-I",      # alternate-table weather
    "\\#": "a-n-G-E-V-R",  # alternate-table digipeater
}

def _aprs_cot_type(track: dict) -> str:
    return _APRS_SYM_COT.get(track.get("symbol", ""), "a-u-G")

# OSM feature_type → CoT type for fixed infrastructure
_OSM_COT = {
    "aerodrome": "a-f-G-I-B-A",  # friendly ground installation base aerodrome
    "port":      "a-f-G-I-B-O",  # friendly ground installation base offloading
    "military":  "a-f-G-I-B-M",  # friendly ground installation base military
    "station":   "a-n-G-I",      # neutral ground installation (railway)
}

# ---------------------------------------------------------------------------
# Embedded icon generator — stdlib only, no Pillow needed.
# Icons are pre-rendered at import time and sent as b64image in CoT <usericon>.
# ATAK CIV 5.x honours b64image without needing any installed iconset.
# ---------------------------------------------------------------------------

def _icon_png_b64(shape: str, rgb: tuple, size: int = 32) -> str:
    W = H = size
    cx = cy = W / 2.0
    F = rgb + (255,)
    T = (0, 0, 0, 0)

    def _px(x, y):
        nx = (x - cx) / W
        ny = (y - cy) / H   # positive = down
        if shape == "aircraft":
            body  = (nx / 0.07) ** 2 + (ny / 0.45) ** 2 <= 1.0
            wings = abs(ny + 0.05) <= 0.13 and abs(nx) <= 0.46
            tail  = abs(ny - 0.30) <= 0.08 and abs(nx) <= 0.22
            return F if (body or wings or tail) else T
        if shape == "ship":
            return F if (nx / 0.28) ** 2 + (ny / 0.46) ** 2 <= 1.0 else T
        if shape == "vehicle":
            return F if abs(nx / 0.30) ** 4 + abs(ny / 0.22) ** 4 <= 1.0 else T
        return F if nx * nx + ny * ny <= 0.18 else T  # circle (unknown)

    pixels = [_px(x, y) for y in range(H) for x in range(W)]

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b"".join(
        b"\x00" + b"".join(bytes(pixels[y * W + x]) for x in range(W))
        for y in range(H)
    )
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


_BLUE   = (0, 116, 217)   # 2525B friendly blue
_GREEN  = (0, 164, 0)     # 2525B neutral green
_YELLOW = (255, 215, 0)   # 2525B unknown yellow

_COT_ICON_B64 = {
    "a-f-A":     _icon_png_b64("aircraft", _BLUE),
    "a-n-A":     _icon_png_b64("aircraft", _GREEN),
    "a-u-A":     _icon_png_b64("aircraft", _YELLOW),
    "a-f-G-U-C": _icon_png_b64("vehicle",  _BLUE),
    "a-f-S-W-C": _icon_png_b64("ship",     _BLUE),
    "a-u-G":     _icon_png_b64("circle",   _YELLOW),
}


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


# ---------------------------------------------------------------------------
# CoT XML builder
# ---------------------------------------------------------------------------

def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _uid(track: dict) -> str:
    # Use the stable radio identifier — source-agnostic so the same
    # aircraft/vessel reported by multiple APIs merges to one ATAK point.
    for key, prefix in (
        ("icao24",    "ICAO"),   # same hex regardless of OpenSky/FR24/airplaneslive
        ("mmsi",      "MMSI"),   # same MMSI from all AIS feeds
        ("sensor_id", "SENS"),
        ("osm_id",    "OSM"),
    ):
        v = track.get(key)
        if v:
            return "EFDI-{}-{}".format(prefix, str(v).upper())
    src = track.get("_src", "efdi")
    cs = (track.get("callsign") or "").strip()
    if cs:
        return "EFDI-{}-{}".format(src, cs)
    return "EFDI-{}-{:.5f}-{:.5f}".format(src, track.get("lat_deg", 0), track.get("lon_deg", 0))


def _callsign(track: dict, uid: str) -> str:
    # OSM name first, then ship_name (AIS), tail number, flight callsign, MMSI
    for key in ("name", "ship_name", "registration", "callsign", "mmsi"):
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
        ("alt_ft",      0.3048),   # FR24
        ("alt_m",       1.0),
    ):
        v = track.get(key)
        if v is not None and float(v) != 0:
            return round(float(v) * scale, 1)
    return 9999999.0


def _speed_ms(track: dict) -> float:
    for key, scale in (
        ("speed_ms",        1.0),
        ("ground_speed_kts", 0.514444),  # airplaneslive
        ("speed_kts",        0.514444),  # FR24
        ("sog_ms",           1.0),       # AIS
    ):
        v = track.get(key)
        if v is not None:
            return round(float(v) * scale, 2)
    return 0.0


def _course(track: dict) -> float:
    for key in ("heading_deg", "track_deg", "cog_deg"):  # cog_deg = AIS
        v = track.get(key)
        if v is not None:
            return round(float(v), 1)
    return 0.0


def track_to_cot(track: dict, cot_type: str, stale_s: float = COT_STALE_S) -> str | None:
    lat = track.get("lat_deg")
    lon = track.get("lon_deg")
    if lat is None or lon is None:
        return None

    now   = float(track.get("_ts", time.time()))
    stale = now + stale_s
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
    icon_b64 = _COT_ICON_B64.get(cot_type)
    if icon_b64:
        ET.SubElement(detail, "usericon", {"b64image": icon_b64})
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

def make_handler(cot_type_or_fn, sender, verbose: bool, stale_s: float = COT_STALE_S):
    def handler(sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        cot_type = cot_type_or_fn(track) if callable(cot_type_or_fn) else cot_type_or_fn
        xml = track_to_cot(track, cot_type, stale_s=stale_s)
        if xml is None:
            return
        sender.send(xml)
        if verbose:
            cs = track.get("callsign") or track.get("registration") or track.get("mmsi") or "?"
            print("CoT {} {}".format(cot_type, cs), flush=True)
    return handler


def make_geo_handler(sender, verbose: bool):
    """Handler for OSM land/geo/v1 — maps feature_type to CoT type with 24h stale."""
    def handler(sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        feature_type = track.get("feature_type", "")
        cot_type = _OSM_COT.get(feature_type, "a-n-G-I")
        xml = track_to_cot(track, cot_type, stale_s=GEO_STALE_S)
        if xml is None:
            return
        sender.send(xml)
        if verbose:
            print("CoT {} {} {}".format(cot_type, feature_type, track.get("name", "?")), flush=True)
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
    for suffix, (cot_type, stale_s) in _TOPIC_COT.items():
        key = "{}/{}".format(ORG, suffix)
        if cot_type is None:
            subs.append(session.declare_subscriber(
                key, make_handler(_aprs_cot_type, sender, args.verbose, stale_s=stale_s)))
            print("SUB {} → [dynamic APRS symbol, stale={}s]".format(key, stale_s), flush=True)
        else:
            subs.append(session.declare_subscriber(
                key, make_handler(cot_type, sender, args.verbose, stale_s=stale_s)))
            print("SUB {} → {} stale={}s".format(key, cot_type, stale_s), flush=True)

    # OSM geo features (aerodromes, ports, military bases, railway stations) — 24h stale
    geo_key = "{}/land/geo/v1".format(ORG)
    subs.append(session.declare_subscriber(geo_key, make_geo_handler(sender, args.verbose)))
    print("SUB {} → [OSM geo features, 24h stale]".format(geo_key), flush=True)

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
