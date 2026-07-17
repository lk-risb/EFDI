#!/usr/bin/env python3
"""aprs_bridge.py — APRS-IS → Zenoh bridge.

Connects to the APRS-IS network (a free, global real-time APRS packet stream),
applies a geographic radius filter, decodes position packets, and publishes
each track as JSON to the EFDI Zenoh fabric.

No API key required — APRS-IS is an open network.

Zenoh topic:  <ORG>/aprs/aprs-is/tracks/v1  (configurable)
Proto schema: aprs_track.proto  (message AprsTrack, package ltu.cis.tracks.v1)

Run:
    . venv/bin/activate
    python3 aprs_bridge.py
    python3 aprs_bridge.py --lat 55.17 --lng 23.88 --range-km 300
"""

import argparse
import json
import os
import re
import socket
import time

import zenoh
from namespace_prefix import prefix

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG   # org prefix (configurable) precedes the pod namespace
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

# APRS-IS network
# Port 10152 = full unfiltered global feed (no filter command supported — floods everything)
# Port 14580 = client-filtered feed (send "filter r/lat/lon/km" on login — use this one)
APRSIS_HOST      = "rotate.aprs2.net"
APRSIS_PORT      = 14580  # filtered port; 10152 is blocked or floods without filter support
APRSIS_APP       = "efdi-aprs-bridge/1.0"

DEFAULT_LAT      = 55.17
DEFAULT_LNG      = 23.88
DEFAULT_RANGE_KM = 1000   # km radius from Lithuania — Eastern NATO flank

RECONNECT_DELAY_S = 10.0

# ---------------------------------------------------------------------------
# Zenoh helpers
# ---------------------------------------------------------------------------

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
# APRS-IS connection
# ---------------------------------------------------------------------------

def aprsis_connect(lat: float, lng: float, range_km: int):
    """Open TCP connection to APRS-IS, log in read-only, set radius filter.
    Returns (sock, file) tuple."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((APRSIS_HOST, APRSIS_PORT))
    sock.settimeout(60.0)
    f = sock.makefile("rb")

    # Read server banner
    banner = f.readline().decode("utf-8", errors="replace").strip()
    print("APRS-IS:", banner, flush=True)

    # Login read-only (pass -1 = no transmit)
    login = "user NOCALL pass -1 vers {}\r\n".format(APRSIS_APP)
    sock.sendall(login.encode())
    resp = f.readline().decode("utf-8", errors="replace").strip()
    print("APRS-IS:", resp, flush=True)

    # Send filter as separate command (more broadly supported than embedding in login)
    if range_km:
        filt = "#filter r/{}/{}/{}".format(lat, lng, range_km)
    else:
        # t/p = all position packets, worldwide
        filt = "#filter t/p"
    sock.sendall((filt + "\r\n").encode())
    print("APRS-IS filter sent: {}".format(filt), flush=True)

    return sock, f


# ---------------------------------------------------------------------------
# APRS packet parsing
# ---------------------------------------------------------------------------

# Matches the path+body separator: "CALLSIGN>PATH:body"
_PKT_RE = re.compile(r'^([A-Z0-9-]+)>([^:]+):(.*)$')

# Position without timestamp: !lat/lng  or  =lat/lng
_POS_NO_TS = re.compile(
    r'^[!=]'
    r'(\d{2})(\d{2}\.\d+)([NS])'
    r'(.)'          # symbol table
    r'(\d{3})(\d{2}\.\d+)([EW])'
    r'(.)'          # symbol code
    r'(.*)',        # rest (may include course/speed/comment)
    re.DOTALL,
)

# Position with timestamp: /DDHHMMz...  or  @DDHHMMz...
_POS_TS = re.compile(
    r'^[/@]'
    r'\d{6}[zh]'    # timestamp: 6 digits + z/h
    r'(\d{2})(\d{2}\.\d+)([NS])'
    r'(.)'
    r'(\d{3})(\d{2}\.\d+)([EW])'
    r'(.)'
    r'(.*)',
    re.DOTALL,
)

# Course/speed embedded after symbol: "CCC/SSS" (degrees / knots)
_CS_RE  = re.compile(r'^(\d{3})/(\d{3})')
# Altitude in comment: /A=NNNNNN (feet)
_ALT_RE = re.compile(r'/A=(\d+)')


def _dm_to_deg(deg_str: str, min_str: str, hemi: str) -> float:
    val = float(deg_str) + float(min_str) / 60.0
    if hemi in ("S", "W"):
        val = -val
    return round(val, 6)


def parse_position(body: str) -> dict | None:
    """Parse an APRS position body; return dict of decoded fields or None."""
    m = _POS_NO_TS.match(body) or _POS_TS.match(body)
    if not m:
        return None

    d_lat, m_lat, h_lat, sym_tbl, d_lng, m_lng, h_lng, sym_code, rest = m.groups()

    track = {
        "lat_deg": _dm_to_deg(d_lat, m_lat, h_lat),
        "lon_deg": _dm_to_deg(d_lng, m_lng, h_lng),
        "symbol": sym_tbl + sym_code,
    }

    rest = rest.strip()

    # Course/speed (only meaningful if not 000/000)
    cs = _CS_RE.match(rest)
    if cs:
        course, speed_kts = int(cs.group(1)), int(cs.group(2))
        if course != 0 or speed_kts != 0:
            track["heading_deg"] = float(course)
            track["speed_ms"] = round(speed_kts * 0.514444, 2)  # knots → m/s
        rest = rest[7:]

    # Altitude
    alt = _ALT_RE.search(rest)
    if alt:
        track["alt_m"] = round(int(alt.group(1)) * 0.3048, 1)  # feet → m

    # Strip altitude and leading comment separator
    comment = _ALT_RE.sub("", rest).lstrip("/").strip()
    if comment:
        track["comment"] = comment

    return track


def parse_packet(line: str) -> dict | None:
    """Parse one raw APRS-IS line; return track dict or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None  # server comment

    m = _PKT_RE.match(line)
    if not m:
        return None

    callsign, path, body = m.group(1), m.group(2), m.group(3)
    track = parse_position(body)
    if track is None:
        return None

    track["_ts"] = time.time()
    track["_src"] = "aprs-is"
    track["callsign"] = callsign
    track["path"] = path
    return track


# ---------------------------------------------------------------------------
# Symbol-based domain routing
# Symbol is a 2-char string: table + code, e.g. "/^" = large aircraft
# ---------------------------------------------------------------------------

# APRS symbol codes (second char) that indicate aircraft
_AIR_CODES  = set("'^XgOS")   # small aircraft, large aircraft, helicopter, glider, balloon, shuttle
# Sea vessels
_SEA_CODES  = set("YsC")      # yacht, ship/powerboat, canoe
# Land vehicles
_LAND_CODES = set("><jkuvURfa=")  # car, motorcycle, jeep, trucks, van, bus, RV, ambulance, fire, rail


def _route_topic(symbol: str) -> str:
    code = symbol[1] if len(symbol) >= 2 else ""
    if code in _AIR_CODES:
        return "{}/air/aprs-is/aprs/civ/aircraft/tracks/v1".format(TOPIC_ROOT)
    if code in _SEA_CODES:
        return "{}/sea/aprs-is/aprs/civ/vessel/tracks/v1".format(TOPIC_ROOT)
    if code in _LAND_CODES:
        return "{}/land/aprs-is/aprs/civ/vehicle/tracks/v1".format(TOPIC_ROOT)
    return "{}/land/aprs-is/aprs/neutral/station/tracks/v1".format(TOPIC_ROOT)   # digipeaters / wx / home


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args):
    filt_desc = "r/{}/{}/{}".format(args.lat, args.lng, args.range_km) if args.range_km else "none (global)"
    print("APRS-IS server: {}:{} | filter: {}".format(APRSIS_HOST, APRSIS_PORT, filt_desc), flush=True)

    session = zenoh.open(make_config())
    pub_air   = session.declare_publisher("{}/air/aprs-is/aprs/civ/aircraft/tracks/v1".format(TOPIC_ROOT))
    pub_sea   = session.declare_publisher("{}/sea/aprs-is/aprs/civ/vessel/tracks/v1".format(TOPIC_ROOT))
    pub_land  = session.declare_publisher("{}/land/aprs-is/aprs/civ/vehicle/tracks/v1".format(TOPIC_ROOT))
    pub_misc  = session.declare_publisher("{}/land/aprs-is/aprs/neutral/station/tracks/v1".format(TOPIC_ROOT))
    _pubs = {
        "{}/air/aprs-is/aprs/civ/aircraft/tracks/v1".format(TOPIC_ROOT):     pub_air,
        "{}/sea/aprs-is/aprs/civ/vessel/tracks/v1".format(TOPIC_ROOT):       pub_sea,
        "{}/land/aprs-is/aprs/civ/vehicle/tracks/v1".format(TOPIC_ROOT):     pub_land,
        "{}/land/aprs-is/aprs/neutral/station/tracks/v1".format(TOPIC_ROOT): pub_misc,
    }

    try:
        while True:
            try:
                sock, f = aprsis_connect(args.lat, args.lng, args.range_km)
                print("Streaming APRS packets…", flush=True)
                while True:
                    raw = f.readline()
                    if not raw:
                        raise EOFError("server closed connection")
                    line = raw.decode("utf-8", errors="replace")
                    track = parse_packet(line)
                    if track is None:
                        continue
                    topic = _route_topic(track.get("symbol", ""))
                    payload = json.dumps(track)
                    _pubs[topic].put(payload.encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                    print("PUB {} {}".format(topic.split("/")[-3], payload[:80]), flush=True)

            except (EOFError, OSError, TimeoutError) as exc:
                print("Connection error: {} — reconnecting in {}s".format(
                    exc, RECONNECT_DELAY_S), flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(RECONNECT_DELAY_S)

    except KeyboardInterrupt:
        pass
    finally:
        for pub in _pubs.values():
            pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="APRS-IS → Zenoh bridge")
    ap.add_argument("--lat", type=float, default=DEFAULT_LAT,
                    help="Filter center latitude (only used with --range-km)")
    ap.add_argument("--lng", type=float, default=DEFAULT_LNG,
                    help="Filter center longitude (only used with --range-km)")
    ap.add_argument("--range-km", type=int, default=DEFAULT_RANGE_KM,
                    help="Filter radius in km. Omit for global feed (default: global)")
    ap.add_argument("--topic-suffix", default="tracks/v1",
                    help="Topic suffix after .../aprs/aprs-is/")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
