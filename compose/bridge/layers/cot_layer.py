#!/usr/bin/env python3
"""cot_layer.py — Zenoh EFDI track topics → TAK Server / ATAK CoT bridge.

Subscribes to all EFDI track topics and forwards position updates as
Cursor-on-Target (CoT) XML to a TAK Server over TCP.
All connected ATAK / iTAK / TAKX / WinTAK devices see the tracks automatically
through the server — no per-device configuration, no multicast required.

Option A — plaintext TCP (NetBird tunnel provides encryption):
  Default: TCP → localhost:8087
  Override: --host <ip> --port <port>

Option B — mutual TLS directly on official TAK Server port 8089:
  --host <ip> --port 8089 --tls --cert cert.pem --key key.pem --ca ca.pem
  Generate certs with: make add-service NAME=efdi-pod  (in the TAK repo)

For direct UDP multicast (same L2 only): --udp --host 239.2.3.1 --port 6969

Zenoh topics consumed (5 main categories):
  AIR:   <ORG>/air/civ/tracks/v1    → CoT a-f-A-C-F / a-h-A-C-F (civil, hostile if RU/BY)
         <ORG>/air/mil/tracks/v1    → CoT a-n-A-M-F / a-h-A-M-F (military, hostile if RU/BY)
         <ORG>/air/radar/tracks/v1  → CoT a-u-A     (radar return, unidentified)
         <ORG>/air/sapient/tracks/v1→ CoT a-u-A     (SAPIENT sensor track)
  LAND:  <ORG>/land/civ/tracks/v1   → CoT a-f-G-E-V-C (friendly ground vehicle)
         <ORG>/land/aprs/tracks/v1  → CoT a-n-G-I   (APRS stations / digipeaters / wx)
         <ORG>/land/nffi/tracks/v1  → CoT a-f-G-U-C (NATO NFFI friendly forces)
         <ORG>/land/geo/v1          → CoT (OSM aerodrome/port/military, 24h stale)
  SEA:   <ORG>/sea/civ/tracks/v1    → CoT a-f-S-X-L / a-h-S-X-L (hostile if RU/BY MMSI)
  SPACE: <ORG>/space/tracks/v1      → CoT a-f-P     (satellite)

Run:
    venv/bin/python3 cot_layer.py                                          # TCP plaintext → localhost:8087
    venv/bin/python3 cot_layer.py --host 100.64.x.x --port 8087           # TCP plaintext → remote TAK Server
    venv/bin/python3 cot_layer.py --host 100.64.x.x --port 8089 --tls \   # mTLS → official TAK Server
        --cert cert.pem --key key.pem --ca ca.pem
    venv/bin/python3 cot_layer.py --udp --host 239.2.3.1 --port 6969      # UDP multicast
"""

import argparse
import base64
import json
import math
import os
import re
import socket
import ssl
import struct
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone

import zenoh

try:
    import mgrs as _mgrs_lib
    _MGRS    = _mgrs_lib.MGRS()
    _MGRS_RE = re.compile(r'^(\d{1,2}[A-Z])([A-Z]{2})(.*)$')
except Exception:
    _MGRS = None
    _MGRS_RE = None

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = "LTU/CISB/" + ORG   # organization prefix precedes the pod namespace
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
# Prefer the local router (plaintext, no TLS handshake over relay) when running
# inside the compose stack. Falls back to the remote router for standalone use.
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

AIR_STALE_S    = 30      # aircraft: ADS-B every 5-15s, 30s gives 2-6× margin
SEA_STALE_S    = 300     # vessels: Class B sends every 30-180s; 5 min covers worst case
LAND_STALE_S   = 120     # ground vehicles and APRS mobiles
COT_STALE_S    = AIR_STALE_S  # default (air)
SAT_STALE_S    = 300     # satellites: polled every 60s, 5 min gives 5× margin
GEO_STALE_S    = 86400   # 24 h for fixed infrastructure (OSM features)
ENV_STALE_S    = 3600    # weather stations: polled every 15–30 min, 1 h gives plenty of margin
SENSOR_STALE_S = 1800    # air quality sensors: polled every 10 min, 30 min stale
RECONNECT_S    = 5
SEND_TIMEOUT_S = 10

# Dead-reckoning — extrapolate position forward when sensor updates stop
_DR_TICK_S   = 2.0   # extrapolation interval (seconds)
_DR_MIN_MS   = 5.15  # don't extrapolate below ~10 kt (5.15 m/s)

# Emergency squawk codes (ICAO Annex 10)
_EMERGENCY_SQUAWK = {"7500": "HIJACK", "7600": "COMMS FAILURE", "7700": "MAYDAY"}

# NATO Mode 1 mission-type codes (5-bit, displayed as 2-digit octal 00–37)
_MODE1_LABEL = {
    "00": "default",         "01": "air defense",     "02": "interceptor",
    "03": "ground attack",   "04": "close air support","05": "interdiction",
    "06": "deep strike",     "07": "anti-sub",
    "10": "fighter",         "11": "attack",          "12": "transport",
    "13": "reconnaissance",  "14": "electronic warfare","15": "tanker",
    "16": "helicopter",      "17": "search & rescue",
    "20": "maritime patrol", "21": "training",        "22": "VIP/government",
    "23": "cargo",           "24": "utility",         "25": "liaison",
    "26": "admin",           "27": "reserved",
    "30": "UAV",             "31": "AWACS/AEW",       "32": "refuelling",
    "33": "medevac",         "34": "special ops",     "35": "mine countermeasures",
    "36": "test/evaluation", "37": "reserved",
}
# AIS nav status values that indicate vessel distress
_DISTRESS_NAV = frozenset({"aground", "not_under_command", "not under command"})

# Module-level stores — initialised once, shared across all handler threads
_dr_lock     = threading.Lock()
_dr_store:   dict[str, dict] = {}
_alert_lock  = threading.Lock()
_alerted:    set = set()   # uids currently in a known emergency state (no re-alert)
_radar_status_lock = threading.Lock()
_radar_status: dict[str, dict] = {}   # "sac-sic" → latest CAT-34 status dict

# ---------------------------------------------------------------------------
# Hostile-state classifiers
# ICAO 24-bit address ranges and MMSI Maritime Identification Digits (MID)
# for states designated hostile in the Eastern NATO flank scenario.
# ---------------------------------------------------------------------------

# (lo, hi) inclusive hex ranges of ICAO 24-bit addresses
_HOSTILE_ICAO_RANGES = [
    (0x140000, 0x17FFFF),  # Russia
    (0x510000, 0x5103FF),  # Belarus
]

# First 3 digits of MMSI = MID (ITU)
_HOSTILE_MID = {"273", "374"}   # 273 = Russia, 374 = Belarus

def _is_hostile_icao24(icao24) -> bool:
    try:
        n = int(str(icao24), 16)
        return any(lo <= n <= hi for lo, hi in _HOSTILE_ICAO_RANGES)
    except (ValueError, TypeError):
        return False

def _is_hostile_mmsi(mmsi) -> bool:
    return str(mmsi)[:3] in _HOSTILE_MID

def _civ_air_type(track: dict) -> str:
    return "a-h-A-C-F" if _is_hostile_icao24(track.get("icao24")) else "a-n-A-C-F"

def _mil_air_type(track: dict) -> str:
    return "a-h-A-M-F" if _is_hostile_icao24(track.get("icao24")) else "a-n-A-M-F"

def _sea_type(track: dict) -> str:
    return "a-h-S-X-L" if _is_hostile_mmsi(track.get("mmsi", "")) else "a-n-S-X-L"


# Sources that must NOT reach ATAK directly — they must pass through
# track_fusion_bridge first so the marker contains merged data.
#
# ADS-B relay sources: identity-only enrichment inputs, blocked until fused.
_ADS_B_RELAY_SOURCES = frozenset({"opensky", "fr24", "airplaneslive"})
# Raw sensor sources: kinematics are good but no identity; fusion adds REG/ICAO/SQWK.
# Blocked here because fusion ALWAYS re-publishes every radar track to air/fused/**,
# so every contact still appears — just once, with merged data.
_RAW_SENSOR_SOURCES = frozenset({"ASTERIX CAT-48", "ASTERIX CAT-20"})

# Schema: {category}/{vendor}/{protocol}/{affiliation}/{entity_type}/{data_type}/v1
# Wildcards: ** matches zero-or-more segments, so air/**/civ/aircraft/** catches any
# vendor+protocol combination under civil air.
# NOTE: air/fused/** is caught by the broad air/** wildcards below — no separate
# fused entries needed. The broad wildcards also catch SAPIENT / Link-16 / cot-rx
# that don't go through the radar fusion path.
_TOPIC_COT = {
    # AIR — affiliation slot drives CoT type; ICAO24 classifier overrides for RU/BY
    # Covers fused tracks (air/fused/**) + SAPIENT + Link-16 + cot-rx.
    # Raw CAT-48 / CAT-20 are dropped by _RAW_SENSOR_SOURCES check in make_handler.
    "air/**/civ/aircraft/**":    (_civ_air_type,  AIR_STALE_S),
    "air/**/mil/aircraft/**":    (_mil_air_type,  AIR_STALE_S),
    "air/**/unknown/**":         ("a-u-A-C-F",    AIR_STALE_S),
    # LAND — full affiliation matrix for SitaWare / NFFI / APRS
    "land/**/civ/vehicle/**":    ("a-f-G-E-V-C", LAND_STALE_S),
    "land/**/neutral/station/**":("a-n-G-I-R",   LAND_STALE_S),
    "land/**/friendly/unit/**":  ("a-f-G-U-C",   LAND_STALE_S),
    "land/**/hostile/unit/**":   ("a-h-G-U-C",   LAND_STALE_S),
    "land/**/neutral/unit/**":   ("a-n-G-U-C",   LAND_STALE_S),
    "land/**/unknown/unit/**":   ("a-u-G-U-C",   LAND_STALE_S),
    # AIR — full affiliation matrix
    "air/**/friendly/aircraft/**": ("a-f-A-M-F",   AIR_STALE_S),
    "air/**/hostile/aircraft/**":  ("a-h-A-M-F",   AIR_STALE_S),
    "air/**/hostile/uav/**":       ("a-h-A-M-F-Q", AIR_STALE_S),
    # SEA — full affiliation matrix
    "sea/**/civ/vessel/**":      (_sea_type,      SEA_STALE_S),
    "sea/**/mil/vessel/**":      ("a-n-S-W-C",   SEA_STALE_S),
    "sea/**/friendly/vessel/**": ("a-f-S-X-L",   SEA_STALE_S),
    "sea/**/hostile/vessel/**":  ("a-h-S-X-L",   SEA_STALE_S),
    "sea/**/neutral/vessel/**":  ("a-n-S-X-L",   SEA_STALE_S),
    # SPACE
    "space/**/civ/satellite/**": ("a-f-P",        SAT_STALE_S),
    # ENV — weather stations and air quality sensors show as ground icons
    "env/weather/station/**":    ("a-n-G-I-R",   ENV_STALE_S),
    "env/air_quality/station/**":("a-n-G-I-R",   SENSOR_STALE_S),
    # RADAR SENSOR SITES — CAT-34 status publishes here; rendered as radar marker + stat card
    "land/**/neutral/radar/**":  ("a-f-G-E-S-R", LAND_STALE_S * 2),
    # ACOUSTIC / RF SENSOR SITES — sensor box; recolors green/yellow/red by
    # last_detection_ts (see _sensor_alert_cot_type), same icon throughout
    "land/**/neutral/sensor/**": (_sensor_alert_cot_type, LAND_STALE_S * 2),
    # GPS JAMMING / SPOOFING THREATS — hostile ground EW equipment
    "env/gps/ew/hostile/threat/**": ("a-h-G-E-X", ENV_STALE_S * 2),
}

# ATC / ground-station callsigns that appear in ADS-B feeds.
# Transponders belonging to ATC towers, ground vehicles, ATIS etc. show flight ID "TWR",
# "GND", "ATIS" etc.  We reclassify them as neutral ground radar/radio stations instead
# of aircraft so they don't pollute the air picture.
_ATC_EXACT  = frozenset(["TWR", "GND", "ATIS", "APP", "DEP", "APCH", "CTR", "OPS",
                          "RAMP", "CARGO", "FUEL", "FIRE", "MAINT", "VAGON"])
_ATC_SUFFIX = ("TWR", "GND", "ATIS", "APP", "CTR")

def _is_ground_station(track: dict) -> bool:
    cs = (track.get("callsign") or "").strip().upper()
    if cs in _ATC_EXACT:
        return True
    return any(cs.endswith(s) for s in _ATC_SUFFIX) and len(cs) <= 8

# APRS symbol table+code → CoT type.  Fixed infrastructure → neutral installation.
# Aircraft/balloon symbols → unknown air.  Everything else → unknown ground.
_APRS_SYM_COT = {
    "/-":  "a-n-G-I",      # house / home installation
    "/#":  "a-n-G-I",      # digipeater → neutral ground installation (tower)
    "/&":  "a-n-G-I",      # iGate → neutral ground installation (tower)
    "/_":  "a-n-G-I",      # weather station installation
    "/r":  "a-n-G-I",      # antenna / tower installation
    "/^":  "a-u-A-C-F",    # civil aircraft (unknown)
    "/O":  "a-u-A",        # balloon / airship
    "\\_": "a-n-G-I",      # alternate-table weather
    "\\#": "a-n-G-I",      # alternate-table digipeater
}

def _aprs_cot_type(track: dict) -> str:
    return _APRS_SYM_COT.get(track.get("symbol", ""), "a-u-G")

# OSM feature_type → CoT type for fixed infrastructure.
# Civilian features are neutral (a-n-) so they never get flipped to hostile.
# Military is friendly (a-f-) so _geo_cot_type() can flip RU/BY bases to hostile.
_OSM_COT = {
    "aerodrome":   "a-n-G-I-B-A",  # neutral aerodrome (civilian shared infrastructure)
    "port":        "a-n-G-I-B-O",  # neutral port
    "military":    "a-f-G-I-B-M",  # friendly military base → flipped to hostile for RU/BY
    "station":     "a-n-G-I",      # neutral ground installation (railway)
    "alpr_camera": "a-n-G-E-S",    # neutral ground sensor (same icon as acoustic sensors)
    "cctv_camera": "a-n-G-E-S",    # neutral ground sensor — general CCTV/surveillance camera
}

# ISO 3166-1 alpha-2 country codes of hostile states in this scenario
_HOSTILE_CC = {"RU", "BY"}

def _geo_cot_type(track: dict, base_type: str) -> str:
    """Flip friendly (a-f-) to hostile (a-h-) for features in RU/BY territory.
    Only the OSM country_code tag is used — no bbox fallback to avoid false
    positives for EU installations near the Belarus/Kaliningrad border."""
    if not base_type.startswith("a-f-"):
        return base_type  # neutral/unknown types stay unchanged
    cc = (track.get("country_code") or "").upper()
    if cc in _HOSTILE_CC:
        return base_type.replace("a-f-", "a-h-", 1)
    return base_type

# Ground sensor site alert coloring — same icon (G-E-S) throughout, only the
# MIL-STD-2525C affiliation letter changes based on how recently a detection
# was reported at that sensor. Used by dronuradaras.lt: no separate drone
# marker exists, the sensor's own marker recolors instead (there's no reliable
# drone position, only "sensor X heard something just now").
_SENSOR_ALERT_HOT_S  = 60    # red  — detection within the last minute
_SENSOR_ALERT_WARM_S = 300   # yellow — cooling down, matches the bridge's DETECT_WINDOW_S

def _sensor_alert_cot_type(track: dict) -> str:
    ts = track.get("last_detection_ts")
    if not ts:
        return "a-n-G-E-S"   # green — no alert on record
    age = time.time() - float(ts)
    if age <= _SENSOR_ALERT_HOT_S:
        return "a-h-G-E-S"   # red — active
    if age <= _SENSOR_ALERT_WARM_S:
        return "a-u-G-E-S"   # yellow — cooling down
    return "a-n-G-E-S"       # green — reverted

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
            # T-shape: narrow fuselage, straight wings, horizontal tail
            body  = (nx / 0.07) ** 2 + (ny / 0.45) ** 2 <= 1.0
            wings = abs(ny + 0.05) <= 0.13 and abs(nx) <= 0.46
            tail  = abs(ny - 0.30) <= 0.08 and abs(nx) <= 0.22
            return F if (body or wings or tail) else T
        if shape == "fighter":
            # Delta/swept wing — wide at mid-body, tapering to nose and tail
            swept = abs(nx) <= max(0.0, 0.44 - abs(ny + 0.08) * 0.78)
            body  = (nx / 0.05) ** 2 + (ny / 0.46) ** 2 <= 1.0
            return F if (swept or body) else T
        if shape == "ship":
            return F if (nx / 0.28) ** 2 + (ny / 0.46) ** 2 <= 1.0 else T
        if shape == "vehicle":
            return F if abs(nx / 0.30) ** 4 + abs(ny / 0.22) ** 4 <= 1.0 else T
        if shape == "car":
            # Top-down car: rectangular body + four wheel-arch ovals at corners
            body = abs(nx) <= 0.22 and abs(ny) <= 0.40
            w1 = (nx + 0.28)**2 / 0.009 + (ny + 0.26)**2 / 0.014 <= 1.0
            w2 = (nx - 0.28)**2 / 0.009 + (ny + 0.26)**2 / 0.014 <= 1.0
            w3 = (nx + 0.28)**2 / 0.009 + (ny - 0.26)**2 / 0.014 <= 1.0
            w4 = (nx - 0.28)**2 / 0.009 + (ny - 0.26)**2 / 0.014 <= 1.0
            return F if (body or w1 or w2 or w3 or w4) else T
        if shape == "satellite":
            # Satellite: rectangular body + two wide solar-panel wings
            body = abs(nx) <= 0.12 and abs(ny) <= 0.12
            panL = -0.46 <= nx <= -0.14 and abs(ny) <= 0.08
            panR =  0.14 <= nx <=  0.46 and abs(ny) <= 0.08
            return F if (body or panL or panR) else T
        if shape == "tower":
            # Lattice antenna / radio tower: thin mast + 3 reducing crossbars
            mast = abs(nx) <= 0.05
            bar1 = abs(ny + 0.10) <= 0.04 and abs(nx) <= 0.18
            bar2 = abs(ny + 0.28) <= 0.04 and abs(nx) <= 0.32
            bar3 = abs(ny + 0.43) <= 0.04 and abs(nx) <= 0.44
            return F if (mast or bar1 or bar2 or bar3) else T
        if shape == "radar":
            # Parabolic dish (upper semicircle arc) + mast + base
            r2   = nx * nx + (ny + 0.15) ** 2
            dish = 0.09 <= r2 <= 0.30 and ny <= -0.15
            mast = abs(nx) <= 0.04 and ny >= -0.15
            base = abs(ny - 0.42) <= 0.04 and abs(nx) <= 0.20
            return F if (dish or mast or base) else T
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


_BLUE   = (0, 116, 217)   # 2525C friendly blue
_GREEN  = (0, 164, 0)     # 2525C neutral green
_YELLOW = (255, 215, 0)   # 2525C unknown yellow
_RED    = (220, 20,  20)  # 2525C hostile red

# b64image fallback — used when ATAK doesn't have the iconset installed.
# ATAK CIV 5.x ignores b64image for recognised 2525C types, so we also set
# iconsetpath (below) which is honoured regardless of the CoT type.
_COT_ICON_B64 = {
    # Civil aircraft — T-shaped commercial silhouette, neutral green
    "a-n-A-C-F":   _icon_png_b64("aircraft", _GREEN),
    "a-h-A-C-F":   _icon_png_b64("aircraft", _RED),
    "a-u-A-C-F":   _icon_png_b64("aircraft", _YELLOW),
    # Military aircraft — swept-wing fighter silhouette
    "a-f-A-M-F":   _icon_png_b64("fighter",  _BLUE),   # allied military (blue)
    "a-n-A-M-F":   _icon_png_b64("fighter",  _GREEN),  # unknown military (neutral)
    "a-h-A-M-F":   _icon_png_b64("fighter",  _RED),
    "a-u-A":       _icon_png_b64("fighter",  _YELLOW),
    # Surface vessels — neutral green for civilian, red for hostile
    "a-n-S-X-L":   _icon_png_b64("ship",     _GREEN),
    "a-h-S-X-L":   _icon_png_b64("ship",     _RED),
    # Space / satellite
    "a-f-P":       _icon_png_b64("satellite", _BLUE),
    # Ground
    "a-f-G-E-V-C": _icon_png_b64("car",      _BLUE),
    "a-u-G":       _icon_png_b64("circle",   _YELLOW),
    "a-n-G-I":     _icon_png_b64("circle",   _GREEN),
    "a-n-G-I-R":   _icon_png_b64("tower",    _GREEN),   # Neutral ground installation radio
    "a-n-G-E-S-R": _icon_png_b64("radar",    _GREEN),   # Neutral ground electronic sensor radar (civ ATC)
    "a-f-G-E-S-R": _icon_png_b64("radar",    _BLUE),    # Friendly ground electronic sensor radar (mil)
    "a-h-G-E-S-R": _icon_png_b64("radar",    _RED),     # Hostile ground electronic sensor radar
    "a-f-G-I-B-A": _icon_png_b64("circle",   _BLUE),
    "a-f-G-I-B-O": _icon_png_b64("circle",   _BLUE),
    "a-f-G-I-B-M": _icon_png_b64("circle",   _BLUE),
    "a-n-G-I-B-A": _icon_png_b64("circle",   _GREEN),
    "a-n-G-I-B-O": _icon_png_b64("circle",   _GREEN),
    "a-h-G-I-B-A": _icon_png_b64("circle",   _RED),
    "a-h-G-I-B-O": _icon_png_b64("circle",   _RED),
    "a-h-G-I-B-M": _icon_png_b64("circle",   _RED),
}

# Primary icon path — references the MIL-STD-2525C iconset pre-installed in
# ATAK CIV 5.x.  ATAK renders iconsetpath even when CoT type is standard 2525C,
# giving the correct inner function symbol (plane, ship silhouette) inside the
# 2525C affiliation frame (arc=air, U=sea, box=ground).
_ISET = "34ae1613-9645-4222-a9d2-e5f243dea2865"
_COT_ICONSET = {
    "a-n-A-C-F":   "{}/Neutral/Air/Fixed Wing.png".format(_ISET),
    "a-h-A-C-F":   "{}/Hostile/Air/Fixed Wing.png".format(_ISET),
    "a-f-A-M-F":   "{}/Friendly/Air/Military Fixed Wing.png".format(_ISET),
    "a-n-A-M-F":   "{}/Neutral/Air/Military Fixed Wing.png".format(_ISET),
    "a-h-A-M-F":   "{}/Hostile/Air/Military Fixed Wing.png".format(_ISET),
    "a-u-A":       "{}/Unknown/Air/Military Fixed Wing.png".format(_ISET),
    "a-u-A-C-F":   "{}/Unknown/Air/Fixed Wing.png".format(_ISET),
    "a-f-G-E-V-C": "{}/Friendly/Land/Vehicle.png".format(_ISET),
    "a-n-S-X-L":   "{}/Neutral/Sea/Vessel.png".format(_ISET),
    "a-h-S-X-L":   "{}/Hostile/Sea/Vessel.png".format(_ISET),
    "a-u-G":       "{}/Unknown/Land/Generic.png".format(_ISET),
    "a-n-G-I":     "{}/Neutral/Land/Structure.png".format(_ISET),
    "a-f-G-I-B-A": "{}/Friendly/Land/Airfield.png".format(_ISET),
    "a-f-G-I-B-O": "{}/Friendly/Land/Port.png".format(_ISET),
    "a-f-G-I-B-M": "{}/Friendly/Land/Military Base.png".format(_ISET),
    "a-n-G-I-B-A": "{}/Neutral/Land/Airfield.png".format(_ISET),
    "a-n-G-I-B-O": "{}/Neutral/Land/Port.png".format(_ISET),
    "a-h-G-I-B-A": "{}/Hostile/Land/Airfield.png".format(_ISET),
    "a-h-G-I-B-O": "{}/Hostile/Land/Port.png".format(_ISET),
    "a-h-G-I-B-M": "{}/Hostile/Land/Military Base.png".format(_ISET),
    "a-f-P":       "{}/Friendly/Space/Satellite.png".format(_ISET),
    "a-f-G-U-C":   "{}/Friendly/Land/Unit.png".format(_ISET),
    "a-n-G-I-R":   "{}/Neutral/Land/Radio.png".format(_ISET),
    "a-n-G-E-S-R": "{}/Neutral/Land/Radar.png".format(_ISET),
    "a-f-G-E-S-R": "{}/Friendly/Land/Radar.png".format(_ISET),
    "a-h-G-E-S-R": "{}/Hostile/Land/Radar.png".format(_ISET),
}

def _extrapolate_pos(lat: float, lon: float, speed_ms: float,
                     heading_deg: float, dt_s: float) -> tuple[float, float]:
    d  = speed_ms * dt_s
    R  = 6_371_000.0
    az = math.radians(heading_deg)
    la = math.radians(lat)
    lo = math.radians(lon)
    la2 = math.asin(math.sin(la) * math.cos(d / R) +
                    math.cos(la) * math.sin(d / R) * math.cos(az))
    lo2 = lo + math.atan2(math.sin(az) * math.sin(d / R) * math.cos(la),
                          math.cos(d / R) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)



def _mgrs_lines(lat: float, lon: float) -> list[str]:
    """Return MGRS at 1km / 100m / 10m / 1m precision as formatted strings."""
    if _MGRS is None:
        return []
    try:
        out = []
        for prec, label in ((2, "1km"), (3, "100m"), (4, "10m"), (5, "1m")):
            raw = _MGRS.toMGRS(lat, lon, MGRSPrecision=prec)
            m = _MGRS_RE.match(raw)
            if m:
                gzd, sq, en = m.groups()
                n   = len(en) // 2
                raw = "{} {} {} {}".format(gzd, sq, en[:n], en[n:])
            out.append("MGRS ({}): {}".format(label, raw))
        return out
    except Exception:
        return []


def _track_age(track: dict) -> str:
    """Return a human-readable age string like '(4s ago)' or '(1m 23s ago)'."""
    tod = track.get("tod_s")
    ts  = track.get("_ts")
    now = time.time()
    if tod is not None:
        now_dt  = datetime.now(tz=timezone.utc)
        now_tod = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second + now_dt.microsecond / 1e6
        age_s   = now_tod - float(tod)
        if age_s < 0:
            age_s += 86400
    elif ts is not None:
        age_s = now - float(ts)
    else:
        return ""
    age_s = max(0.0, age_s)
    if age_s < 60:
        return "({:.0f}s ago)".format(age_s)
    elif age_s < 3600:
        return "({}m {}s ago)".format(int(age_s // 60), int(age_s % 60))
    else:
        return "({}h {}m ago)".format(int(age_s // 3600), int((age_s % 3600) // 60))


def _start_dr_thread(sender):
    """Background thread: dead-reckon contacts that haven't sent a real update."""
    def _loop():
        while True:
            time.sleep(_DR_TICK_S)
            now = time.time()
            with _dr_lock:
                items = list(_dr_store.items())
            for uid, st in items:
                age    = now - st["base_ts"]
                max_dr = st["stale_s"] * 0.85
                if age < _DR_TICK_S or age > max_dr:
                    continue
                spd = _speed_ms(st["track"])
                if spd < _DR_MIN_MS:
                    continue
                hdg  = _course(st["track"])
                blat = st["base_lat"]
                blon = st["base_lon"]
                if blat is None or blon is None:
                    continue
                elat, elon = _extrapolate_pos(blat, blon, spd, hdg, age)
                et = dict(st["track"])
                et["lat_deg"] = elat
                et["lon_deg"] = elon
                et["_ts"]     = now
                et["_extrap"] = True
                xml = track_to_cot(et, st["cot_type"], stale_s=_DR_TICK_S * 3)
                if xml:
                    sender.send(xml)
    threading.Thread(target=_loop, daemon=True).start()


def _send_geochat_alert(sender, uid: str, lat: float, lon: float, message: str):
    """Send a GeoChat broadcast to All Chat Rooms — shows as a popup on every ATAK device."""
    now    = time.time()
    msg_id = "{}-ALERT-{:.0f}".format(uid, now)
    event  = ET.Element("event", {
        "version": "2.0",
        "uid":     "GeoChat.EFDI.ALL.{}".format(msg_id),
        "type":    "b-t-f",
        "how":     "m-g",
        "time":    _ts(now), "start": _ts(now), "stale": _ts(now + 300),
    })
    ET.SubElement(event, "point", {
        "lat": str(round(lat, 6)), "lon": str(round(lon, 6)),
        "hae": "9999999.0", "ce": "9999999.0", "le": "9999999.0",
    })
    detail = ET.SubElement(event, "detail")
    chat = ET.SubElement(detail, "__chat", {
        "parent": "TeamTalk", "groupOwner": "false",
        "messageId": msg_id, "chatroom": "All Chat Rooms",
        "id": "All Chat Rooms", "senderCallsign": "EFDI-ALERT",
    })
    ET.SubElement(chat, "chatgrp", {
        "uid0": "EFDI-ALERT", "uid1": "All Chat Rooms", "id": "All Chat Rooms",
    })
    ET.SubElement(detail, "link", {"uid": "EFDI-ALERT", "type": "a-n-G-I", "relation": "p-p"})
    ET.SubElement(detail, "remarks", {
        "source": "EFDI-ALERT", "to": "All Chat Rooms", "time": _ts(now),
    }).text = message
    ET.SubElement(detail, "__serverdestination", {"destinations": "All Chat Rooms"})
    sender.send('<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(event, encoding="unicode"))


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
        ("sat_id",    "SAT"),    # NORAD catalogue number (n2yo)
        ("radar_id",  "RAD"),    # CAT-48 PSR track (no Mode-S) — SAC/SIC/track_num
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
    # Named features (OSM, vessels, satellites)
    for key in ("name", "ship_name", "sat_name", "sensor_name"):
        v = track.get(key)
        if v and str(v).strip():
            return str(v).strip()
    # Aircraft label priority: REG > TRK-# > callsign
    reg       = (track.get("registration") or "").strip()
    atype     = (track.get("aircraft_type") or "").strip()
    cs        = (track.get("callsign")      or "").strip()
    track_num = track.get("track_num")
    if reg:
        label = reg
    elif track_num is not None:
        label = "TRK-{}".format(track_num)
    else:
        label = cs
    if label and atype:
        return "{} ({})".format(label, atype)
    if label:
        return label
    if atype:
        return atype
    # Other entity types
    place = (track.get("place_name") or "").strip()
    if place:
        return place
    mmsi = track.get("mmsi")
    if mmsi:
        return str(mmsi)
    return uid[-12:]


def _build_remarks(track: dict, cot_type: str) -> str:
    """Build a structured vertical key-value info panel for the ATAK callout bubble."""
    src    = track.get("_src", "?")
    parts  = cot_type.split("-")
    domain = parts[2] if len(parts) > 2 else "?"
    lines  = []

    def _row(label: str, value) -> None:
        if value not in (None, "", 0.0):
            lines.append("{}: {}".format(label, value))

    if domain == "A":   # ---- AIR ----------------------------------------
        ident_l = []; iff_l = []; kinem_l = []; radar_l = []; status_l = []

        def _sec(title, buf):
            if buf:
                lines.append("─── {} ───".format(title))
                lines.extend(buf)

        def _r(label, value, buf):
            if value not in (None, "", 0.0):
                buf.append("{}: {}".format(label, value))

        # ── IDENTITY ──
        cs_raw  = (track.get("callsign")      or "").strip().upper()
        atype   = (track.get("aircraft_type") or "").strip().upper()
        cs_disp = "{} ({})".format(cs_raw, atype) if cs_raw and atype else (cs_raw or atype or None)
        if track.get("track_num") is not None:
            _r("TRK (track #)", track["track_num"], ident_l)
        if cs_disp:
            ident_l.append("CS (callsign): {}".format(cs_disp))
        _r("REG (registration)",  (track.get("registration") or "").strip().upper() or None, ident_l)
        _r("ICAO (hex address)",  (track.get("icao24")       or "").strip().upper() or None, ident_l)
        _r("FLAG (country)",  track.get("origin_country"), ident_l)
        opr = (track.get("operating_as") or track.get("painted_as") or "").strip()
        _r("OPR (operator)",  opr or None, ident_l)
        _r("ROUTE",           track.get("route"),    ident_l)
        _r("UAV Type",        track.get("mav_type"), ident_l)

        # ── IFF / MODES ──
        m1 = track.get("mode1")
        if m1 is not None:
            m1_str = str(m1)
            label  = _MODE1_LABEL.get(m1_str.zfill(2))
            iff_l.append("MODE 1 (NATO MIL ID): {}{}".format(
                m1_str, " ({})".format(label) if label else ""))
        _r("MODE 2 (military code)", track.get("mode2"), iff_l)
        _r("MODE 3 (squawk)", track.get("squawk"), iff_l)
        iff = track.get("iff", "")
        if iff == "friendly":
            iff_l.append("MODE 4 (IFF): FRIENDLY")
        elif iff == "unknown":
            iff_l.append("MODE 4 (IFF): UNKNOWN")
        elif iff == "no_reply":
            iff_l.append("MODE 4 (IFF): NO REPLY")
        if track.get("mode5_active"):
            m5 = ["MODE 5 (secure IFF): ACTIVE"]
            if track.get("mode5_iff"):  m5.append("IFF OK")
            if track.get("mode5_data"): m5.append("DATA VALID")
            iff_l.append("  ".join(m5))
        sq_str = str(track.get("squawk") or "")
        if sq_str in _EMERGENCY_SQUAWK:
            iff_l.append("[!!! EMERGENCY: {} !!!]".format(_EMERGENCY_SQUAWK[sq_str]))
        if track.get("mil_emergency"):
            iff_l.append("[!!! MILITARY EMERGENCY !!!]")
        com = track.get("com_capability")
        if com:
            iff_l.append("COM (comms capability): level-{}{}".format(
                com, "  25ft altitude reporting" if track.get("altitude_25ft") else ""))
        lt = track.get("link_tech")
        if lt:
            iff_l.append("LINK (data link): {}".format(" / ".join(lt)))
        ec_str = track.get("emitter_category_str")
        if ec_str:
            iff_l.append("EMITTER (aircraft category): {}".format(ec_str))

        # ── KINEMATICS ──
        tod = track.get("tod_s")
        age = _track_age(track)
        if tod is not None:
            h = int(tod // 3600) % 24; m = int((tod % 3600) // 60); s = tod % 60
            kinem_l.append("TOD (time of detection): {:02d}:{:02d}:{:04.1f} UTC  {}".format(h, m, s, age).rstrip())
        else:
            ts = track.get("_ts")
            if ts:
                kinem_l.append("TIME: {}  {}".format(
                    datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"), age).rstrip())
        lat = track.get("lat_deg"); lon = track.get("lon_deg")
        if lat is not None: kinem_l.append("LAT (latitude): {:.5f}°".format(round(lat, 5)))
        if lon is not None: kinem_l.append("LON (longitude): {:.5f}°".format(round(lon, 5)))
        if lat is not None and lon is not None:
            kinem_l.extend(_mgrs_lines(lat, lon))
        hdg  = _course(track)
        roll = track.get("roll_deg")
        if roll is not None:
            kinem_l.append("HDG (heading): {}°   ROLL (bank angle): {:+.1f}°".format(int(hdg), roll))
        else:
            kinem_l.append("HDG (heading): {}°".format(int(hdg)))
        alt_m = _hae(track)
        if alt_m < 9_999_998:
            alt_ft = int(alt_m / 0.3048)
            alt_fl = "FL{:03d}".format(alt_ft // 100) if alt_ft > 1000 else "{} ft".format(alt_ft)
            h_src  = track.get("height_src")
            alt_line = "ALT (Altitude): {}  ({} ft / {} m)".format(alt_fl, alt_ft, int(alt_m))
            if h_src: alt_line += "  [{}]".format(h_src)
            kinem_l.append(alt_line)
        baro_vr = track.get("baro_vr_fpm")
        geo_vr  = track.get("geo_vr_fpm")
        vr_ms   = track.get("vertical_rate_ms")
        vt = track.get("vertical_trend", "")
        if baro_vr is not None:
            vfpm = baro_vr; vms = baro_vr / 196.85
            vr_label = "V/S (vertical speed)"
        elif geo_vr is not None:
            vfpm = geo_vr; vms = geo_vr / 196.85
            vr_label = "V/S (geometric vertical speed)"
        elif vr_ms is not None:
            vfpm = int(float(vr_ms) * 196.85); vms = float(vr_ms)
            vr_label = "V/S (vertical speed)"
        else:
            vfpm = None; vr_label = "V/S (vertical speed)"
        if vfpm is not None:
            vs = "{}: {:+d} ft/min / {:+.1f} m/s".format(vr_label, vfpm, vms)
            kinem_l.append(vs + ("  ({})".format(vt.upper()) if vt else ""))
        elif vt:
            kinem_l.append("CDM (climb/descent mode): {}".format(vt.upper()))
        spd = _speed_ms(track)
        ias_mav = track.get("airspeed_ms")
        tas_kt  = track.get("tas_kt")
        ias_kt  = track.get("ias_kt")
        mach    = track.get("mach")
        if ias_mav is not None:
            kinem_l.append("AIRSPEED: {} kt / {} km/h".format(
                round(float(ias_mav) / 0.514444), round(float(ias_mav) * 3.6)))
        if tas_kt is not None:
            kinem_l.append("TAS (true airspeed): {} kt / {} km/h".format(int(tas_kt), int(tas_kt * 1.852)))
        if ias_kt is not None:
            kinem_l.append("IAS (indicated airspeed): {} kt / {} km/h".format(int(ias_kt), int(ias_kt * 1.852)))
        if spd:
            gs_kt = round(spd / 0.514444); gs_kmh = round(spd * 3.6)
            lbl = "GS (ground speed)" if (tas_kt or ias_kt or ias_mav) else "SPD (speed)"
            kinem_l.append("{}: {} kt / {} km/h".format(lbl, gs_kt, gs_kmh))
        if mach is not None:
            kinem_l.append("MACH (Mach number): {:.3f}".format(mach))
        # Magnetic heading (if differs from track by more than 2°)
        mag_hdg = track.get("mag_hdg_deg")
        if mag_hdg is not None:
            diff = abs(mag_hdg - hdg)
            if diff > 180: diff = 360 - diff
            if diff > 2:
                kinem_l.append("MAG HDG (magnetic heading): {}°".format(round(mag_hdg, 1)))
        # Doppler speed
        dop = track.get("doppler_kt")
        if dop is not None:
            kinem_l.append("DOPPLER (radial speed): {:+.0f} kt".format(dop))
        # Selected altitude
        sel_alt = track.get("selected_alt_ft")
        if sel_alt is not None:
            src_lbl = track.get("selected_alt_source", "")
            fl_lbl  = "FL{:03d}".format(abs(sel_alt) // 100) if abs(sel_alt) > 1000 else "{} ft".format(sel_alt)
            kinem_l.append("SEL ALT (selected altitude): {}{}".format(
                fl_lbl, "  ({})".format(src_lbl) if src_lbl else ""))
        fin_alt = track.get("final_alt_ft")
        if fin_alt is not None:
            fl_lbl = "FL{:03d}".format(abs(fin_alt) // 100) if abs(fin_alt) > 1000 else "{} ft".format(fin_alt)
            kinem_l.append("FINAL ALT (target altitude): {}".format(fl_lbl))
        # Wind / temperature from ADS-B met
        ws = track.get("wind_speed_kt"); wd = track.get("wind_dir_deg"); tc = track.get("temp_c")
        if ws is not None or wd is not None:
            parts = []
            if wd is not None: parts.append("{}°".format(int(wd)))
            if ws is not None: parts.append("{} kt".format(round(ws, 1)))
            line = "WIND: {}".format(" / ".join(parts))
            if tc is not None: line += "  TEMP: {}°C".format(round(tc, 1))
            kinem_l.append(line)
        elif tc is not None:
            kinem_l.append("TEMP: {}°C".format(round(tc, 1)))
        # Track angle rate
        tar = track.get("track_angle_rate_degs")
        if tar is not None and abs(tar) >= 0.05:
            kinem_l.append("TURN RATE: {:+.2f} °/s".format(tar))

        # ── RADAR ──
        rng = track.get("range_nm"); azm = track.get("azimuth_deg")
        if rng is not None:
            radar_l.append("RNG (range): {} nm / {} km   AZM (azimuth): {}°".format(
                round(rng, 1), round(rng * 1.852, 1), round(azm or 0, 1)))
        rssi = track.get("rssi_db")
        if rssi is not None: radar_l.append("RSSI (signal strength): {} dBFS".format(rssi))
        ssr_amp = track.get("ssr_amplitude_dbm"); psr_amp = track.get("psr_amplitude_dbm")
        sig_amp = track.get("signal_amplitude_dbm")
        if psr_amp is not None or ssr_amp is not None:
            amp_parts = []
            if psr_amp is not None: amp_parts.append("PSR (primary radar): {} dBm".format(psr_amp))
            if ssr_amp is not None: amp_parts.append("SSR (secondary radar): {} dBm".format(ssr_amp))
            radar_l.append("AMPLITUDE — {}".format("  ".join(amp_parts)))
        elif sig_amp is not None:
            radar_l.append("AMPLITUDE (signal): {} dBm".format(sig_amp))
        # Track quality / accuracy
        sx = track.get("track_sigma_x_nm"); sh = track.get("track_sigma_h_ft")
        if sx is not None or sh is not None:
            acc_parts = []
            if sx is not None:
                sy = track.get("track_sigma_y_nm")
                acc_parts.append("±{:.3f} nm".format(max(sx, sy) if sy is not None else sx))
            if sh is not None: acc_parts.append("±{} ft".format(sh))
            radar_l.append("ACC (position accuracy): {}".format("  ".join(acc_parts)))
        nac_p = track.get("nac_p"); nic = track.get("nic"); nac_v = track.get("nac_v")
        if nac_p is not None or nic is not None or nac_v is not None:
            ads_acc = []
            if nac_p is not None: ads_acc.append("NACp={}".format(nac_p))
            if nic   is not None: ads_acc.append("NIC={}".format(nic))
            if nac_v is not None: ads_acc.append("NACv={}".format(nac_v))
            radar_l.append("ADS-B ACC (ADS-B accuracy): {}".format("  ".join(ads_acc)))
        # Track update ages (CAT-062 I062/290)
        ages = []
        for sensor_key, label in (("psr","PSR"),("ssr","SSR"),("ads","ADS"),("mds","MDS"),
                                   ("es","ES"),("vdl","VDL"),("uat","UAT"),
                                   ("lop","LOP"),("mlt","MLT")):
            v = track.get("track_age_{}_s".format(sensor_key))
            if v is not None: ages.append("{}: {}s".format(label, round(v, 1)))
        if ages: radar_l.append("AGE — {}".format("  ".join(ages)))
        data_ages = []
        for _dk, _dl in (("psr","PSR"),("ssr","SSR"),("mds","MDS"),("ads_b","ADS-B"),("es","ES"),("vdl4","VDL4"),("uat","UAT")):
            v = track.get("data_age_{}_s".format(_dk))
            if v is not None: data_ages.append("{}: {}s".format(_dl, round(v, 1)))
        if data_ages: radar_l.append("DATA AGE — {}".format("  ".join(data_ages)))
        sac_t = track.get("sac"); sic_t = track.get("sic")
        if rng is not None and sac_t is not None:
            radar_l.append("SITE (SAC/SIC): {}/{}".format(sac_t, sic_t))
        if track.get("radar_id"):
            radar_l.append("RDR (radar ID): {}".format(track["radar_id"]))
        pol = track.get("psr_polarization"); chan = track.get("ssr_channel")
        if pol or chan:
            pol_parts = []
            if pol:  pol_parts.append("PSR POL (polarisation): {}".format(pol.upper()))
            if chan: pol_parts.append("SSR CH (channel): {}".format(chan))
            radar_l.append("  ".join(pol_parts))
        tod_acc = track.get("tod_accuracy_s")
        if tod_acc is not None:
            radar_l.append("TOD ACC (time accuracy): ±{:.4f}s".format(tod_acc))
        mlat_rx = track.get("mlat_receivers")
        if mlat_rx:
            radar_l.append("MLAT (receivers): {}".format(", ".join(str(r) for r in mlat_rx)))
        # Enrich with live CAT-34 sensor status if available
        if sac_t is not None:
            with _radar_status_lock:
                rs = _radar_status.get("{}-{}".format(sac_t, sic_t or 0))
            if rs:
                parts = []
                for k, lbl in (("psr_status","PSR"),("ssr_status","SSR"),("mds_status","MDS")):
                    v = rs.get(k, "")
                    if v and v not in ("", "not_present", "not_operational"):
                        parts.append("{}: {}".format(lbl, v[:4].upper()))
                if parts: radar_l.append("  ".join(parts))
                if rs.get("sys_nogo"): radar_l.append("[SENSOR DEGRADED]")
                ae = rs.get("collimation_az_deg"); re = rs.get("collimation_rng_nm")
                if ae is not None and re is not None and (abs(ae) > 0.001 or abs(re) > 0.001):
                    radar_l.append("CAL (calibration error): AZ {:+.3f}°  RNG {:+.3f} nm".format(ae, re))

        # ── STATUS ──
        if track.get("spi"):
            status_l.append("[⚠ SPI (special position ident)]")
        sense = track.get("acas_ra_sense")
        if sense:
            status_l.append("[⚠ TCAS/ACAS RA (collision avoidance): {}]".format(sense))
        elif track.get("acas_ra_active"):
            status_l.append("[⚠ TCAS/ACAS (collision avoidance) ACTIVE]")
        em_str = track.get("emergency_str")
        if em_str:
            status_l.append("[⚠ EMERGENCY: {}]".format(em_str))
        if track.get("msaw"):         status_l.append("[MSAW (minimum safe altitude warning)]")
        if track.get("squawk_garbled"): status_l.append("[SQUAWK GARBLED]")
        if track.get("on_ground"):    status_l.append("[ON GROUND]")
        if track.get("is_military"):  status_l.append("[MILITARY]")
        if track.get("track_ghost"):     status_l.append("[GHOST TARGET]")
        if track.get("track_tentative"): status_l.append("[TENTATIVE]")
        if track.get("track_end"):       status_l.append("[TRACK END]")
        if track.get("track_coasting"):  status_l.append("[COASTING]")
        if track.get("track_begin"):     status_l.append("[TRACK START]")
        if track.get("amalgamated"):     status_l.append("[AMALGAMATED]")
        if track.get("track_manoeuvre"): status_l.append("[MANOEUVRE]")
        if track.get("_extrap"):         status_l.append("[DEAD RECKONED]")
        xp_stat = track.get("transponder_status")
        if xp_stat:                    status_l.append("XPDR (transponder): {}".format(xp_stat.upper()))
        ant = track.get("antenna_type")
        if ant:                        status_l.append("ANT (sensor type): {}".format(ant.upper()))
        if track.get("simulated"):     status_l.append("[SIMULATED TARGET]")
        if track.get("test_target"):   status_l.append("[TEST TARGET]")
        if track.get("extended_range"):status_l.append("[EXTENDED RANGE]")
        if track.get("mil_ident"):     status_l.append("[MIL IDENT]")
        if track.get("in_trouble"):    status_l.append("[IN TROUBLE]")
        pmsg = track.get("preprog_msg")
        if pmsg:
            status_l.append("[MSG: {}]".format(pmsg.upper().replace("_", " ")))
        status_l.append("SRC (data source): {}".format(src))

        # ── FLIGHT PLAN ──
        fp_l = []
        fp_cs    = track.get("fp_callsign")
        ac_type  = (track.get("aircraft_type") or "").strip().upper() or None
        wtc      = track.get("wake_turb_cat")
        dep      = (track.get("departure_icao") or "").strip() or None
        dst      = (track.get("destination_icao") or "").strip() or None
        cfl      = track.get("cleared_fl")
        ctl      = track.get("current_fl")
        sid      = (track.get("sid")  or "").strip() or None
        star     = (track.get("star") or "").strip() or None
        stand    = (track.get("aircraft_stand") or "").strip() or None
        if fp_cs or ac_type:
            line = []
            if fp_cs:   line.append("FLT (flight id): {}".format(fp_cs))
            if ac_type: line.append("TYPE (aircraft): {}".format(ac_type))
            if wtc:     line.append("WTC (wake turbulence): {}".format(wtc))
            fp_l.append("  ".join(line))
        if dep or dst:
            fp_l.append("DEP (departure): {} → DST (destination): {}".format(dep or "----", dst or "----"))
        if sid or star:
            fp_l.append("SID (departure proc): {}  STAR (arrival proc): {}".format(sid or "--", star or "--"))
        if cfl is not None:
            fp_l.append("CFL (cleared level): FL{:03d}".format(int(cfl)))
        if ctl is not None:
            fp_l.append("CTL (current level): FL{:03d}".format(int(ctl)))
        if stand:
            fp_l.append("STAND (aircraft stand): {}".format(stand))
        fr_str = track.get("flight_rules"); gat_str = track.get("flight_gat")
        rvsm_s = track.get("rvsm")
        fp_cat = "  ".join(filter(None, [fr_str, gat_str,
                                          ("RVSM: " + rvsm_s) if rvsm_s else None,
                                          "[HIGH PRIORITY]" if track.get("high_priority") else None]))
        if fp_cat: fp_l.append(fp_cat)

        _sec("IDENTITY",   ident_l)
        _sec("IFF / MODES", iff_l)
        _sec("KINEMATICS", kinem_l)
        _sec("RADAR",      radar_l)
        _sec("FLIGHT PLAN", fp_l)
        _sec("STATUS",     status_l)

    elif domain == "S":  # ---- SEA ----------------------------------------
        def _sec(title, buf):
            if buf:
                lines.append("─── {} ───".format(title))
                lines.extend(buf)
        ident_l = []; kinem_l = []; status_l = []

        # IDENTITY
        ts = track.get("_ts")
        if ts:
            age = _track_age(track)
            ident_l.append("TIME: {}  {}".format(
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"), age).rstrip())
        _r = lambda lbl, v, b: b.append("{}: {}".format(lbl, v)) if v not in (None, "", 0.0) else None
        _r("NAME", (track.get("ship_name") or "").strip() or None, ident_l)
        _r("MMSI", track.get("mmsi"), ident_l)
        _r("IMO",  track.get("imo"),  ident_l)
        _r("CALL", (track.get("callsign") or "").strip().upper() or None, ident_l)
        _r("TYPE", track.get("ship_type"), ident_l)
        _r("FLAG", track.get("flag") or track.get("origin_country") or None, ident_l)
        _r("DEST", (track.get("destination") or "").strip() or None, ident_l)
        _r("ETA",  track.get("eta"), ident_l)
        length = track.get("length_m"); beam = track.get("beam_m")
        if length:
            _r("DIM", "{} × {} m".format(int(length), int(beam)) if beam else "{} m".format(int(length)), ident_l)
        if track.get("draft_m"):
            ident_l.append("DRAFT: {} m".format(round(float(track["draft_m"]), 1)))

        # KINEMATICS
        lat = track.get("lat_deg"); lon = track.get("lon_deg")
        if lat is not None: kinem_l.append("LAT: {:.5f}°".format(round(lat, 5)))
        if lon is not None: kinem_l.append("LON: {:.5f}°".format(round(lon, 5)))
        if lat is not None and lon is not None:
            kinem_l.extend(_mgrs_lines(lat, lon))
        hdg = track.get("heading_deg"); cog = track.get("cog_deg")
        if hdg is not None and cog is not None and abs(hdg - cog) > 5:
            kinem_l.append("HDG: {}° (bow)   COG: {}° (track)".format(int(hdg), int(cog)))
        else:
            kinem_l.append("COG: {}°".format(int(cog or hdg or 0)))
        sog = _speed_ms(track)
        if sog:
            kinem_l.append("SOG: {} kt / {} km/h".format(
                round(sog / 0.514444, 1), round(sog * 3.6, 1)))
        nav = track.get("nav_status", "").replace("_", " ")
        if nav: kinem_l.append("STATUS: {}".format(nav))
        nav_key = track.get("nav_status", "").lower().replace(" ", "_")
        if nav_key in _DISTRESS_NAV:
            status_l.append("[!!! DISTRESS: {} !!!]".format(nav.upper()))
        status_l.append("SRC: {}".format(src))

        _sec("IDENTITY",   ident_l)
        _sec("KINEMATICS", kinem_l)
        _sec("STATUS",     status_l)

    elif domain == "P":  # ---- SPACE / SATELLITE ---------------------------
        def _sec(title, buf):
            if buf:
                lines.append("─── {} ───".format(title))
                lines.extend(buf)
        ident_l = []; kinem_l = []

        ts = track.get("_ts")
        if ts:
            age = _track_age(track)
            ident_l.append("TIME: {}  {}".format(
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"), age).rstrip())
        _r = lambda lbl, v, b: b.append("{}: {}".format(lbl, v)) if v not in (None, "", 0.0) else None
        _r("NORAD", track.get("sat_id") or track.get("sensor_id") or track.get("norad_id"), ident_l)
        _r("NAME",  track.get("sat_name") or track.get("name"), ident_l)

        lat = track.get("lat_deg"); lon = track.get("lon_deg")
        if lat is not None: kinem_l.append("LAT: {:.5f}°".format(round(lat, 5)))
        if lon is not None: kinem_l.append("LON: {:.5f}°".format(round(lon, 5)))
        if lat is not None and lon is not None:
            kinem_l.extend(_mgrs_lines(lat, lon))
        el = track.get("elevation_deg"); az = track.get("azimuth_deg")
        if el is not None and az is not None:
            kinem_l.append("ELEV: {}°   AZIM: {}°".format(round(float(el), 1), round(float(az), 1)))
        elif el is not None: kinem_l.append("ELEV: {}°".format(round(float(el), 1)))
        alt_km = track.get("alt_km")
        if alt_km is None:
            raw_m = _hae(track)
            alt_km = raw_m / 1000.0 if raw_m < 9_999_998 else None
        if alt_km is not None:
            kinem_l.append("ALT: {} km ({} m)".format(
                round(float(alt_km)), int(float(alt_km) * 1000)))
        spd = _speed_ms(track)
        if spd:
            kinem_l.append("SPD: {} km/s ({} km/h)".format(
                round(spd / 1000, 2), round(spd * 3.6)))
        kinem_l.append("SRC: {}".format(src))

        _sec("IDENTITY",   ident_l)
        _sec("KINEMATICS", kinem_l)

    else:                # ---- GROUND / ENV / APRS / OSM ------------------
        def _sec(title, buf):
            if buf:
                lines.append("─── {} ───".format(title))
                lines.extend(buf)
        _r = lambda lbl, v, b: b.append("{}: {}".format(lbl, v)) if v not in (None, "", 0.0) else None

        ts = track.get("_ts")
        if ts:
            age      = _track_age(track)
            time_str = ("{}  {}".format(
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"), age)).rstrip()
        else:
            time_str = None
        lat = track.get("lat_deg"); lon = track.get("lon_deg")

        if track.get("sensor_type") == "radar":   # ---- RADAR SENSOR SITE ----
            sensor_l = []; status_l = []; calib_l = []; stats_l = []
            # Show wall-clock update time (use _ts, not radar's local tod_s)
            ts_r = track.get("_ts")
            if ts_r:
                age_r = time.time() - float(ts_r)
                if age_r < 60:
                    age_str = "({:.0f}s ago)".format(age_r)
                else:
                    age_str = "({}m {}s ago)".format(int(age_r // 60), int(age_r % 60))
                sensor_l.append("TIME: {}  {}".format(
                    datetime.fromtimestamp(float(ts_r), tz=timezone.utc).strftime("%H:%M:%S UTC"),
                    age_str))
            # Show radar's own clock (may differ from UTC due to local timezone)
            radar_clk = track.get("radar_clock_s")
            if radar_clk is not None:
                h = int(radar_clk // 3600) % 24; m = int((radar_clk % 3600) // 60); s = radar_clk % 60
                sensor_l.append("RADAR CLOCK: {:02d}:{:02d}:{:04.1f}".format(h, m, s))
            # Online duration
            first = track.get("online_since")
            if first is not None:
                up_s = time.time() - float(first)
                if up_s < 3600:
                    up_str = "{}m {:02d}s".format(int(up_s // 60), int(up_s % 60))
                else:
                    up_str = "{}h {:02d}m".format(int(up_s // 3600), int((up_s % 3600) // 60))
                sensor_l.append("ONLINE: {}".format(up_str))
            sac = track.get("sac"); sic = track.get("sic")
            if sac is not None: sensor_l.append("SAC/SIC: {}/{}".format(sac, sic))
            nm = track.get("sensor_name")
            if nm: sensor_l.append("NAME: {}".format(nm))
            if lat is not None: sensor_l.append("LAT: {:.5f}°".format(round(lat, 5)))
            if lon is not None: sensor_l.append("LON: {:.5f}°".format(round(lon, 5)))
            if lat is not None and lon is not None:
                sensor_l.extend(_mgrs_lines(lat, lon))
            rot = track.get("rotation_s")
            if rot: sensor_l.append("ROTATION: {}s/rev  ({:.1f} RPM)".format(
                round(rot, 1), 60.0 / rot))
            # STATUS
            for k, lbl in (("psr_status","PSR"), ("ssr_status","SSR"), ("mds_status","MODE-S")):
                v = track.get(k)
                if v: status_l.append("{}: {}".format(lbl, v.upper().replace("_"," ")))
            if track.get("sys_nogo"):        status_l.append("[SYSTEM DEGRADED]")
            if track.get("sys_ovl_rdp"):     status_l.append("[RDP OVERLOAD]")
            if track.get("sys_ovl_xmt"):     status_l.append("[TX OVERLOAD]")
            if track.get("sys_tsv_invalid"): status_l.append("[TIME SOURCE INVALID]")
            red = track.get("reduction_level")
            if red: status_l.append("REDUCTION LEVEL: {}".format(red))
            status_l.append("SRC: {}".format(src))
            # CALIBRATION
            ae = track.get("collimation_az_deg"); re = track.get("collimation_rng_nm")
            if ae is not None: calib_l.append("AZ ERROR:  {:+.4f}°".format(ae))
            if re is not None: calib_l.append("RNG ERROR: {:+.4f} nm".format(re))
            # STATISTICS
            count_lbl = {"psr":"PSR","ssr":"SSR","psr_ssr":"PSR+SSR","all":"ALL",
                         "mode5":"MODE-5","mil_id":"MIL ID"}
            for typ, cnt in sorted((track.get("msg_counts") or {}).items()):
                stats_l.append("{}: {} tracks".format(count_lbl.get(typ, typ.upper()), cnt))
            _sec("SENSOR",      sensor_l)
            _sec("STATUS",      status_l)
            _sec("CALIBRATION", calib_l)
            _sec("STATISTICS",  stats_l)

        elif track.get("sensor_type") == "acoustic":   # ---- ACOUSTIC SENSOR SITE (dronuradaras) ----
            sensor_l = []; alert_l = []
            if time_str: sensor_l.append("TIME: {}".format(time_str))
            nm = track.get("sensor_name")
            if nm: sensor_l.append("NAME: {}".format(nm))
            if lat is not None: sensor_l.append("LAT: {:.5f}°".format(round(lat, 5)))
            if lon is not None: sensor_l.append("LON: {:.5f}°".format(round(lon, 5)))
            if lat is not None and lon is not None:
                sensor_l.extend(_mgrs_lines(lat, lon))
            sensor_l.append("SRC: {}".format(src))
            last_det = track.get("last_detection_ts")
            if last_det:
                age = time.time() - float(last_det)
                if age <= _SENSOR_ALERT_HOT_S:
                    alert_l.append("[⚠ ACTIVE DETECTION — {:.0f}s ago]".format(age))
                elif age <= _SENSOR_ALERT_WARM_S:
                    alert_l.append("[COOLING DOWN — last detection {:.0f}s ago]".format(age))
                audio_url = track.get("last_detection_audio_url")
                if audio_url:
                    alert_l.append("AUDIO: {}".format(audio_url))
            _sec("SENSOR", sensor_l)
            _sec("ALERT",  alert_l)

        elif src in ("gpsjam", "eurocontrol", "custom") and track.get("event_type") in ("jamming", "spoofing", "unknown"):  # GPS EW
            threat_l = []; pos_l = []
            if time_str: threat_l.append("TIME: {}".format(time_str))
            etype  = track.get("event_type", "unknown").upper()
            sev    = track.get("severity",   "unknown").upper()
            prob   = track.get("jam_prob", -1.0)
            radius = track.get("radius_km", 0.0)
            detail = track.get("detail", "")
            threat_l.append("TYPE: GPS {}".format(etype))
            threat_l.append("SEVERITY: {}".format(sev))
            if prob >= 0:
                threat_l.append("PROBABILITY: {:.0%}".format(prob))
            if radius:
                threat_l.append("RADIUS: {} km".format(round(radius)))
            threat_l.append("SRC: {}".format(src.upper()))
            rep = track.get("report_utc", "")
            if rep:
                threat_l.append("REPORTED: {}".format(rep))
            if detail:
                threat_l.append("NOTE: {}".format(detail))
            if lat is not None: pos_l.append("LAT: {:.5f}°".format(round(lat, 5)))
            if lon is not None: pos_l.append("LON: {:.5f}°".format(round(lon, 5)))
            if lat is not None and lon is not None:
                pos_l.extend(_mgrs_lines(lat, lon))
            _sec("GPS EW THREAT", threat_l)
            _sec("POSITION",      pos_l)

        elif src in ("openmeteo", "meteolt", "yrno", "windy"):   # WEATHER
            place = (track.get("place_name") or track.get("place_code") or src).upper()
            ident_l = []; env_l = []
            if time_str: ident_l.append("TIME: {}".format(time_str))
            ident_l.append("STATION: {}".format(place))
            if lat is not None: ident_l.append("LAT: {:.5f}°".format(round(lat, 5)))
            if lon is not None: ident_l.append("LON: {:.5f}°".format(round(lon, 5)))
            if lat is not None and lon is not None:
                ident_l.extend(_mgrs_lines(lat, lon))
            t = track.get("temperature_c"); ft = track.get("apparent_temperature_c") or track.get("feels_like_c")
            if t is not None:
                feels = "  (feels {} °C)".format(round(float(ft), 1)) if ft is not None else ""
                env_l.append("TEMP: {} °C{}".format(round(float(t), 1), feels))
            rh = track.get("relative_humidity_pct")
            if rh is not None: env_l.append("HUMIDITY: {}%".format(int(rh)))
            ws = track.get("wind_speed_ms"); wd = track.get("wind_direction_deg")
            wg = track.get("wind_gusts_ms")
            if ws is not None:
                wind = "WIND: {} m/s".format(round(float(ws), 1))
                if wd is not None: wind += "  {}°".format(int(wd))
                if wg is not None: wind += "  (gusts {} m/s)".format(round(float(wg), 1))
                env_l.append(wind)
            p = track.get("pressure_hpa")
            if p is not None: env_l.append("PRESSURE: {} hPa".format(round(float(p), 1)))
            cc = track.get("cloud_cover_pct")
            if cc is not None: env_l.append("CLOUD: {}%".format(int(cc)))
            pr = track.get("precipitation_mm")
            if pr is not None and float(pr) > 0:
                env_l.append("PRECIP: {} mm".format(round(float(pr), 1)))
            env_l.append("SRC: {}".format(src))
            _sec("WEATHER",     ident_l)
            _sec("CONDITIONS",  env_l)

        elif src == "purpleair":   # AIR QUALITY
            name = track.get("sensor_name") or "Sensor #{}".format(track.get("sensor_id", "?"))
            ident_l = []; env_l = []
            if time_str: ident_l.append("TIME: {}".format(time_str))
            ident_l.append("SENSOR: {}".format(name))
            if lat is not None: ident_l.append("LAT: {:.5f}°".format(round(lat, 5)))
            if lon is not None: ident_l.append("LON: {:.5f}°".format(round(lon, 5)))
            if lat is not None and lon is not None:
                ident_l.extend(_mgrs_lines(lat, lon))
            aqi = track.get("aqi"); aqicat = track.get("aqi_category", "")
            if aqi is not None:
                env_l.append("AQI: {} ({})".format(int(aqi), aqicat) if aqicat else "AQI: {}".format(int(aqi)))
            for key, label in (("pm25_ugm3", "PM2.5"), ("pm10_ugm3", "PM10"), ("pm1_ugm3", "PM1")):
                v = track.get(key)
                if v is not None: env_l.append("{}: {} µg/m³".format(label, round(float(v), 1)))
            t = track.get("temperature_c"); rh = track.get("relative_humidity_pct")
            if t  is not None: env_l.append("TEMP: {} °C".format(round(float(t), 1)))
            if rh is not None: env_l.append("HUMIDITY: {}%".format(int(rh)))
            p = track.get("pressure_hpa")
            if p  is not None: env_l.append("PRESSURE: {} hPa".format(round(float(p), 1)))
            env_l.append("SRC: {}".format(src))
            _sec("AIR QUALITY", ident_l)
            _sec("READINGS",    env_l)

        else:                      # APRS / OSM / vehicles
            feat = track.get("feature_type")
            if feat in ("alpr_camera", "cctv_camera"):   # OSM surveillance camera (ALPR or general CCTV)
                ident_l = []
                if time_str: ident_l.append("TIME: {}".format(time_str))
                ident_l.append("TYPE: {}".format("ALPR CAMERA" if feat == "alpr_camera" else "CCTV CAMERA"))
                _r("SUBTYPE",  track.get("surveillance_type"), ident_l)
                _r("BRAND",    track.get("brand"),    ident_l)
                _r("OPERATOR", track.get("operator"), ident_l)
                _r("ZONE",     track.get("zone"),     ident_l)
                _r("MOUNT",    track.get("mount"),    ident_l)
                heading = track.get("heading_deg")
                if heading is not None:
                    ident_l.append("HEADING: {}°".format(int(heading)))
                elif track.get("heading_raw"):
                    ident_l.append("HEADING: {}".format(track["heading_raw"]))
                _r("COUNTRY",  (track.get("country_code") or "").upper() or None, ident_l)
                if lat is not None: ident_l.append("LAT: {:.5f}°".format(round(lat, 5)))
                if lon is not None: ident_l.append("LON: {:.5f}°".format(round(lon, 5)))
                if lat is not None and lon is not None:
                    ident_l.extend(_mgrs_lines(lat, lon))
                ident_l.append("SRC: {}".format(src))
                _sec("SURVEILLANCE CAMERA", ident_l)
            elif feat:               # OSM geo feature
                ident_l = []
                if time_str: ident_l.append("TIME: {}".format(time_str))
                feat_label = {"aerodrome": "AERODROME", "port": "PORT",
                              "military": "MILITARY BASE", "station": "STATION"}.get(feat, feat.upper())
                ident_l.append("TYPE: {}".format(feat_label))
                _r("NAME",    track.get("name"),    ident_l)
                _r("ICAO",    track.get("icao") or track.get("aerodrome_icao"), ident_l)
                _r("IATA",    track.get("iata"),    ident_l)
                _r("COUNTRY", (track.get("country_code") or "").upper() or None, ident_l)
                if lat is not None: ident_l.append("LAT: {:.5f}°".format(round(lat, 5)))
                if lon is not None: ident_l.append("LON: {:.5f}°".format(round(lon, 5)))
                if lat is not None and lon is not None:
                    ident_l.extend(_mgrs_lines(lat, lon))
                ident_l.append("SRC: {}".format(src))
                _sec("GEO FEATURE", ident_l)
            else:                  # APRS or vehicle
                ident_l = []; kinem_l = []
                if time_str: ident_l.append("TIME: {}".format(time_str))
                _r("CALL", (track.get("callsign") or "").strip().upper() or None, ident_l)
                if lat is not None: ident_l.append("LAT: {:.5f}°".format(round(lat, 5)))
                if lon is not None: ident_l.append("LON: {:.5f}°".format(round(lon, 5)))
                if lat is not None and lon is not None:
                    ident_l.extend(_mgrs_lines(lat, lon))
                spd = _speed_ms(track)
                if spd:
                    kinem_l.append("HDG: {}°".format(int(_course(track))))
                    kinem_l.append("SPD: {} kt / {} km/h".format(
                        round(spd / 0.514444, 1), round(spd * 3.6, 1)))
                kinem_l.append("SRC: {}".format(src))
                _sec("IDENTITY",   ident_l)
                _sec("KINEMATICS", kinem_l)
    return "\n".join(lines)


def _hae(track: dict) -> float:
    for key, scale in (
        ("geo_alt_m",   1.0),
        ("alt_geom_ft", 0.3048),
        ("baro_alt_m",  1.0),
        ("alt_baro_ft", 0.3048),
        ("alt_3d_ft",   0.3048),   # CAT-048 I048/110 3D radar height
        ("alt_ft",      0.3048),   # FR24
        ("alt_m",       1.0),
        ("alt_km",      1000.0),   # n2yo satellites
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
        # b64image only — ATAK ignores b64image when iconsetpath is also present
        # but the iconsetpath file doesn't exist, falling back to the MIL-STD symbol.
        ET.SubElement(detail, "usericon", {"b64image": icon_b64})
    ET.SubElement(detail, "contact", {"callsign": cs})
    spd_cot = _speed_ms(track)
    crs_cot = _course(track)
    ET.SubElement(detail, "track", {
        "speed":  str(spd_cot),
        "course": str(crs_cot),
    })
    sweep_az = track.get("sweep_azimuth_deg")
    rng = int(track.get("radar_range_m", 200_000))
    if sweep_az is not None:
        # Radar dish sweep: narrow 5° beam rotating around the site
        ET.SubElement(detail, "sensor", {
            "vfov": "1", "hfov": "5",
            "range": str(rng),
            "azimuth": str(int(round(float(sweep_az)))),
            "model": "Generic", "ranges": "0",
            "type": "radar",
            "displayMagneticReference": "0",
            "stockTool": "false",
        })
    elif track.get("sensor_type") == "radar":
        # Static radar site: show full 360° coverage circle
        ET.SubElement(detail, "sensor", {
            "vfov": "90", "hfov": "360",
            "range": str(rng),
            "azimuth": "0",
            "model": "Generic", "ranges": "0",
            "type": "radar",
            "displayMagneticReference": "0",
            "stockTool": "false",
        })
    elif track.get("radius_km", 0) > 0:
        # GPS jamming / EW threat area — render as a threat circle
        threat_range_m = int(float(track["radius_km"]) * 1000)
        ET.SubElement(detail, "sensor", {
            "vfov": "90", "hfov": "360",
            "range": str(threat_range_m),
            "azimuth": "0",
            "model": "Generic", "ranges": "0",
            "type": "radar",
            "displayMagneticReference": "0",
            "stockTool": "false",
        })
    elif spd_cot > 0:
        ET.SubElement(detail, "sensor", {
            "vfov": "45", "hfov": "360",
            "range": "0", "azimuth": str(int(crs_cot)),
            "model": "Generic", "ranges": "0",
            "type": "radar",
            "displayMagneticReference": "0",
            "stockTool": "false",
        })
    ET.SubElement(detail, "remarks").text = _build_remarks(track, cot_type)

    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(event, encoding="unicode")


# ---------------------------------------------------------------------------
# TCP sender — persistent connection with auto-reconnect
# ---------------------------------------------------------------------------

class TcpSender:
    """Thread-safe TCP writer with reconnect. Supports plaintext and mutual TLS.

    Accepts multiple (host, port) candidates — e.g. a LAN IP and a NetBird mesh
    IP for the same TAK Server. Tries them in round-robin order on each connect
    attempt, so it converges on whichever network path is actually reachable.
    """

    def __init__(self, hosts: list[tuple[str, int]], tls: bool = False,
                 certfile: str | None = None, keyfile: str | None = None,
                 cafile: str | None = None):
        self.hosts     = hosts
        self._idx      = 0
        self._tls      = tls
        self._certfile = certfile
        self._keyfile  = keyfile
        self._cafile   = cafile
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        host, port = self.hosts[self._idx % len(self.hosts)]
        try:
            raw = socket.create_connection((host, port), timeout=SEND_TIMEOUT_S)
            if self._tls:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                # Server identity is anchored by mutual TLS (client cert + shared CA),
                # not hostname — we deliberately dial multiple candidate addresses
                # (LAN IP, NetBird IP) for the same server, so no single hostname/IP
                # could ever match the server cert's SAN. Chain verification stays on.
                ctx.check_hostname = False
                ctx.load_verify_locations(self._cafile)  # cafile required; enforced in run()
                if self._certfile and self._keyfile:
                    ctx.load_cert_chain(self._certfile, self._keyfile)
                s = ctx.wrap_socket(raw, server_hostname=host)
            else:
                s = raw
            s.settimeout(SEND_TIMEOUT_S)
            with self._lock:
                self._sock = s
            mode = "TLS" if self._tls else "TCP"
            print("TAK {} connected → {}:{}".format(mode, host, port), flush=True)
        except OSError as exc:
            print("TAK connect failed ({}:{}) — {}, trying next candidate in {}s".format(
                host, port, exc, RECONNECT_S), flush=True)
            self._sock = None
            self._idx += 1
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
            self._idx += 1
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
    """Sends each CoT event to every configured destination (dual-homed fallback).

    UDP has no connection state, so there's nothing to "detect" and fail over —
    instead, every destination gets every packet. Whichever network path (LAN,
    NetBird mesh, multicast) is actually up delivers it; unreachable paths just
    drop silently (or log once) without blocking the others.
    """

    def __init__(self, dests: list[tuple[str, int]]):
        self.dests = dests
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if any(addr.startswith("224.") or addr.startswith("239.") for addr, _ in dests):
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 32))
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    def send(self, xml: str):
        data = xml.encode("utf-8")
        for dest in self.dests:
            try:
                self._sock.sendto(data, dest)
            except OSError as exc:
                print("CoT UDP send error ({}:{}): {}".format(dest[0], dest[1], exc), flush=True)

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
        # Raw sensor sources and ADS-B relay sources must pass through
        # track_fusion_bridge before reaching ATAK.  Exception: anything arriving
        # on an air/fused/** topic was already processed by the fusion bridge and
        # must be allowed through — this includes ADS-B fallback tracks that the
        # fusion bridge publishes when no radar is covering that aircraft.
        src = track.get("_src", "")
        key = str(sample.key_expr)
        if "/fused/" not in key and (src in _ADS_B_RELAY_SOURCES or src in _RAW_SENSOR_SOURCES):
            return
        # ATC towers / ground vehicles show up in ADS-B with "TWR", "GND" etc.
        # Reclassify as neutral ground radar/radio station instead of aircraft.
        if _is_ground_station(track):
            cot_type     = "a-n-G-E-S-R"
            stale_s_used = LAND_STALE_S
        else:
            cot_type     = cot_type_or_fn(track) if callable(cot_type_or_fn) else cot_type_or_fn
            stale_s_used = stale_s
            # Emergency squawk → force red hostile + one-shot GeoChat alert
            sq = str(track.get("squawk") or "")
            if sq in _EMERGENCY_SQUAWK and "-A-" in cot_type:
                cot_type = "a-h-A-C-F"
                uid_now = _uid(track)
                with _alert_lock:
                    fire = uid_now not in _alerted
                    _alerted.add(uid_now)
                if fire:
                    lat_a = track.get("lat_deg", 0)
                    lon_a = track.get("lon_deg", 0)
                    cs_a  = (track.get("callsign") or track.get("registration") or
                             track.get("icao24") or "UNKNOWN").upper()
                    alt_a = int(_hae(track) / 0.3048)
                    msg   = "[{}] {} {} - squawk {} - FL{} - {:.3f}/{:.3f}".format(
                        _EMERGENCY_SQUAWK[sq], sq, cs_a, sq,
                        alt_a // 100, lat_a, lon_a)
                    _send_geochat_alert(sender, uid_now, lat_a, lon_a, msg)
            else:
                # Clear alert state when squawk returns to normal
                uid_now = _uid(track)
                with _alert_lock:
                    _alerted.discard(uid_now)

        # Ship distress alert (nav_status)
        nav_key = str(track.get("nav_status") or "").lower().replace(" ", "_")
        if nav_key in _DISTRESS_NAV and "-S-" in cot_type:
            uid_now = _uid(track)
            with _alert_lock:
                fire = uid_now not in _alerted
                _alerted.add(uid_now)
            if fire:
                lat_s  = track.get("lat_deg", 0)
                lon_s  = track.get("lon_deg", 0)
                name_s = (track.get("ship_name") or str(track.get("mmsi") or "VESSEL")).upper()
                msg    = "[SOS] {} - {} - MMSI {} - {:.3f}/{:.3f}".format(
                    nav_key.upper().replace("_", " "), name_s,
                    track.get("mmsi", "?"), lat_s, lon_s)
                _send_geochat_alert(sender, uid_now, lat_s, lon_s, msg)

        xml = track_to_cot(track, cot_type, stale_s=stale_s_used)
        if xml is None:
            return
        sender.send(xml)

        # Update dead-reckoning state (skip extrapolated updates to prevent feedback)
        uid = _uid(track)
        lat = track.get("lat_deg")
        lon = track.get("lon_deg")
        now = float(track.get("_ts", time.time()))
        if lat is not None and lon is not None and not track.get("_extrap"):
            with _dr_lock:
                _dr_store[uid] = {
                    "track":    track,
                    "cot_type": cot_type,
                    "stale_s":  stale_s_used,
                    "base_ts":  now,
                    "base_lat": lat,
                    "base_lon": lon,
                }

        if verbose:
            cs = track.get("callsign") or track.get("registration") or track.get("mmsi") or "?"
            print("CoT {} {}".format(cot_type, cs), flush=True)
    return handler


def make_radar_status_handler(sender, verbose: bool):
    """Handler for CAT-34 radar sensor status topics.
    Stores status for AIR stat card enrichment and forwards a CoT radar-site marker."""
    def handler(sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        sac = track.get("sac"); sic = track.get("sic")
        if sac is not None:
            key = "{}-{}".format(sac, sic if sic is not None else 0)
            with _radar_status_lock:
                # Only update enrichment store from full status (not sweep ticks)
                if "sweep_azimuth_deg" not in track:
                    _radar_status[key] = track
        if "sweep_azimuth_deg" in track:
            # Sweep tick: send a SEPARATE beam entity (different UID) so the site
            # marker keeps its permanent 360° coverage ring at the same time.
            rot = float(track.get("rotation_s") or 4.0)
            beam_track = dict(track)
            beam_track["sensor_id"] = "BEAM-" + str(track.get("sensor_id", "RADAR"))
            beam_track["sensor_name"] = (track.get("sensor_name") or "RADAR") + " BEAM"
            xml = track_to_cot(beam_track, "a-f-G-E-S-R", stale_s=max(rot * 0.6, 1.0))
        else:
            # Full status: site marker with permanent 360° coverage ring
            xml = track_to_cot(track, "a-f-G-E-S-R", stale_s=LAND_STALE_S * 2)
        if xml:
            sender.send(xml)
        if verbose:
            print("CoT a-f-G-E-S-R {}  psr={} ssr={} mds={}".format(
                track.get("sensor_name", "RADAR"),
                track.get("psr_status", "-"), track.get("ssr_status", "-"),
                track.get("mds_status", "-")), flush=True)
    return handler


def make_geo_handler(sender, verbose: bool):
    """Handler for OSM land/geo features — maps feature_type to CoT type with 24h stale.
    Hostile country (RU/BY) features are flipped to a-h- affiliation."""
    def handler(sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        feature_type = track.get("feature_type", "")
        base_type = _OSM_COT.get(feature_type, "a-n-G-I")
        cot_type  = _geo_cot_type(track, base_type)
        xml = track_to_cot(track, cot_type, stale_s=GEO_STALE_S)
        if xml is None:
            return
        sender.send(xml)
        if verbose:
            cc = track.get("country_code", "??")
            print("CoT {} {} {} [{}]".format(
                cot_type, feature_type, track.get("name", "?"), cc), flush=True)
    return handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    hosts = [(h, args.port) for h in args.host]
    if args.udp:
        sender = UdpSender(hosts)
        print("CoT → UDP {}".format(", ".join("{}:{}".format(h, p) for h, p in hosts)), flush=True)
    else:
        tls = getattr(args, "tls", False)
        if tls and not getattr(args, "ca", None):
            raise SystemExit("--ca / TAK_CA is required when --tls is specified")
        sender = TcpSender(hosts,
                           tls=tls,
                           certfile=getattr(args, "cert", None),
                           keyfile=getattr(args, "key", None),
                           cafile=getattr(args, "ca", None))
        mode = "TLS mTLS" if tls else "TCP plaintext"
        print("CoT → {} candidates: {} (TAK Server, first reachable wins)".format(
            mode, ", ".join("{}:{}".format(h, p) for h, p in hosts)), flush=True)

    session = zenoh.open(make_config())
    _start_dr_thread(sender)
    subs = []
    for suffix, (cot_type, stale_s) in _TOPIC_COT.items():
        key = "{}/{}".format(TOPIC_ROOT, suffix)
        fn = cot_type.__name__ if callable(cot_type) else str(cot_type)
        subs.append(session.declare_subscriber(
            key, make_handler(cot_type, sender, args.verbose, stale_s=stale_s)))
        print("SUB {} → {} stale={}s".format(key, fn, stale_s), flush=True)

    # Geo features (aerodromes, ports, military bases) — 24h stale
    # Matches: land/{vendor}/{protocol}/neutral/geo/features/v1
    geo_key = "{}/land/**/neutral/geo/**".format(TOPIC_ROOT)
    subs.append(session.declare_subscriber(geo_key, make_geo_handler(sender, args.verbose)))
    print("SUB {} → [geo features, 24h stale]".format(geo_key), flush=True)

    # Radar sensor site status (CAT-34) — updates _radar_status dict + renders CoT marker
    radar_key = "{}/land/asterix/cat34/neutral/radar/**".format(TOPIC_ROOT)
    subs.append(session.declare_subscriber(radar_key,
                make_radar_status_handler(sender, args.verbose)))
    print("SUB {} → [radar sensor sites]".format(radar_key), flush=True)

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
    ap.add_argument("--host", action="append", default=None,
                    help="TAK Server host — repeatable (e.g. --host <lan-ip> --host <netbird-ip>) "
                         "to try multiple network paths; falls back to TAK_HOST env or 127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TAK_PORT", "8087")),
                    help="TAK Server port (default: 8087)")
    ap.add_argument("--tls", action="store_true",
                    default=os.environ.get("TAK_TLS", "") == "1",
                    help="Enable mutual TLS — Option B, port 8089 (requires --cert, --key, --ca)")
    ap.add_argument("--cert", default=os.environ.get("TAK_CERT"),
                    help="Client certificate PEM (mTLS Option B)")
    ap.add_argument("--key", default=os.environ.get("TAK_KEY"),
                    help="Client private key PEM (mTLS Option B)")
    ap.add_argument("--ca", default=os.environ.get("TAK_CA"),
                    help="CA certificate PEM (mTLS Option B)")
    ap.add_argument("--udp", action="store_true",
                    help="Use UDP instead of TCP (for direct multicast/unicast, no TAK Server)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each CoT message sent")
    args = ap.parse_args()
    if not args.host:
        args.host = [os.environ.get("TAK_HOST", "127.0.0.1")]
    run(args)


if __name__ == "__main__":
    main()
