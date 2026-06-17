    #!/usr/bin/env python3
"""cot_layer.py — Zenoh EFDI track topics → TAK Server / ATAK CoT bridge.

Subscribes to all EFDI track topics and forwards position updates as
Cursor-on-Target (CoT) XML to a TAK Server (FreeTAKServer) over TCP.
All connected ATAK / iTAK / TAKX / WinTAK devices see the tracks automatically
through the server — no per-device configuration, no multicast required.

Default: TCP → localhost:8087  (FreeTAKServer running in the same compose stack)
Override: --host <ip> --port <port>

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
    venv/bin/python3 cot_layer.py                        # TCP → FTS localhost
    venv/bin/python3 cot_layer.py --host 100.64.59.10   # TCP → remote FTS
    venv/bin/python3 cot_layer.py --udp --host 239.2.3.1 --port 6969  # UDP multicast
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
ENV_STALE_S    = 3600    # weather stations: polled every 15–30 min, 1 h gives plenty of margin
SENSOR_STALE_S = 1800    # air quality sensors: polled every 10 min, 30 min stale
RECONNECT_S    = 5
SEND_TIMEOUT_S = 10

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
    return "a-h-A-C-F" if _is_hostile_icao24(track.get("icao24")) else "a-f-A-C-F"

def _mil_air_type(track: dict) -> str:
    return "a-h-A-M-F" if _is_hostile_icao24(track.get("icao24")) else "a-n-A-M-F"

def _sea_type(track: dict) -> str:
    return "a-h-S-X-L" if _is_hostile_mmsi(track.get("mmsi", "")) else "a-f-S-X-L"


# Schema: {category}/{vendor}/{protocol}/{affiliation}/{entity_type}/{data_type}/v1
# Wildcards: ** matches zero-or-more segments, so air/**/civ/aircraft/** catches any
# vendor+protocol combination under civil air.
_TOPIC_COT = {
    # AIR — affiliation slot drives CoT type; ICAO24 classifier overrides for RU/BY
    "air/**/civ/aircraft/**":    (_civ_air_type,  AIR_STALE_S),
    "air/**/mil/aircraft/**":    (_mil_air_type,  AIR_STALE_S),
    "air/**/unknown/**":         ("a-u-A",        AIR_STALE_S),   # radar / SAPIENT returns
    # LAND — neutral stations, friendly forces, civilian vehicles
    "land/**/civ/vehicle/**":    ("a-f-G-E-V-C", LAND_STALE_S),
    "land/**/neutral/station/**":("a-n-G-I",     LAND_STALE_S),
    "land/**/friendly/unit/**":  ("a-f-G-U-C",  LAND_STALE_S),
    # SEA — MMSI classifier overrides for RU/BY vessels
    "sea/**/civ/vessel/**":      (_sea_type,      SEA_STALE_S),
    "sea/**/mil/vessel/**":      ("a-n-S-W-C",   SEA_STALE_S),   # reserved: neutral mil vessel
    # SPACE
    "space/**/civ/satellite/**": ("a-f-P",        GEO_STALE_S),
    # ENV — weather stations and air quality sensors show as ground icons
    "env/weather/station/**":    ("a-n-G-I-R",   ENV_STALE_S),
    "env/air_quality/station/**":("a-n-G-I-R",   SENSOR_STALE_S),
}

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


_BLUE   = (0, 116, 217)   # 2525B friendly blue
_GREEN  = (0, 164, 0)     # 2525B neutral green
_YELLOW = (255, 215, 0)   # 2525B unknown yellow
_RED    = (220, 20,  20)  # 2525B hostile red

# b64image fallback — used when ATAK doesn't have the iconset installed.
# ATAK CIV 5.x ignores b64image for recognised 2525B types, so we also set
# iconsetpath (below) which is honoured regardless of the CoT type.
_COT_ICON_B64 = {
    # Civil aircraft — T-shaped commercial silhouette
    "a-f-A-C-F":   _icon_png_b64("aircraft", _BLUE),
    "a-h-A-C-F":   _icon_png_b64("aircraft", _RED),
    "a-u-A-C-F":   _icon_png_b64("aircraft", _YELLOW),
    # Military aircraft — swept-wing fighter silhouette
    "a-n-A-M-F":   _icon_png_b64("fighter",  _GREEN),
    "a-h-A-M-F":   _icon_png_b64("fighter",  _RED),
    "a-u-A":       _icon_png_b64("fighter",  _YELLOW),
    # Surface vessels
    "a-f-S-X-L":   _icon_png_b64("ship",     _BLUE),
    "a-h-S-X-L":   _icon_png_b64("ship",     _RED),
    # Space / satellite
    "a-f-P":       _icon_png_b64("circle",   _BLUE),
    # Ground
    "a-f-G-E-V-C": _icon_png_b64("vehicle",  _BLUE),
    "a-u-G":       _icon_png_b64("circle",   _YELLOW),
    "a-n-G-I":     _icon_png_b64("circle",   _GREEN),
    "a-n-G-I-R":   _icon_png_b64("tower",    _GREEN),   # Neutral ground installation radio
    "a-f-G-E-S-R": _icon_png_b64("radar",    _BLUE),    # Friendly ground electronic sensor radar
    "a-f-G-I-B-A": _icon_png_b64("circle",   _BLUE),
    "a-f-G-I-B-O": _icon_png_b64("circle",   _BLUE),
    "a-f-G-I-B-M": _icon_png_b64("circle",   _BLUE),
}

# Primary icon path — references the MIL-STD-2525B iconset pre-installed in
# ATAK CIV 5.x.  ATAK renders iconsetpath even when CoT type is standard 2525B,
# giving the correct inner function symbol (plane, ship silhouette) inside the
# 2525B affiliation frame (arc=air, U=sea, box=ground).
_ISET = "34ae1613-9645-4222-a9d2-e5f243dea2865"
_COT_ICONSET = {
    "a-f-A-C-F":   "{}/Friendly/Air/Fixed Wing.png".format(_ISET),
    "a-h-A-C-F":   "{}/Hostile/Air/Fixed Wing.png".format(_ISET),
    "a-n-A-M-F":   "{}/Neutral/Air/Military Fixed Wing.png".format(_ISET),
    "a-h-A-M-F":   "{}/Hostile/Air/Military Fixed Wing.png".format(_ISET),
    "a-u-A":       "{}/Unknown/Air/Military Fixed Wing.png".format(_ISET),
    "a-u-A-C-F":   "{}/Unknown/Air/Fixed Wing.png".format(_ISET),
    "a-f-G-E-V-C": "{}/Friendly/Land/Vehicle.png".format(_ISET),
    "a-f-S-X-L":   "{}/Friendly/Sea/Vessel.png".format(_ISET),
    "a-h-S-X-L":   "{}/Hostile/Sea/Vessel.png".format(_ISET),
    "a-u-G":       "{}/Unknown/Land/Generic.png".format(_ISET),
    "a-n-G-I":     "{}/Neutral/Land/Structure.png".format(_ISET),
    "a-f-G-I-B-A": "{}/Friendly/Land/Airfield.png".format(_ISET),
    "a-f-G-I-B-O": "{}/Friendly/Land/Port.png".format(_ISET),
    "a-f-G-I-B-M": "{}/Friendly/Land/Military Base.png".format(_ISET),
    "a-f-P":       "{}/Friendly/Space/Satellite.png".format(_ISET),
    "a-f-G-U-C":   "{}/Friendly/Land/Unit.png".format(_ISET),
    "a-n-G-I-R":   "{}/Neutral/Land/Radio.png".format(_ISET),
    "a-f-G-E-S-R": "{}/Friendly/Land/Radar.png".format(_ISET),
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
    # OSM name / ship name → short map label
    for key in ("name", "ship_name", "sensor_name"):
        v = track.get(key)
        if v and str(v).strip():
            return str(v).strip()
    # Aircraft: prefer "REG (TYPE)" → "CALLSIGN (TYPE)" → "CALLSIGN" → "REG"
    reg   = (track.get("registration") or "").strip()
    atype = (track.get("aircraft_type") or "").strip()
    cs    = (track.get("callsign")      or "").strip()
    label = reg or cs
    if label and atype:
        return "{} ({})".format(label, atype)
    if label:
        return label
    # Weather point: place name
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
            lines.append("{:<10}{}".format(label + ":", value))

    if domain == "A":   # ---- AIR ----------------------------------------
        _row("REG",  (track.get("registration") or "").strip().upper() or None)
        _row("TYPE", (track.get("aircraft_type") or "").strip().upper() or None)
        _row("CALL", (track.get("callsign")      or "").strip().upper() or None)
        _row("ICAO", (track.get("icao24")        or "").strip().upper() or None)
        alt_m = _hae(track)
        if alt_m < 9_999_998:
            alt_ft  = int(alt_m / 0.3048)
            alt_str = "FL{:03d}".format(alt_ft // 100) if alt_ft > 1000 else "{} ft".format(alt_ft)
        else:
            alt_str = "---"
        spd = _speed_ms(track)
        _row("ALT",  alt_str)
        _row("SPD",  "{} kts".format(round(spd / 0.514444)) if spd else None)
        _row("HDG",  "{}°".format(int(_course(track))))
        _row("SQWK", track.get("squawk"))
        if track.get("is_military"):
            lines.append("[MILITARY]")

    elif domain == "S":  # ---- SEA ----------------------------------------
        _row("NAME",   track.get("ship_name", "").strip() or None)
        _row("MMSI",   track.get("mmsi"))
        _row("CALL",   (track.get("callsign") or "").strip().upper() or None)
        _row("TYPE",   track.get("ship_type"))
        _row("SOG",    "{} kts".format(round(_speed_ms(track) / 0.514444, 1)))
        _row("COG",    "{}°".format(int(_course(track))))
        nav = track.get("nav_status", "").replace("_", " ")
        _row("STATUS", nav or None)

    elif domain == "P":  # ---- SPACE / SATELLITE ---------------------------
        _row("SAT ID", track.get("sensor_id") or track.get("norad_id"))
        _row("NAME",   track.get("sat_name")  or track.get("name"))
        alt_km = _hae(track) / 1000.0
        _row("ALT",   "{} km".format(round(alt_km)) if alt_km < 9999 else None)
        spd = _speed_ms(track)
        _row("SPD",   "{} km/s".format(round(spd / 1000, 2)) if spd else None)

    else:                # ---- GROUND / ENV / APRS / OSM ------------------
        if src in ("openmeteo", "meteolt", "yrno", "windy"):   # WEATHER
            place = (track.get("place_name") or track.get("place_code") or src).upper()
            lines.append("[WEATHER] {}".format(place))
            t = track.get("temperature_c")
            _row("TEMP",     "{} °C".format(round(float(t), 1)) if t is not None else None)
            ft = track.get("apparent_temperature_c") or track.get("feels_like_c")
            _row("FEELS",    "{} °C".format(round(float(ft), 1)) if ft is not None else None)
            rh = track.get("relative_humidity_pct")
            _row("HUMIDITY", "{}%".format(int(rh)) if rh is not None else None)
            ws = track.get("wind_speed_ms")
            wd = track.get("wind_direction_deg")
            if ws is not None:
                _row("WIND",  "{} m/s{}".format(round(float(ws), 1),
                              "  {}°".format(int(wd)) if wd is not None else ""))
            _row("GUSTS",    "{} m/s".format(round(float(track["wind_gusts_ms"]), 1))
                             if track.get("wind_gusts_ms") is not None else None)
            p = track.get("pressure_hpa")
            _row("PRESSURE", "{} hPa".format(round(float(p), 1)) if p is not None else None)
            cc = track.get("cloud_cover_pct")
            _row("CLOUD",    "{}%".format(int(cc)) if cc is not None else None)
            pr = track.get("precipitation_mm")
            if pr is not None and float(pr) > 0:
                _row("PRECIP", "{} mm".format(round(float(pr), 1)))

        elif src == "purpleair":   # AIR QUALITY
            name = track.get("sensor_name") or "Sensor #{}".format(track.get("sensor_id", "?"))
            lines.append("[AIR QUALITY] {}".format(name))
            aqi    = track.get("aqi")
            aqicat = track.get("aqi_category", "")
            _row("AQI",      "{} ({})".format(int(aqi), aqicat) if aqi is not None else None)
            pm25 = track.get("pm25_ugm3")
            _row("PM2.5",    "{} µg/m³".format(round(float(pm25), 1)) if pm25 is not None else None)
            pm10 = track.get("pm10_ugm3")
            _row("PM10",     "{} µg/m³".format(round(float(pm10), 1)) if pm10 is not None else None)
            pm1  = track.get("pm1_ugm3")
            _row("PM1",      "{} µg/m³".format(round(float(pm1),  1)) if pm1  is not None else None)
            t = track.get("temperature_c")
            _row("TEMP",     "{} °C".format(round(float(t), 1)) if t is not None else None)
            rh = track.get("relative_humidity_pct")
            _row("HUMIDITY", "{}%".format(int(rh)) if rh is not None else None)
            p = track.get("pressure_hpa")
            _row("PRESSURE", "{} hPa".format(round(float(p), 1)) if p is not None else None)

        else:                      # APRS / OSM / vehicles
            feat = track.get("feature_type")
            if feat:               # OSM geo feature
                _row("TYPE", feat.upper())
                _row("ICAO", track.get("icao"))
                _row("IATA", track.get("iata"))
            else:                  # APRS or vehicle
                _row("SYM",  track.get("symbol"))
                spd = _speed_ms(track)
                _row("SPD",  "{} kts".format(round(spd / 0.514444, 1)) if spd else None)
                _row("HDG",  "{}°".format(int(_course(track))) if spd else None)

    _row("SRC", src)
    return "\n".join(lines)


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
    icon_path = _COT_ICONSET.get(cot_type)
    icon_b64  = _COT_ICON_B64.get(cot_type)
    if icon_path:
        ET.SubElement(detail, "usericon", {"iconsetpath": icon_path})
    elif icon_b64:
        ET.SubElement(detail, "usericon", {"b64image": icon_b64})
    ET.SubElement(detail, "contact", {"callsign": cs})
    ET.SubElement(detail, "track", {
        "speed":  str(_speed_ms(track)),
        "course": str(_course(track)),
    })
    ET.SubElement(detail, "remarks").text = _build_remarks(track, cot_type)

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
        fn = cot_type.__name__ if callable(cot_type) else str(cot_type)
        subs.append(session.declare_subscriber(
            key, make_handler(cot_type, sender, args.verbose, stale_s=stale_s)))
        print("SUB {} → {} stale={}s".format(key, fn, stale_s), flush=True)

    # Geo features (aerodromes, ports, military bases) — 24h stale
    # Matches: land/{vendor}/{protocol}/neutral/geo/features/v1
    geo_key = "{}/land/**/neutral/geo/**".format(ORG)
    subs.append(session.declare_subscriber(geo_key, make_geo_handler(sender, args.verbose)))
    print("SUB {} → [geo features, 24h stale]".format(geo_key), flush=True)

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
