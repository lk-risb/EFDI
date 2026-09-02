#!/usr/bin/env python3
"""tak_layer.py — Zenoh EFDI track topics → TAK Server / ATAK CoT bridge.

Subscribes to all EFDI track topics and forwards position updates as
Cursor-on-Target (CoT) XML to a TAK Server over TCP.
All connected ATAK / iTAK / TAKX / WinTAK devices see the tracks automatically
through the server — no per-device configuration, no multicast required.

Transport — mutual TLS on the official TAK Server streaming port 8089:
  --host <ip> --port 8089 --tls --cert cert.pem --key key.pem --ca ca.pem
  Generate certs with: make add-service NAME=efdi-pod  (in the TAK repo)

Do NOT use port 8087. That is the anonymous `stdtcp` input: TAK Server accepts
and ACKs every byte written to it, but does not distribute those events to the
8089 subscribers, so nothing ever reaches WinTAK/ATAK. Only an authenticated
8089 connection lands in the __ANON__ group the clients are subscribed to.
Plaintext TCP to another port stays supported (omit --tls) for a local relay.

Zenoh topics consumed (5 main categories):
  AIR:   <ORG>/air/civ/json/tracks    → CoT a-f-A-C-F / a-h-A-C-F (civil, hostile if RU/BY)
         <ORG>/air/mil/json/tracks    → CoT a-n-A-M-F / a-h-A-M-F (military, hostile if RU/BY)
         <ORG>/air/radar/json/tracks  → CoT a-u-A     (radar return, unidentified)
         <ORG>/air/sapient/fused/json/tracks→ CoT a-u-A     (SAPIENT sensor track)
  LAND:  <ORG>/land/civ/json/tracks   → CoT a-f-G-E-V-C (friendly ground vehicle)
         <ORG>/land/nffi/json/tracks  → CoT a-f-G-U-C (NATO NFFI friendly forces)
  SEA:   <ORG>/sea/civ/json/tracks    → CoT a-f-S-X-L / a-h-S-X-L (hostile if RU/BY MMSI)
  SPACE: <ORG>/space/json/tracks      → CoT a-f-P     (satellite)

Run:
    venv/bin/python3 tak_layer.py --host 100.64.x.x --port 8089 --tls       # mTLS → official TAK Server
        --cert cert.pem --key key.pem --ca ca.pem
"""

import argparse
import base64
import json
import math
import os
import queue
import re
import signal
import socket
import ssl
import struct
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from namespace_prefix import topic_root
from protocols.gateway import open_session, subscribe

try:
    import mgrs as _mgrs_lib
    _MGRS    = _mgrs_lib.MGRS()
    _MGRS_RE = re.compile(r'^(\d{1,2}[A-Z])([A-Z]{2})(.*)$')
except Exception:
    _MGRS = None
    _MGRS_RE = None

TOPIC_ROOT = topic_root()
# Prefer the local router (plaintext, no TLS handshake over relay) when running
# inside the compose stack. Falls back to the remote router for standalone use.

# Base lifetime for high-rate sources.
AIR_STALE_S    = 120
SEA_STALE_S    = 300     # vessels: Class B sends every 30-180s; 5 min covers worst case
LAND_STALE_S   = 120     # ground vehicles
COT_STALE_S    = AIR_STALE_S  # default (air)
SAT_STALE_S    = 300     # satellites: polled every 60s, 5 min gives 5× margin
ENV_STALE_S    = 3600    # weather stations: polled every 15–30 min, 1 h gives plenty of margin
RECONNECT_S    = 5
SEND_TIMEOUT_S = 10
TAK_QUEUE_MAX  = 10000   # bounded CoT backlog; drops oldest when a link stalls

# Dead-reckoning — extrapolate position forward when sensor updates stop
_DR_TICK_S   = 2.0   # extrapolation interval (seconds)
_DR_MIN_MS   = 5.15  # don't extrapolate below ~10 kt (5.15 m/s)

# Emergency squawk codes (ICAO Annex 10)
_EMERGENCY_SQUAWK = {"7500": "HIJACK", "7600": "COMMS FAILURE", "7700": "MAYDAY"}

# CAT-48 I048/020 target report descriptor TYP subfield (cat.py's _TYP048) —
# which sensor(s) actually produced this specific detection, not the track's
# overall sensor mix (that's track_sensor below).
_DETECTION_TYPE_LABEL = {
    "no_detection": "NO DETECTION (coasted)",
    "psr": "PSR ONLY",
    "ssr": "SSR ONLY",
    "ssr_psr": "SSR + PSR",
    "mode_s_all_call": "MODE-S ALL-CALL",
    "mode_s_roll_call": "MODE-S ROLL-CALL",
    "mode_s_all_call_psr": "MODE-S ALL-CALL + PSR",
    "mode_s_roll_call_psr": "MODE-S ROLL-CALL + PSR",
}

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

# `civ`/`mil` affiliation means "civilian traffic"/"military traffic" with no
# posture judgment implied — real friend/hostile/neutral classification comes
# from the actual data (SAPIENT classification, IFF, an explicit source
# affiliation field) and is rendered through the dedicated hostile/friendly/
# neutral topic entries below. Nationality is never a hostility signal on its
# own, so these render as CoT's neutral affiliation unconditionally.
_CIV_AIR_TYPE = "a-n-A-C-F"
_MIL_AIR_TYPE = "a-n-A-M-F"

def _unknown_air_type(track: dict) -> str:
    object_class = str(
        track.get("sapient_class")
        or track.get("remote_id_ua_type")
        or track.get("utm_vehicle_type")
        or track.get("aartos_category")
        or ""
    ).lower()
    if any(token in object_class for token in ("drone", "uas", "uav", "quadcopter")):
        return "a-u-A-M-F-Q"
    return "a-u-A-C-F"

_CIV_SEA_TYPE = "a-n-S-X-L"

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


# Sources that must NOT reach ATAK directly — they must pass through
# fusion (compose/protocols/fusion.py) first so the marker contains merged data.
#
# ADS-B relay sources: identity-only enrichment inputs, blocked until fused.
# Raw sensor sources: kinematics are good but no identity; fusion adds
# REG/ICAO/SQWK. Blocked here because fusion re-keys them — a CAT-48 track is
# EFDI-RAD-<sac/sic/num> raw but EFDI-ICAO-<hex> once identified, so letting
# both through really would draw the same contact twice.
#
_RAW_SENSOR_SOURCE_PREFIXES = ("ASTERIX CAT-48", "ASTERIX CAT-20")


def _is_unfused_sensor_track(track: dict, key: str) -> bool:
    """True for a raw radar/MLAT track that must first pass through fusion."""
    if "/fused/" in key:
        return False
    source = str(track.get("_src", ""))
    return any(source.startswith(prefix) for prefix in _RAW_SENSOR_SOURCE_PREFIXES)

# Schema: {domain}/{source}/{modality}/{affiliation}/{entity}/{type}/{id}/{view}
# Wildcards: ** matches zero-or-more segments, so air/**/civ/aircraft/** catches
# any source+modality combination under civil air, and absorbs the trailing
# {type}/{id}/{view} without naming them.
# NOTE: air/trackfusion/fused/** is caught by the broad air/** wildcards below —
# no separate fused entries needed. They also catch SAPIENT, which
# do not go through the radar fusion path.
_TOPIC_COT = {
    # AIR — affiliation slot drives CoT type. Covers fused tracks
    # (air/trackfusion/fused/**) + SAPIENT.
    # Raw CAT-48 / CAT-20 are dropped by _RAW_SENSOR_SOURCES check in make_handler.
    "air/**/civ/aircraft/**":    (_CIV_AIR_TYPE,  AIR_STALE_S),
    "air/**/mil/aircraft/**":    (_MIL_AIR_TYPE,  AIR_STALE_S),
    "air/**/unknown/**":         (_unknown_air_type, AIR_STALE_S),
    # LAND — full affiliation matrix for SitaWare / NFFI
    "land/**/civ/vehicle/**":    ("a-f-G-E-V-C", LAND_STALE_S),
    "land/**/neutral/station/**":("a-n-G-I-R",   LAND_STALE_S),
    "land/**/friendly/unit/**":  ("a-f-G-U-C",   LAND_STALE_S),
    "land/**/hostile/unit/**":   ("a-h-G-U-C",   LAND_STALE_S),
    "land/**/neutral/unit/**":   ("a-n-G-U-C",   LAND_STALE_S),
    "land/**/unknown/unit/**":   ("a-u-G-U-C",   LAND_STALE_S),
    "land/**/unknown/vehicle/**":("a-u-G-E-V",   LAND_STALE_S),
    "land/**/unknown/person/**": ("a-u-G-U-C-I", LAND_STALE_S),
    "land/**/unknown/sensor/**": ("a-u-G-E-S",   LAND_STALE_S),
    "land/**/neutral/zone/**":   ("a-n-G-I-R",    LAND_STALE_S),
    "land/**/neutral/alert/**":  ("a-n-G-I-R",    LAND_STALE_S),
    # AIR — full affiliation matrix
    "air/**/friendly/aircraft/**": ("a-f-A-M-F",   AIR_STALE_S),
    "air/**/friendly/uav/**":      ("a-f-A-M-F-Q", AIR_STALE_S),
    "air/**/hostile/aircraft/**":  ("a-h-A-M-F",   AIR_STALE_S),
    "air/**/neutral/aircraft/**":  ("a-n-A-M-F",   AIR_STALE_S),
    "air/**/hostile/uav/**":       ("a-h-A-M-F-Q", AIR_STALE_S),
    # SEA — full affiliation matrix
    "sea/**/civ/vessel/**":      (_CIV_SEA_TYPE,  SEA_STALE_S),
    "sea/**/mil/vessel/**":      ("a-n-S-W-C",   SEA_STALE_S),
    "sea/**/friendly/vessel/**": ("a-f-S-X-L",   SEA_STALE_S),
    "sea/**/hostile/vessel/**":  ("a-h-S-X-L",   SEA_STALE_S),
    "sea/**/neutral/vessel/**":  ("a-n-S-X-L",   SEA_STALE_S),
    "sea/**/unknown/vessel/**":  ("a-u-S-X-L",   SEA_STALE_S),
    # SPACE
    "space/**/civ/satellite/**": ("a-f-P",        SAT_STALE_S),
    "space/**/friendly/satellite/**": ("a-f-P",   SAT_STALE_S),
    "space/**/hostile/satellite/**":  ("a-h-P",   SAT_STALE_S),
    "space/**/neutral/satellite/**":  ("a-n-P",   SAT_STALE_S),
    "space/**/unknown/satellite/**":  ("a-u-P",   SAT_STALE_S),
    # ENV — weather stations and air quality sensors show as ground icons
    "env/weather/station/**":    ("a-n-G-I-R",   ENV_STALE_S),
    # ACOUSTIC / RF SENSOR SITES — sensor box; recolors green/yellow/red by
    # last_detection_ts (see _sensor_alert_cot_type), same icon throughout
    "land/**/neutral/sensor/**": (_sensor_alert_cot_type, LAND_STALE_S * 2),
}

# CAT-34 radar status has a dedicated subscriber below because it also updates
# the radar-enrichment store and distinguishes the site from its sweep beam.
# Keep it out of _TOPIC_COT: subscribing through both paths sends the same UID
# twice with competing affiliations, which makes TAK clients flicker, replace,
# or omit the marker depending on update order.
_RADAR_COT_TYPE = "a-n-G-E-S-R"
# A radar site publishing "passive": true (VERA-NG's passive coherent
# location, AARTOS's RF direction-finding antennas — neither transmits, both
# listen) gets the standard CoT type for that, distinct from an emitting
# radar. See CoTtypes.xml's G-U-U-M-S-E subtree (SIGINT/Electronic Warfare).
_RADAR_COT_TYPE_PASSIVE = "a-n-G-U-U-M-S-E-D"


def _radar_cot_type(track: dict) -> str:
    return _RADAR_COT_TYPE_PASSIVE if track.get("passive") else _RADAR_COT_TYPE

# ATC / ground-station callsigns that appear in ADS-B feeds.
# Transponders belonging to ATC towers, ground vehicles, ATIS etc. show flight ID "TWR",
# "GND", "ATIS" etc.  We reclassify them as neutral ground radar/radio stations instead
# of aircraft so they don't pollute the air picture.
_ATC_EXACT  = frozenset(["TWR", "GND", "ATIS", "APP", "DEP", "APCH", "CTR", "OPS",
                          "RAMP", "CARGO", "FUEL", "FIRE", "MAINT", "VAGON"])
_ATC_SUFFIX = ("TWR", "GND", "ATIS", "APP", "CTR")
_ADS_B_SURFACE_VEHICLE_CATEGORIES = frozenset({"C1", "C2"})


def _is_adsb_surface_vehicle(track: dict) -> bool:
    category = track.get("emitter_category_str") or track.get("category") or ""
    return str(category).strip().upper() in _ADS_B_SURFACE_VEHICLE_CATEGORIES

def _is_ground_station(track: dict) -> bool:
    cs = (track.get("callsign") or "").strip().upper()
    if cs in _ATC_EXACT:
        return True
    return any(cs.endswith(s) for s in _ATC_SUFFIX) and len(cs) <= 8

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
#
# Off by default: WinTAK cannot resolve an icon from b64image alone and both
# mis-draws and crashes on hover (see track_to_cot). Set COT_USERICON=1 for an
# ATAK-only audience that wants the custom art.
_USERICON_ENABLED = os.environ.get("COT_USERICON", "") == "1"

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


# ponytail: bounding-box check instead of a real geo→timezone lookup (e.g. timezonefinder) —
# every sensor this pod currently carries is in Lithuania. Swap for timezonefinder (per-track
# IANA name from lat/lon) if a non-Lithuania sensor is ever added.
_LT_BBOX = (53.9, 56.5, 20.9, 26.9)  # lat_min, lat_max, lon_min, lon_max


def _local_clock_suffix(ts, lat, lon) -> str:
    """Local wall-clock next to UTC for stat cards, e.g. '  (17:32:07 EEST)'."""
    if ts is None or lat is None or lon is None:
        return ""
    lat_min, lat_max, lon_min, lon_max = _LT_BBOX
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        return ""
    try:
        local = datetime.fromtimestamp(float(ts), tz=ZoneInfo("Europe/Vilnius"))
        return "  ({} {})".format(local.strftime("%H:%M:%S"), local.strftime("%Z"))
    except Exception:
        return ""


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


def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _uid(track: dict) -> str:
    # Use the stable radio identifier — source-agnostic so the same
    # aircraft/vessel reported by multiple APIs merges to one ATAK point.
    for key, id_prefix in (
        ("icao24",    "ICAO"),   # same address across ADS-B aggregators
        ("mmsi",      "MMSI"),   # same MMSI from all AIS feeds
        ("sat_id",    "SAT"),    # satellite catalogue number
        ("radar_id",  "RAD"),    # CAT-48 PSR track (no Mode-S) — SAC/SIC/track_num
        ("sensor_id", "SENS"),
        ("uid",       "UID"),
    ):
        v = track.get(key)
        if v:
            return "EFDI-{}-{}".format(id_prefix, str(v).upper())
    src = track.get("_src", "efdi")
    cs = (track.get("callsign") or "").strip()
    if cs:
        return "EFDI-{}-{}".format(src, cs)
    return "EFDI-{}-{:.5f}-{:.5f}".format(src, track.get("lat_deg", 0), track.get("lon_deg", 0))


def _callsign(track: dict, uid: str) -> str:
    # Named vessels, satellites, and sensors
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
        if track.get("squawk_not_extracted"):
            iff_l.append("[SQUAWK NOT EXTRACTED]")
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
        lat = track.get("lat_deg"); lon = track.get("lon_deg")
        if tod is not None:
            h = int(tod // 3600) % 24; m = int((tod % 3600) // 60); s = tod % 60
            kinem_l.append("TOD (time of detection): {:02d}:{:02d}:{:04.1f} UTC  {}".format(h, m, s, age).rstrip())
        else:
            ts = track.get("_ts")
            if ts:
                kinem_l.append("TIME: {}{}  {}".format(
                    datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"),
                    _local_clock_suffix(ts, lat, lon), age).rstrip())
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
        if rssi is None:
            rssi = track.get("rssi_dbfs")
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
        det_type = track.get("detection_type")
        if det_type:
            radar_l.append("DET (detection type): {}".format(
                _DETECTION_TYPE_LABEL.get(det_type, det_type.upper())))
        trk_sensor = track.get("track_sensor")
        if trk_sensor and trk_sensor != "combined":
            radar_l.append("SENSOR (tracker source): {}".format(trk_sensor.upper()))
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
        if track.get("track_doubtful"):  status_l.append("[TRACK DOUBTFUL]")
        if track.get("field_monitor"):   status_l.append("[FIELD MONITOR]")
        if track.get("supported_by_neighbour_node"): status_l.append("[SUPPORTED BY NEIGHBOUR NODE]")
        if track.get("slant_range_correction"):       status_l.append("[SLANT RANGE CORRECTED]")
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
        lat = track.get("lat_deg"); lon = track.get("lon_deg")
        ts = track.get("_ts")
        if ts:
            age = _track_age(track)
            ident_l.append("TIME: {}{}  {}".format(
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"),
                _local_clock_suffix(ts, lat, lon), age).rstrip())
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

        lat = track.get("lat_deg"); lon = track.get("lon_deg")
        ts = track.get("_ts")
        if ts:
            age = _track_age(track)
            ident_l.append("TIME: {}{}  {}".format(
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"),
                _local_clock_suffix(ts, lat, lon), age).rstrip())
        _r = lambda lbl, v, b: b.append("{}: {}".format(lbl, v)) if v not in (None, "", 0.0) else None
        _r("NORAD", track.get("sat_id") or track.get("sensor_id") or track.get("norad_id"), ident_l)
        _r("NAME",  track.get("sat_name") or track.get("name"), ident_l)

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

    else:                # ---- GROUND / ENV -------------------------------
        def _sec(title, buf):
            if buf:
                lines.append("─── {} ───".format(title))
                lines.extend(buf)
        _r = lambda lbl, v, b: b.append("{}: {}".format(lbl, v)) if v not in (None, "", 0.0) else None

        lat = track.get("lat_deg"); lon = track.get("lon_deg")
        ts = track.get("_ts")
        if ts:
            age      = _track_age(track)
            time_str = ("{}{}  {}".format(
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC"),
                _local_clock_suffix(ts, lat, lon), age)).rstrip()
        else:
            time_str = None

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
                sensor_l.append("TIME: {}{}  {}".format(
                    datetime.fromtimestamp(float(ts_r), tz=timezone.utc).strftime("%H:%M:%S UTC"),
                    _local_clock_suffix(ts_r, lat, lon), age_str))
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
            # Coverage ring — label its provenance so a configured instrumented
            # maximum is never mistaken for measured, terrain-limited coverage.
            rng_m = track.get("radar_range_m")
            try:
                cov_km = float(rng_m) / 1000.0 if rng_m else 0.0
            except (TypeError, ValueError):
                cov_km = 0.0
            if cov_km > 0:
                rsrc = track.get("radar_range_source")
                if rsrc == "configured":
                    note = "  (configured instrumented max — not terrain/target coverage)"
                elif rsrc == "advertised":
                    note = "  (advertised by radar, I034/100)"
                else:
                    note = ""
                sensor_l.append("COVERAGE: {:.0f} km{}".format(cov_km, note))
            # STATUS
            for k, lbl in (("psr_status","PSR"), ("ssr_status","SSR"), ("mds_status","MODE-S")):
                v = track.get(k)
                if v: status_l.append("{}: {}".format(lbl, v.upper().replace("_"," ")))
            if track.get("sys_nogo"):        status_l.append("[SYSTEM DEGRADED]")
            if track.get("sys_ovl_rdp"):     status_l.append("[RDP OVERLOAD]")
            if track.get("sys_ovl_xmt"):     status_l.append("[TX OVERLOAD]")
            if track.get("sys_tsv_invalid"): status_l.append("[TIME SOURCE INVALID]")
            rdp_red = track.get("rdp_reduction_level")
            xmt_red = track.get("xmt_reduction_level")
            if rdp_red: status_l.append("RDP REDUCTION LEVEL: {}".format(rdp_red))
            if xmt_red: status_l.append("TX REDUCTION LEVEL: {}".format(xmt_red))
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

        elif src == "meteo-lt":   # WEATHER
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

        else:                      # vehicles and other ground entities
            ident_l = []; kinem_l = []
            if time_str: ident_l.append("TIME: {}".format(time_str))
            _r("CALL", (track.get("callsign") or "").strip().upper() or None, ident_l)
            if track.get("tak_user"):
                _r("TEAM", track.get("team"), ident_l)
                _r("ROLE", track.get("role"), ident_l)
                _r("PLATFORM", track.get("tak_platform"), ident_l)
                _r("DEVICE", track.get("tak_device"), ident_l)
                _r("APP VERSION", track.get("tak_version"), ident_l)
                _r("OS", track.get("tak_os"), ident_l)
            if lat is not None: ident_l.append("LAT: {:.5f}°".format(round(lat, 5)))
            if lon is not None: ident_l.append("LON: {:.5f}°".format(round(lon, 5)))
            if lat is not None and lon is not None:
                ident_l.extend(_mgrs_lines(lat, lon))
            if track.get("alt_m") is not None:
                altitude_m = float(track["alt_m"])
                kinem_l.append("ALT: {} ft / {} m".format(
                    round(altitude_m / 0.3048), round(altitude_m)
                ))
            spd = _speed_ms(track)
            if spd:
                kinem_l.append("HDG: {}°".format(int(_course(track))))
                kinem_l.append("SPD: {} kt / {} km/h".format(
                    round(spd / 0.514444, 1), round(spd * 3.6, 1)))
            _r("POSITION SOURCE", track.get("position_source"), kinem_l)
            _r("ALTITUDE SOURCE", track.get("altitude_source"), kinem_l)
            if track.get("ce_m") is not None:
                kinem_l.append("POSITION ACCURACY: {} m CE".format(track["ce_m"]))
            if track.get("le_m") is not None:
                kinem_l.append("VERTICAL ACCURACY: {} m LE".format(track["le_m"]))
            if track.get("battery_pct") is not None:
                kinem_l.append("BATTERY: {}%".format(track["battery_pct"]))
            if track.get("remarks"):
                kinem_l.append("REMARKS: {}".format(track["remarks"]))
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
        ("alt_ft",      0.3048),   # generic feet-based source fallback
        ("alt_m",       1.0),
        ("alt_km",      1000.0),   # satellites
    ):
        v = track.get(key)
        if v is not None:
            try:
                number = float(v) * scale
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and number != 0:
                return round(number, 1)
    return 9999999.0


def _ce(track: dict) -> float:
    """Circular position error (CoT `ce`), meters — the radius TAK draws the
    uncertainty ring at. Real per-track accuracy where a category decodes
    one; CoT's own "unknown" sentinel otherwise, never a guess."""
    for key in ("position_uncertainty_m",  # CAT-205 I205/110/150 — already circular
                "position_accuracy_m"):    # CAT-021 I021/090 NACp -> EPU, ADS-B
        v = track.get(key)
        if v is None:
            continue
        try:
            number = float(v)
        except (TypeError, ValueError, OverflowError):
            number = None
        if number is not None and math.isfinite(number) and number >= 0:
            return round(number, 1)
    for x_key, y_key, scale in (
        ("sigma_x_m", "sigma_y_m", 1.0),            # CAT-010 I010/500
        ("track_sigma_x_nm", "track_sigma_y_nm", 1852.0),  # CAT-048 I048/210
    ):
        sx, sy = track.get(x_key), track.get(y_key)
        if sx is None or sy is None:
            continue
        try:
            radius = math.hypot(float(sx) * scale, float(sy) * scale)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(radius) and radius >= 0:
            return round(radius, 1)
    return 9999999.0


def _le(track: dict) -> float:
    """Linear (vertical) position error (CoT `le`), meters."""
    v = track.get("height_accuracy_m")  # CAT-011 I011/500, CAT-020 I020/500
    if v is not None:
        try:
            number = float(v)
        except (TypeError, ValueError, OverflowError):
            number = None
        if number is not None and math.isfinite(number) and number >= 0:
            return round(number, 1)
    return 9999999.0


def _speed_ms(track: dict) -> float:
    for key, scale in (
        ("speed_ms",        1.0),
        ("ground_speed_kts", 0.514444),  # generic ADS-B source
        ("speed_kts",        0.514444),  # generic knots-based source fallback
        ("sog_ms",           1.0),       # AIS
    ):
        v = track.get(key)
        if v is not None:
            try:
                number = float(v) * scale
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and number >= 0:
                return round(number, 2)
    return 0.0


def _course(track: dict) -> float:
    for key in ("heading_deg", "track_deg", "cog_deg"):  # cog_deg = AIS
        v = track.get(key)
        if v is not None:
            try:
                number = float(v)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number):
                return round(number % 360.0, 1)
    return 0.0


def _delete_point_cot(uid: str, ts: float | None = None) -> str:
    """Standard CoT "delete point" event (type t-x-d-d) — tells a TAK client
    to remove the marker for this uid immediately, whatever its real type
    was. Deliberately position-independent (dummy 0,0 point, already-stale
    timestamp): ATAK/WinTAK act on uid+type for this specific message, which
    is why it works for a bare {"uid", "_delete": true} tombstone that never
    carries a real lat/lon at all — unlike track_to_cot(), which requires one."""
    now = ts if ts is not None and math.isfinite(ts) else time.time()
    event = ET.Element("event", {
        "version": "2.0",
        "uid":     uid,
        "type":    "t-x-d-d",
        "how":     "m-g",
        "time":    _ts(now),
        "start":   _ts(now),
        "stale":   _ts(now),
    })
    ET.SubElement(event, "point", {"lat": "0.0", "lon": "0.0", "hae": "0.0", "ce": "9999999.0", "le": "9999999.0"})
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "link", {"relation": "p-p", "uid": uid, "type": "a-u-G"})
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(event, encoding="unicode")


def track_to_cot(track: dict, cot_type: str, stale_s: float = COT_STALE_S) -> str | None:
    lat = track.get("lat_deg")
    lon = track.get("lon_deg")
    if lat is None or lon is None:
        return None

    try:
        lat = float(lat)
        lon = float(lon)
        now = float(track.get("_ts", time.time()))
        stale_s = float(stale_s)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if not math.isfinite(now):
        now = time.time()
    if not math.isfinite(stale_s) or stale_s <= 0:
        return None
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
        "lat": str(round(lat, 6)),
        "lon": str(round(lon, 6)),
        "hae": str(_hae(track)),
        "ce":  str(_ce(track)),
        "le":  str(_le(track)),
    })
    detail = ET.SubElement(event, "detail")
    # Provenance marker for bidirectional C2 gateways. TAK Server sends a
    # client's own CoT back on its receive stream; tak_bridge retains that raw
    # frame for audit but does not normalize this exact fabric-export copy.
    # This avoids a C2 feedback loop without filtering unrelated UID prefixes.
    ET.SubElement(detail, "efdi", {"role": "fabric-export"})
    # CoT shapes are optional detail.  Keep the point anchor for consumers
    # that do not render shapes, then add polygon/line points for airspace,
    # routes, and sensor areas.
    geometry = track.get("geometry")
    if isinstance(geometry, dict):
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")

        def shape_point(parent, coordinate):
            if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 2:
                return
            try:
                x_lon, y_lat = float(coordinate[0]), float(coordinate[1])
            except (TypeError, ValueError):
                return
            if -180 <= x_lon <= 180 and -90 <= y_lat <= 90:
                ET.SubElement(parent, "point", {
                    "lat": str(round(y_lat, 6)),
                    "lon": str(round(x_lon, 6)),
                    "hae": str(_hae(track)),
                })

        if geometry_type == "Polygon" and isinstance(coordinates, list):
            rings = coordinates[:1]
            if rings and isinstance(rings[0], list):
                shape = ET.SubElement(detail, "shape")
                polygon = ET.SubElement(shape, "polygon")
                for coordinate in rings[0][:256]:
                    shape_point(polygon, coordinate)
        elif geometry_type in {"LineString", "MultiLineString"}:
            lines = coordinates if geometry_type == "MultiLineString" else [coordinates]
            if isinstance(lines, list) and lines:
                shape = ET.SubElement(detail, "shape")
                line = ET.SubElement(shape, "line")
                for coordinate in (lines[0] if isinstance(lines[0], list) else [])[:256]:
                    shape_point(line, coordinate)
    # Custom marker art is opt-in because it is not portable across clients.
    # ATAK reads <usericon b64image>, but WinTAK does not resolve an icon from
    # b64image alone (it wants an iconsetpath into an installed iconset). The
    # marker then draws as a featureless black diamond, and hovering it throws
    # System.NullReferenceException in CotMapMarker.OnHoverChanged because the
    # icon it dereferences was never resolved. Without the element, both clients
    # fall back to the MIL-STD symbol implied by the event type, which always
    # renders. The icons themselves are fine — 25 valid 32x32 RGBA PNGs.
    if _USERICON_ENABLED:
        icon_b64 = _COT_ICON_B64.get(cot_type)
        if icon_b64:
            ET.SubElement(detail, "usericon", {"b64image": icon_b64})
    ET.SubElement(detail, "contact", {"callsign": cs})
    spd_cot = _speed_ms(track)
    crs_cot = _course(track)
    ET.SubElement(detail, "track", {
        "speed":  str(spd_cot),
        "course": str(crs_cot),
    })
    sweep_az = track.get("sweep_azimuth_deg")
    try:
        rng = int(float(track.get("radar_range_m") or 0))
    except (TypeError, ValueError, OverflowError):
        rng = 0
    if not 0 < rng <= 1_000_000:
        rng = 0
    if sweep_az is not None and rng:
        # Radar dish sweep: narrow 5° beam rotating around the site
        ET.SubElement(detail, "sensor", {
            "vfov": "1", "fov": "5", "hfov": "5",
            "range": str(rng),
            "azimuth": str(int(round(float(sweep_az)))),
            "model": "Generic", "ranges": "0",
            "type": "radar",
            "displayMagneticReference": "0",
            "stockTool": "false",
        })
    elif track.get("sensor_type") == "radar" and rng:
        # Static radar site: show full 360° coverage circle
        ET.SubElement(detail, "sensor", {
            "vfov": "90", "fov": "360", "hfov": "360",
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
            "vfov": "90", "fov": "360", "hfov": "360",
            "range": str(threat_range_m),
            "azimuth": "0",
            "model": "Generic", "ranges": "0",
            "type": "radar",
            "displayMagneticReference": "0",
            "stockTool": "false",
        })
    # Anything that merely MOVES used to get a <sensor> too — an airliner, a
    # car or other moving contact claimed to be a 360° radar with range="0". That
    # is wrong on its face (a moving contact is not a sensor) and a zero-range
    # sensor cone is a degenerate shape for a client to draw. <sensor> is now
    # emitted only for things that actually sense.
    ET.SubElement(detail, "remarks").text = _build_remarks(track, cot_type)
    snapshot_url = track.get("snapshot_url")
    if isinstance(snapshot_url, str) and snapshot_url.startswith("https://") \
            and len(snapshot_url) <= 2048:
        ET.SubElement(detail, "link", {
            "url": snapshot_url,
            "type": "image/jpeg",
            "relation": "r-u",
        })

    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(event, encoding="unicode")


# ---------------------------------------------------------------------------
# TCP sender — persistent connection with auto-reconnect
# ---------------------------------------------------------------------------

def _enable_keepalive(sock: socket.socket) -> None:
    """Make the OS notice a silently-dropped peer instead of leaving a half-open
    socket.

    Over a flaky mesh (e.g. a NetBird tunnel that dies mid-stream) TCP does not
    fail a write immediately: sendall() keeps succeeding into an unacknowledged
    send buffer and every CoT is lost with no error to trigger a reconnect. This
    arms two Linux guards so a dead peer surfaces as an error within ~20s:
      * SO_KEEPALIVE probes an *idle* connection (10s idle, 5s interval, 3 fails)
      * TCP_USER_TIMEOUT bounds *in-flight unACKed* data — the case that bit us,
        where the link drops while we are actively sending.
    Best-effort: options absent on non-Linux platforms are skipped.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 20000)
    except OSError:
        pass


class TcpSender:
    """CoT writer with a single-writer reconnect loop. Plaintext or mutual TLS.

    Accepts multiple (host, port) candidates — e.g. a LAN IP, a NetBird mesh
    IP, and a Tailscale mesh IP for one TAK Server — and rotates through them
    when a *connect* fails, converging on whichever path is reachable.

    All socket I/O runs on ONE background thread that owns the socket. Callers
    only enqueue; send() never touches the socket. That is deliberate: it closes
    the native use-after-free that core-dumped the process whenever the TAK link
    flapped — a zenoh callback thread doing an OpenSSL write on a socket another
    thread had just replaced mid-reconnect. An unreachable host now degrades to
    "drop CoT until reconnected" instead of crashing. The queue is bounded and
    drops the oldest on overflow: CoT is state, so a newer update supersedes a
    dropped one, and a stalled link never blocks the callbacks or grows memory.
    """

    def __init__(self, hosts: list[tuple[str, int]], tls: bool = False,
                 certfile: str | None = None, keyfile: str | None = None,
                 cafile: str | None = None, server_name: str | None = None):
        self.hosts     = hosts
        self._tls      = tls
        self._certfile = certfile
        self._keyfile  = keyfile
        self._cafile   = cafile
        self._server_name = server_name
        self.drop_tak_ingress = True
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=TAK_QUEUE_MAX)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tak-writer", daemon=True)
        self._thread.start()

    def send(self, xml: str) -> None:
        # Hand off to the writer thread; never blocks the caller. On overflow,
        # drop the oldest queued CoT so the freshest still gets through.
        while not self._stop.is_set():
            try:
                self._q.put_nowait(xml)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def _open(self, host: str, port: int) -> socket.socket:
        raw = socket.create_connection((host, port), timeout=SEND_TIMEOUT_S)
        _enable_keepalive(raw)
        if not self._tls:
            raw.settimeout(SEND_TIMEOUT_S)
            return raw
        # Dial and identity names are deliberately separate: redundant IP/DNS
        # paths may all reach one TAK server certificate. The configured TLS
        # server name must still match that certificate's SAN.
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self._cafile)
        ctx.check_hostname = True
        if self._certfile and self._keyfile:
            ctx.load_cert_chain(self._certfile, self._keyfile)
        s = ctx.wrap_socket(raw, server_hostname=self._server_name or host)
        s.settimeout(SEND_TIMEOUT_S)
        return s

    def _run(self) -> None:
        sock: socket.socket | None = None
        idx = 0
        while not self._stop.is_set():
            if sock is None:
                host, port = self.hosts[idx % len(self.hosts)]
                try:
                    sock = self._open(host, port)
                    print("TAK {} connected → {}:{}".format(
                        "TLS" if self._tls else "TCP", host, port), flush=True)
                except OSError as exc:
                    # Connect failed → this path is down; rotate to the next
                    # candidate and back off. One thread, so no reconnect storm.
                    print("TAK connect failed ({}:{}) — {}, next candidate in {}s".format(
                        host, port, exc, RECONNECT_S), flush=True)
                    idx += 1
                    self._stop.wait(RECONNECT_S)
                    continue
            try:
                xml = self._q.get(timeout=1.0)
            except queue.Empty:
                continue  # idle: loop back to re-check the stop flag
            try:
                sock.sendall((xml + "\n").encode("utf-8"))
            except OSError:
                # A failed write means the server closed an established stream,
                # not that the path is down — reconnect to the SAME candidate.
                # The candidates are alternate addresses of one TAK Server, which
                # identifies clients by certificate, so returning on a different
                # address would drop the prior session and churn. If the path is
                # genuinely down, the next connect fails and rotation happens there.
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                self._stop.wait(RECONNECT_S)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()


# Views that carry the same object as the flat JSON and must not be processed
# twice. Anything else — including a bare topic with no view suffix — is treated
# as the JSON payload.
_NON_JSON_VIEWS = frozenset({"sapient", "proto", "raw"})


def _terminal_view(key: str) -> str:
    """The view segment, ignoring a trailing /tracks/vN version tail.

    Every object key ends with the fabric version (…/tracks/v1), so the format
    sits one segment back: …/{id}/sapient/tracks/v1. Reading the literal last
    segment would only ever see "v1" and never skip a non-JSON view."""
    parts = key.split("/")
    if (len(parts) >= 2 and parts[-2] == "tracks"
            and parts[-1][:1] == "v" and parts[-1][1:].isdigit()):
        parts = parts[:-2]
    return parts[-1] if parts else ""


# ---------------------------------------------------------------------------
# Zenoh → CoT callbacks
# ---------------------------------------------------------------------------

def make_handler(cot_type_or_fn, sender, verbose: bool, stale_s: float = COT_STALE_S):
    def handler(sample):
        # An object published via publish_dual appears in several views
        # (sapient/json/proto) and the topic wildcards match all of them; CoT is
        # built from the flat JSON, so the non-JSON views are skipped rather
        # than left to fail json.loads.
        #
        # Skip by DENYING the redundant views, not by requiring "/json": several
        # bridges (weather, ADS-B, dronuradaras) publish a single flat JSON
        # sample on a bare topic with no view suffix at all. Requiring "/json"
        # silently discarded every one of them, so TAK stayed empty while the
        # SitaWare feed — which has no such filter — showed them all.
        if _terminal_view(str(sample.key_expr)) in _NON_JSON_VIEWS:
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        # Raw sensor sources and ADS-B relay sources must pass through
        # fusion (compose/protocols/fusion.py) before reaching ATAK.  Exception:
        # anything arriving on an air/fused/** topic was already processed by
        # fusion and must be allowed through — this includes ADS-B fallback
        # tracks that fusion publishes when no radar is covering that aircraft.
        src = track.get("_src", "")
        key = str(sample.key_expr)
        # TAK Server connections are bidirectional. Never send a TAK-ingress
        # track straight back into the server TCP connection; SitaWare (via the
        # NVG layer) still receives it normally.
        if track.get("_ingress") == "tak_server" and getattr(
            sender, "drop_tak_ingress", False
        ):
            return
        if src == "dronuradaras.lt" and track.get("_delete"):
            uid = _uid(track)
            xml = track_to_cot(track, "a-n-G-E-S", stale_s=1.0)
            if xml is not None:
                sender.send(xml)
            with _dr_lock:
                _dr_store.pop(uid, None)
            if verbose:
                print("CoT expired offline sensor {}".format(uid), flush=True)
            return
        # Generic tombstone: any OTHER vendor's plain {"uid", "_delete": true}
        # (e.g. aartos_json.py's per-track tombstones) — this was previously
        # silently dropped since only the dronuradaras.lt case above was
        # handled, leaving expired markers to linger for a full stale_s
        # instead of disappearing immediately.
        if track.get("_delete"):
            uid = _uid(track)
            sender.send(_delete_point_cot(uid, track.get("_ts")))
            if verbose:
                print("CoT deleted {}".format(uid), flush=True)
            return
        if _is_unfused_sensor_track(track, key):
            return
        # ATC towers / ground vehicles show up in ADS-B with "TWR", "GND" etc.
        # Reclassify as neutral ground radar/radio station instead of aircraft.
        if _is_adsb_surface_vehicle(track):
            cot_type = "a-n-G-E-V"
            stale_s_used = LAND_STALE_S
        elif _is_ground_station(track):
            cot_type     = _RADAR_COT_TYPE
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
        # CAT-34 status also ships in several views; consume the flat JSON only
        # (bare-topic publishers have no view suffix — see make_handler).
        if _terminal_view(str(sample.key_expr)) in _NON_JSON_VIEWS:
            return
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
            if not track.get("radar_range_m"):
                return
            # Sweep tick: send a SEPARATE beam entity (different UID) so the site
            # marker keeps its permanent 360° coverage ring at the same time.
            rot = float(track.get("rotation_s") or 4.0)
            beam_track = dict(track)
            beam_track["sensor_id"] = "BEAM-" + str(track.get("sensor_id", "RADAR"))
            beam_track["sensor_name"] = (track.get("sensor_name") or "RADAR") + " BEAM"
            xml = track_to_cot(beam_track, _radar_cot_type(track), stale_s=max(rot * 0.6, 1.0))
        else:
            # Full status: site marker with permanent 360° coverage ring
            xml = track_to_cot(track, _radar_cot_type(track), stale_s=LAND_STALE_S * 2)
        if xml:
            sender.send(xml)
        if verbose:
            print("CoT {} {}  psr={} ssr={} mds={}".format(
                _radar_cot_type(track), track.get("sensor_name", "RADAR"),
                track.get("psr_status", "-"), track.get("ssr_status", "-"),
                track.get("mds_status", "-")), flush=True)
    return handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    hosts = [(h, args.port) for h in args.host]
    tls = getattr(args, "tls", False)
    if tls and not getattr(args, "ca", None):
        raise SystemExit("--ca / TAK_CA is required when --tls is specified")
    sender = TcpSender(hosts,
                       tls=tls,
                       certfile=getattr(args, "cert", None),
                       keyfile=getattr(args, "key", None),
                       cafile=getattr(args, "ca", None),
                       server_name=getattr(args, "tls_server_name", None))
    mode = "TLS mTLS" if tls else "TCP plaintext"
    print("CoT → {} candidates: {} (TAK Server, first reachable wins)".format(
        mode, ", ".join("{}:{}".format(h, p) for h, p in hosts)), flush=True)

    session = open_session()
    _start_dr_thread(sender)
    subs = []
    for suffix, (cot_type, stale_s) in _TOPIC_COT.items():
        key = "{}/{}".format(TOPIC_ROOT, suffix)
        fn = cot_type.__name__ if callable(cot_type) else str(cot_type)
        subs.append(subscribe(session,
            key, make_handler(cot_type, sender, args.verbose, stale_s=stale_s)))
        print("SUB {} → {} stale={}s".format(key, fn, stale_s), flush=True)

    # Radar sensor site status (CAT-34) — updates _radar_status dict + renders CoT marker
    # The source segment is a wildcard: a radar names itself by SAC/SIC, so its
    # topic is only known once a record has been decoded.
    radar_key = "{}/land/*/radar/neutral/radar/**".format(TOPIC_ROOT)
    subs.append(subscribe(session, radar_key,
                make_radar_status_handler(sender, args.verbose)))
    print("SUB {} → [radar sensor sites]".format(radar_key), flush=True)

    # The subscribers run on Zenoh's threads; this thread exists only to hold
    # the process open. It used to do that by waking once a second to sleep
    # again — 86400 no-op wakeups a day, and worse, nothing was watching for
    # the signal that actually ends this process. Python's default SIGTERM
    # disposition kills the interpreter with no traceback and no log line,
    # which is why every one of these exits looked like an unexplained
    # disappearance. Block until told to stop, and say who told us.
    stop = threading.Event()

    def _on_signal(signum, _frame):
        print("shutting down on {}".format(signal.Signals(signum).name), flush=True)
        stop.set()

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _on_signal)
        except (OSError, ValueError):
            pass  # not the main thread, or the platform lacks this signal

    print("Bridge running — Ctrl-C to stop", flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        print("shutting down on KeyboardInterrupt", flush=True)
    finally:
        for sub in subs:
            sub.undeclare()
        session.close()
        sender.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Zenoh tracks → TAK Server / ATAK CoT bridge")
    ap.add_argument("--host", action="append", default=None,
                    help="TAK Server host — repeatable (e.g. --host <lan-ip> --host <netbird-ip>) "
                         "to try multiple network paths; falls back to TAK_HOST/TAK_HOST_FALLBACK/"
                         "TAK_HOST_TAILSCALE env or 127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TAK_PORT", "8089")),
                    help="TAK Server port (default: 8089, the mTLS streaming port — the "
                         "anonymous 8087 input is not distributed to 8089 subscribers)")
    ap.add_argument("--tls", action="store_true",
                    default=os.environ.get("TAK_TLS", "") == "1",
                    help="Enable mutual TLS — Option B, port 8089 (requires --cert, --key, --ca)")
    ap.add_argument("--cert", default=os.environ.get("TAK_CERT"),
                    help="Client certificate PEM (mTLS Option B)")
    ap.add_argument("--key", default=os.environ.get("TAK_KEY"),
                    help="Client private key PEM (mTLS Option B)")
    ap.add_argument("--ca", default=os.environ.get("TAK_CA"),
                    help="CA certificate PEM (mTLS Option B)")
    ap.add_argument("--tls-server-name", default=os.environ.get("TAK_TLS_SERVER_NAME"),
                    help="DNS SAN expected in the TAK server certificate; defaults to the dial host")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each CoT message sent")
    args = ap.parse_args(argv)
    if not args.host:
        args.host = [
            host
            for host in (
                os.environ.get("TAK_HOST", "").strip(),
                os.environ.get("TAK_HOST_FALLBACK", "").strip(),
                os.environ.get("TAK_HOST_TAILSCALE", "").strip(),
            )
            if host
        ] or ["127.0.0.1"]
    run(args)


if __name__ == "__main__":
    main()
