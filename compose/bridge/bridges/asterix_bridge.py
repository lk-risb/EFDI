#!/usr/bin/env python3
"""asterix_bridge.py — Unified ASTERIX multi-category → Zenoh bridge.

Handles five ASTERIX categories from surveillance sensors in a single process:

  CAT-020  MLAT target reports    — WGS-84 from multilateration network
  CAT-021  ADS-B target reports   — WGS-84 from Mode-S ground station
  CAT-034  Monoradar service msgs — north marker, antenna status, calibration
  CAT-048  Monoradar target rpts  — slant-polar plots from primary radar
  CAT-062  System track updates   — outbound TCP connect to track server

CAT-020, CAT-021, and CAT-048/034 are inbound listeners (sensor connects to us).
CAT-062 uses an outbound TCP connector (we connect to the radar) with auto-reconnect.
Each configured category runs in its own daemon thread.

Zenoh topics:
  CAT-020: <ORG>/air/asterix/cat20/civ/aircraft/tracks/v1
  CAT-021: <ORG>/air/asterix/cat21/civ/aircraft/tracks/v1
  CAT-034: <ORG>/land/asterix/cat34/neutral/radar/status/v1
  CAT-048: <ORG>/air/asterix/cat48/unknown/aircraft/tracks/v1
  CAT-062: <ORG>/air/asterix/cat62/unknown/aircraft/tracks/v1  (configurable)

Args:
  --cat48-port PORT    Inbound port for CAT-034/048 stream (or $CAT48_PORT)
  --cat48-tcp          TCP server mode for CAT-034/048 (default: UDP)
  --radar-lat LAT      Radar latitude for polar→WGS-84 (or $CAT48_RADAR_LAT)
  --radar-lon LON      Radar longitude (or $CAT48_RADAR_LON)
  --radar-name NAME    Radar site label in ATAK (or $CAT48_RADAR_NAME)
  --cat21-port PORT    Inbound port for CAT-021 (or $CAT21_PORT)
  --cat21-tcp          TCP server mode for CAT-021
  --cat20-port PORT    Inbound port for CAT-020 (or $CAT20_PORT)
  --cat20-tcp          TCP server mode for CAT-020
  --cat62-host HOST    Radar host for CAT-062 outbound TCP (or $CAT62_HOST)
  --cat62-port PORT    Radar port for CAT-062 (default: 30002)
  --cat62-udp          Inbound UDP mode for CAT-062 instead of outbound TCP
  --cat62-topic TOPIC  Override default Zenoh topic for CAT-062
  --verbose / -v
"""

import argparse
import json
import math
import os
import socket
import struct
import threading
import time

import zenoh

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = "1851281db70ccc0409dad4ecfc874cf5"
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

TOPIC_020    = "{}/air/asterix/cat20/civ/aircraft/tracks/v1".format(ORG)
TOPIC_021    = "{}/air/asterix/cat21/civ/aircraft/tracks/v1".format(ORG)
TOPIC_048    = "{}/air/asterix/cat48/unknown/aircraft/tracks/v1".format(ORG)
TOPIC_SENSOR = "{}/land/asterix/cat34/neutral/radar/status/v1".format(ORG)
TOPIC_062    = "{}/air/asterix/cat62/unknown/aircraft/tracks/v1".format(ORG)

CAT_020 = 0x14
CAT_021 = 0x15
CAT_034 = 0x22
CAT_048 = 0x30
CAT_062 = 0x3E

RECONNECT_DELAY_S = 5.0


# ---------------------------------------------------------------------------
# Zenoh
# ---------------------------------------------------------------------------

def _env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _netbird_ip() -> str:
    for iface in ("wt0", "netbird0"):
        try:
            import subprocess
            out = subprocess.check_output(
                ["ip", "-4", "addr", "show", iface],
                stderr=subprocess.DEVNULL, text=True)
            for tok in out.split():
                if "/" in tok and tok[0].isdigit():
                    return tok.split("/")[0]
        except Exception:
            pass
    return socket.gethostname()


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
# ASTERIX framing
# ---------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            continue
        data = _recv_exact(sock, length - 3)
        yield cat, data


def iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            yield cat, pkt[offset + 3:offset + length]
            offset += length


# ---------------------------------------------------------------------------
# FSPEC parser
# ---------------------------------------------------------------------------

def parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _skip_fx_field(data: bytes, pos: int) -> int:
    while pos < len(data):
        b = data[pos]; pos += 1
        if not (b & 0x01):
            break
    return pos


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------

def _s16(b: bytes) -> int: return struct.unpack(">h", b)[0]
def _u16(b: bytes) -> int: return struct.unpack(">H", b)[0]
def _s32(b: bytes) -> int: return struct.unpack(">i", b)[0]


def _s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw


_CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"


def _decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()


# ---------------------------------------------------------------------------
# CAT-048/034 specific helpers
# ---------------------------------------------------------------------------

def _decode_bds50(mb: bytes) -> dict:
    """BDS 5,0 Track and Turn Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["roll_deg"]         = round(_sgn(2, 11) * 45.0 / 512.0, 1)
    if _bit(12): out["true_track_deg"]   = round(_sgn(13, 22) * 90.0 / 512.0 % 360, 1)
    if _bit(23): out["bds_gs_kt"]        = round(_uns(24, 33) * 2.0, 0)
    if _bit(34): out["track_rate_degs"]  = round(_sgn(35, 44) * 8.0 / 256.0, 2)
    if _bit(45): out["tas_kt"]           = round(_uns(46, 55) * 2.0, 0)
    return out


def _decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_sgn(2, 11) * 90.0 / 512.0 % 360, 1)
    if _bit(12): out["ias_kt"]       = _uns(13, 22)
    if _bit(23): out["mach"]         = round(_uns(24, 34) * 2.048 / 2048.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 46) * 32
    if _bit(47): out["ivv_fpm"]      = _sgn(48, 56) * 32
    return out


def _gillham_to_ft(code: int) -> int | None:
    """Decode ASTERIX Mode-C Gillham code (u16 from I048/100 or I020/100 bytes 0-1).

    Byte layout: V G C1 A1 C2 B1 D1 B2 | D2 B4 A4 x x C4 x x
    Returns altitude in feet, or None if invalid/garbled.
    """
    v  = (code >> 15) & 1
    g  = (code >> 14) & 1
    if v or g:
        return None
    C1 = (code >> 13) & 1; A1 = (code >> 12) & 1; C2 = (code >> 11) & 1
    B1 = (code >> 10) & 1; D1 = (code >>  9) & 1; B2 = (code >>  8) & 1
    D2 = (code >>  7) & 1; B4 = (code >>  6) & 1; A4 = (code >>  5) & 1
    C4 = (code >>  2) & 1
    if D1 and D2:
        return None
    def _gc3(a, b, c):
        x = a; y = x ^ b; z = y ^ c; return x * 4 + y * 2 + z
    n_b = _gc3(B1, B2, B4)          # 500ft group (0-7)
    n_a = A1 * 2 + A4               # A bits are binary-coded (0-3)
    n_c = _gc3(C1, C2, C4)          # 100ft offset (1-5 valid)
    if n_c == 0 or n_c > 5:
        return None
    n500 = n_b * 4 + (n_a if n_b % 2 else 3 - n_a)
    return n500 * 500 + (n_c - 1) * 100 - 1200


_EMERGENCY_CODES = {
    0: None,
    1: "GENERAL EMERGENCY",
    2: "LIFEGUARD/MEDICAL",
    3: "MIN FUEL",
    4: "NO COMMS",
    5: "UNLAWFUL INTERFERENCE",
    6: "DOWNED AIRCRAFT",
}

_EMITTER_CATEGORY = {
    0: "no info",    1: "light",      2: "small",       3: "medium",
    4: "high vortex large", 5: "heavy", 6: "manoeuvrable/high speed",
    10: "glider",    11: "airship",   12: "UAV",        13: "space vehicle",
    14: "emergency vehicle", 15: "service vehicle",    16: "ground obstruction",
}


def _decode_bds30(mb: bytes) -> dict:
    """BDS 3,0 ACAS Resolution Advisory (shared by I048/260, I062/380 sub-10, I020/250)."""
    if len(mb) < 7:
        return {}
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    ara = _uns(5, 14)
    if not ara:
        return {}
    out: dict = {"acas_ra_active": True, "acas_ra_hex": format(v, "014x")}
    corrective = bool(_bit(5))
    downward   = bool(_bit(6))
    if corrective:
        out["acas_ra_sense"]     = "DESCEND" if downward else "CLIMB"
        out["acas_ra_corrective"] = True
    out["acas_ra_terminated"]  = bool(_bit(15))
    out["acas_multi_threat"]   = bool(_bit(16))
    return out


def _polar_to_wgs84(radar_lat: float, radar_lon: float,
                    range_nm: float, azimuth_deg: float):
    """Haversine forward: slant-polar radar plot → WGS-84 lat/lon."""
    d    = range_nm * 1852.0
    R    = 6_371_000.0
    lat1 = math.radians(radar_lat)
    lon1 = math.radians(radar_lon)
    az   = math.radians(azimuth_deg)
    lat2 = math.asin(math.sin(lat1) * math.cos(d / R) +
                     math.cos(lat1) * math.sin(d / R) * math.cos(az))
    lon2 = lon1 + math.atan2(math.sin(az) * math.sin(d / R) * math.cos(lat1),
                              math.cos(d / R) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


# ---------------------------------------------------------------------------
# CAT-034 compound field decoders
# ---------------------------------------------------------------------------

_MSG_TYPES_034 = {1: "north_marker", 2: "sector_crossing",
                  3: "geo_filter",   4: "jamming_strobe"}

_COUNT_LABELS = {
    0: "no_detection", 1: "psr",        2: "ssr",         3: "psr_ssr",
    4: "all",          5: "no_det_psr", 6: "no_det_ssr",  7: "mode5",
    11: "mil_id",
}


def _decode_i034_050(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/050 System Configuration and Status — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    sf = data[pos]; pos += 1
    if sf & 0x80:
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        out["sys_nogo"]        = bool(com & 0x80)
        out["sys_ovl_rdp"]     = bool(com & 0x10)
        out["sys_ovl_xmt"]     = bool(com & 0x08)
        out["sys_tsv_invalid"] = bool(com & 0x02)
    for key, mask in (("psr_status", 0x20), ("ssr_status", 0x10), ("mds_status", 0x08)):
        if sf & mask:
            if pos >= len(data): return out, pos
            b = data[pos]; pos += 1
            an = (b & 0x60) >> 5
            out[key] = ("not_operational", "operational", "degraded", "test")[an]
            if b & 0x10: out[key.replace("status", "overload")] = True
    while sf & 0x01:
        if pos >= len(data): break
        sf = data[pos]; pos += 1
        for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
            if (sf & mask) and pos < len(data): pos += 1
    return out, pos


def _decode_i034_060(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/060 System Processing Mode — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    sf = data[pos]; pos += 1
    if sf & 0x80:
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        red = (com & 0xE0) >> 5
        if red: out["reduction_level"] = red
    for mask in (0x20, 0x10, 0x08):
        if sf & mask:
            if pos < len(data): pos += 1
    while sf & 0x01:
        if pos >= len(data): break
        sf = data[pos]; pos += 1
        for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
            if (sf & mask) and pos < len(data): pos += 1
    return out, pos


# ---------------------------------------------------------------------------
# CAT-034 decoder  (Monoradar Service Messages, Edition 1.29)
# ---------------------------------------------------------------------------

def decode_cat034(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I034/010  SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I034/000  Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _MSG_TYPES_034.get(data[pos], data[pos]); pos += 1
        elif frn == 2:                  # I034/030  Time of Day (1/128 s)
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I034/020  Sector Number (360/256 °)
            if pos + 1 > len(data): break
            msg["sector_deg"] = round(data[pos] * 360.0 / 256.0, 2); pos += 1
        elif frn == 4:                  # I034/041  Antenna Rotation Period (1/128 s)
            if pos + 2 > len(data): break
            msg["rotation_s"] = round(_u16(data[pos:pos+2]) / 128.0, 2); pos += 2
        elif frn == 5:                  # I034/050  System Configuration (compound)
            extra, pos = _decode_i034_050(data, pos)
            msg.update(extra)
        elif frn == 6:                  # I034/060  System Processing Mode (compound)
            extra, pos = _decode_i034_060(data, pos)
            msg.update(extra)
        elif frn == 7:                  # I034/070  Message Count Values (REP × 2 bytes)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            counts = {}
            for _ in range(rep):
                if pos + 2 > len(data): break
                word = _u16(data[pos:pos+2]); pos += 2
                counts[_COUNT_LABELS.get((word >> 11) & 0x1F,
                                          "type{}".format((word >> 11) & 0x1F))] = word & 0x7FF
            if counts: msg["msg_counts"] = counts
        elif frn == 8:                  # I034/100  Generic Polar Window (8 bytes)
            if pos + 8 > len(data): break
            pos += 8
        elif frn == 9:                  # I034/110  Data Filter (1 byte)
            if pos + 1 > len(data): break
            pos += 1
        elif frn == 10:                 # I034/120  3D-Range and Azimuth (compound, skip)
            if pos >= len(data): break
            sf = data[pos]; pos += 1
            if sf & 0x80 and pos + 2 <= len(data): pos += 2
            if sf & 0x40 and pos + 2 <= len(data): pos += 2
            while sf & 0x01:
                if pos >= len(data): break
                sf = data[pos]; pos += 1
                for m in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
                    if (sf & m) and pos + 2 <= len(data): pos += 2
        elif frn == 11:                 # I034/090  Collimation Error (4 bytes)
            if pos + 4 > len(data): break
            msg["collimation_rng_nm"] = round(_s16(data[pos:pos+2]) / 128.0, 4);   pos += 2
            msg["collimation_az_deg"] = round(_s16(data[pos:pos+2]) * 360.0 / 65536.0, 4); pos += 2
        else:
            break
    return msg if msg else None


# ---------------------------------------------------------------------------
# CAT-048 decoder  (Monoradar Target Reports, Edition 1.15)
# ---------------------------------------------------------------------------

def decode_cat048_record(data: bytes, pos: int,
                         radar_lat: float | None, radar_lon: float | None):
    """Decode one CAT-048 record. Returns (track_dict, new_pos)."""
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-48"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I048/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos+1]; pos += 2
        elif frn == 1:                  # I048/140  Time of Day (1/128 s)
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            track["tod_s"] = raw / 128.0; pos += 3
        elif frn == 2:                  # I048/020  Target Report Descriptor (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x10: track["simulated"] = True
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["on_ground"]     = True
                if b & 0x40: track["mil_emergency"]  = True
                foe = (b >> 3) & 0x03
                if foe: track["iff"] = ("", "friendly", "unknown", "no_reply")[foe]
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 3:                  # I048/040  Slant Polar Coordinates
            if pos + 4 > len(data): return track, len(data)
            range_raw = _u16(data[pos:pos+2])
            az_raw    = _u16(data[pos+2:pos+4])
            range_nm  = range_raw / 256.0
            az_deg    = az_raw * 360.0 / 65536.0
            track["range_nm"]    = round(range_nm, 3)
            track["azimuth_deg"] = round(az_deg, 3)
            if radar_lat is not None and radar_lon is not None and range_nm > 0:
                lat, lon = _polar_to_wgs84(radar_lat, radar_lon, range_nm, az_deg)
                track.setdefault("lat_deg", round(lat, 6))
                track.setdefault("lon_deg", round(lon, 6))
            pos += 4
        elif frn == 4:                  # I048/070  Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_u16(data[pos:pos+2]) & 0x0FFF); pos += 2
        elif frn == 5:                  # I048/090  Flight Level (1/4 FL, signed)
            if pos + 2 > len(data): return track, len(data)
            track["alt_baro_ft"] = round(_s16(data[pos:pos+2]) * 0.25 * 100); pos += 2
        elif frn == 6:                  # I048/130  Radar Plot Characteristics (compound)
            if pos >= len(data): break
            psf = data[pos]; pos += 1
            while True:
                if psf & 0x80: pos += 1
                if psf & 0x40: pos += 1
                if psf & 0x20:
                    if pos < len(data):
                        track["rcs_dbm"] = struct.unpack_from("b", data, pos)[0]
                    pos += 1
                if psf & 0x10: pos += 1
                if psf & 0x08: pos += 1
                if psf & 0x04: pos += 2
                if psf & 0x02: pos += 2
                if not (psf & 0x01): break
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 7:                  # I048/220  Aircraft Address (ICAO 24-bit)
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 8:                  # I048/240  Aircraft Identification
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _decode_callsign(data[pos:pos+6]); pos += 6
        elif frn == 9:                  # I048/250  Mode-S MB Data (REP × 8-byte records)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb   = bytes(data[pos:pos + 7])
                bds  = data[pos + 7]; pos += 8
                bds1 = (bds >> 4) & 0x0F; bds2 = bds & 0x0F
                if bds1 == 5 and bds2 == 0:  track.update(_decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_decode_bds60(mb))
        elif frn == 10:                 # I048/161  Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _u16(data[pos:pos+2]) & 0x0FFF; pos += 2
        elif frn == 11:                 # I048/042  Cartesian Position (1/128 NM, s16×2)
            if pos + 4 > len(data): return track, len(data)
            track["cart_x_nm"] = round(_s16(data[pos:pos+2]) / 128.0, 3)
            track["cart_y_nm"] = round(_s16(data[pos+2:pos+4]) / 128.0, 3)
            pos += 4
        elif frn == 12:                 # I048/200  Track Velocity Polar
            if pos + 4 > len(data): return track, len(data)
            spd_raw = _u16(data[pos:pos+2])
            hdg_raw = _u16(data[pos+2:pos+4])
            track["speed_ms"]    = round(spd_raw / 4.0 * 0.514444, 2)
            track["heading_deg"] = round(hdg_raw * 360.0 / 65536.0, 2)
            pos += 4
        elif frn == 13:                 # I048/170  Track Status (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["track_tentative"] = True
            if b & 0x10: track["track_doubtful"]  = True
            if b & 0x08: track["track_manoeuvre"] = True
            cdm = (b & 0x06) >> 1
            if   cdm == 1: track["vertical_trend"] = "climbing"
            elif cdm == 2: track["vertical_trend"] = "descending"
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["track_end"]   = True
                if b & 0x40: track["track_ghost"]  = True
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 14:                 # I048/210  Track Quality (4 bytes)
            if pos + 4 > len(data): return track, len(data)
            track["track_sigma_x_nm"] = round(data[pos]     / 128.0, 4)
            track["track_sigma_y_nm"] = round(data[pos + 1] / 128.0, 4)
            track["track_sigma_h_ft"] = data[pos + 2] * 25
            track["track_sigma_v_kt"] = round(data[pos + 3] / 128.0 * 3600.0, 1)
            pos += 4
        elif frn == 15:                 # I048/030  Warning/Error Conditions (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["spi"]  = True
            if b & 0x40: track["pai"]  = True
            if b & 0x20: track["stc"]  = True
            if b & 0x10: track["apw"]  = True
            if b & 0x04: track["msaw"] = True
            if b & 0x02: track["cst"]  = True
            raw_hex = "{:02x}".format(b)
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
                raw_hex += "{:02x}".format(b)
            track["we_conditions_hex"] = raw_hex
        elif frn == 16:                 # I048/080  Mode-3/A Confidence (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _u16(data[pos:pos + 2])
            if w & 0x2000: track["squawk_not_transponder"] = True
            if w & 0x1000: track["squawk_garbled"]         = True
            if w & 0x0800: track["squawk_smoothed"]        = True
            pos += 2
        elif frn == 17:                 # I048/100  Mode-C Gillham (2 + 2 confidence bytes)
            if pos + 4 > len(data): return track, len(data)
            alt = _gillham_to_ft(_u16(data[pos:pos + 2]))
            if alt is not None: track["mode_c_alt_ft"] = alt
            pos += 4
        elif frn == 18:                 # I048/110  3D Height (25 ft/LSB, signed)
            if pos + 2 > len(data): return track, len(data)
            track["alt_3d_ft"] = _s16(data[pos:pos+2]) * 25; pos += 2
        elif frn == 19:                 # I048/120  Radial Doppler (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80:              # CAL sub-field: 2 bytes s16, 1/256 NM/s
                if pos + 2 > len(data): return track, len(data)
                track["doppler_kt"] = round(_s16(data[pos:pos + 2]) / 256.0 * 3600.0, 1)
                pos += 2
            if psf & 0x40:              # RDS sub-field: REP × 6 bytes
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                if rep > 0 and pos + 4 <= len(data):
                    spd = _s16(data[pos + 2:pos + 4])
                    track["doppler_raw_kt"] = round(spd / 256.0 * 3600.0, 1)
                pos += rep * 6
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 20:                 # I048/230  Communications/ACAS Capability (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            b0 = data[pos]; b1 = data[pos + 1]
            com = (b0 >> 5) & 0x07
            if com: track["com_capability"] = com
            if b1 & 0x80: track["mssc"]        = True
            if b1 & 0x40: track["altitude_25ft"] = True
            if b1 & 0x20: track["aic"]          = True
            pos += 2
        elif frn == 21:                 # I048/260  ACAS RA (7 bytes = BDS 3,0)
            if pos + 7 > len(data): return track, len(data)
            track.update(_decode_bds30(data[pos:pos + 7]))
            pos += 7
        elif frn == 22:                 # I048/055  Mode-1
            if pos >= len(data): return track, len(data)
            track["mode1"] = "{:02o}".format(data[pos] & 0x3F); pos += 1
        elif frn == 23:                 # I048/050  Mode-2 Code (2 bytes, lower 12 bits)
            if pos + 2 > len(data): return track, len(data)
            track["mode2"] = "{:04o}".format(_u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        else: break

    if "icao24" not in track:
        sac = track.get("sac", 0); sic = track.get("sic", 0)
        track["radar_id"] = "CAT48-{:03d}-{:03d}-{:04d}".format(
            sac, sic, track.get("track_num", 0))
    return track, pos


# ---------------------------------------------------------------------------
# CAT-020 decoder  (MLAT Target Reports, Edition 1.9)
# ---------------------------------------------------------------------------

def decode_cat020_record(data: bytes, pos: int):
    """Decode one CAT-020 MLAT record. Returns (track_dict, new_pos)."""
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-20"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I020/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I020/020  Target Report Descriptor (FX)
            pos = _skip_fx_field(data, pos)
        elif frn == 2:                  # I020/140  Time of Day (1/128 s)
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I020/041  WGS-84 (4+4 bytes, 180/2^25)
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 25)
            track["lat_deg"] = round(_s32(data[pos:pos + 4]) * scale, 6)
            track["lon_deg"] = round(_s32(data[pos + 4:pos + 8]) * scale, 6)
            pos += 8
        elif frn == 4:                  # I020/042  Cartesian (x 3B + y 3B, 0.5 m)
            if pos + 6 > len(data): return track, len(data)
            track["x_m"] = round(_s24(data[pos:pos + 3]) * 0.5, 1)
            track["y_m"] = round(_s24(data[pos + 3:pos + 6]) * 0.5, 1)
            pos += 6
        elif frn == 5:                  # I020/070  Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 6:                  # I020/090  Barometric FL (1/4 FL × 100 ft)
            if pos + 2 > len(data): return track, len(data)
            track["alt_baro_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 7:                  # I020/100  Mode-C + Confidence (4 bytes)
            if pos + 4 > len(data): return track, len(data)
            alt = _gillham_to_ft(_u16(data[pos:pos + 2]))
            if alt is not None: track["mode_c_alt_ft"] = alt
            pos += 4
        elif frn == 8:                  # I020/220  ICAO 24-bit
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 9:                  # I020/245  Target ID (1B flags + 6B callsign)
            if pos + 7 > len(data): return track, len(data)
            pos += 1
            track["callsign"] = _decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 10:                 # I020/110  3D Radar Height (25 ft/LSB)
            if pos + 2 > len(data): return track, len(data)
            track["alt_3d_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 11:                 # I020/105  Geometric Altitude (6.25 ft/LSB)
            if pos + 2 > len(data): return track, len(data)
            track["alt_geom_ft"] = round(_s16(data[pos:pos + 2]) * 6.25); pos += 2
        elif frn == 12:                 # I020/210  Track Quality (compound, same layout as I048/210)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80:
                if pos + 1 > len(data): return track, len(data)
                track["track_sigma_x_nm"] = round(data[pos] / 128.0, 4); pos += 1
            if psf & 0x40:
                if pos + 1 > len(data): return track, len(data)
                track["track_sigma_y_nm"] = round(data[pos] / 128.0, 4); pos += 1
            if psf & 0x20:
                if pos + 1 > len(data): return track, len(data)
                track["track_sigma_h_ft"] = data[pos] * 25; pos += 1
            if psf & 0x10:
                if pos + 1 > len(data): return track, len(data)
                track["track_sigma_v_kt"] = round(data[pos] / 128.0 * 3600.0, 1); pos += 1
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 13: pos += 1        # I020/300  Vehicle Fleet ID
        elif frn == 14: pos = _skip_fx_field(data, pos)  # I020/310
        elif frn == 15:                 # I020/500  Position Accuracy (compound, skip sub-fields)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80: pos += 4   # DOP matrix (4 bytes)
            if psf & 0x40:            # σ lat/lon (4 bytes each × 2 = 8 bytes, u16 units)
                if pos + 4 <= len(data):
                    track["pos_accuracy_lat_m"] = round(_u16(data[pos:pos+2]) * 0.5, 1)
                    track["pos_accuracy_lon_m"] = round(_u16(data[pos+2:pos+4]) * 0.5, 1)
                pos += 4
            if psf & 0x20: pos += 2   # σ height
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 16:                 # I020/400  Contributing Receivers (variable)
            if pos + 1 > len(data): return track, len(data)
            count = data[pos]; pos += 1 + count
        elif frn == 17:                 # I020/250  Mode-S MB Data (REP × 8 bytes)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb   = bytes(data[pos:pos + 7])
                bds  = data[pos + 7]; pos += 8
                bds1 = (bds >> 4) & 0x0F; bds2 = bds & 0x0F
                if bds1 == 3 and bds2 == 0: track.update(_decode_bds30(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_decode_bds60(mb))
        else: break
    return track, pos


# ---------------------------------------------------------------------------
# CAT-021 decoder  (ADS-B Target Reports, Edition 2.4)
# ---------------------------------------------------------------------------

def decode_cat021_record(data: bytes, pos: int):
    """Decode one CAT-021 ADS-B record. Returns (track_dict, new_pos)."""
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-21"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I021/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos + 1]; pos += 2
        elif frn == 1: pos = _skip_fx_field(data, pos)   # I021/040  TRD (FX)
        elif frn == 2:                  # I021/030  Time of Day (1/128 s)
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I021/130  WGS-84 (3+3 bytes, 180/2^23)
            if pos + 6 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 23)
            track["lat_deg"] = round(_s24(data[pos:pos + 3]) * scale, 6)
            track["lon_deg"] = round(_s24(data[pos + 3:pos + 6]) * scale, 6)
            pos += 6
        elif frn == 4:                  # I021/080  ICAO 24-bit
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 5:                  # I021/140  Geometric Altitude (6.25 ft/LSB)
            if pos + 2 > len(data): return track, len(data)
            track["alt_geom_ft"] = round(_s16(data[pos:pos + 2]) * 6.25); pos += 2
        elif frn == 6:                  # I021/090  Figure of Merit (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _u16(data[pos:pos + 2])
            track["nac_p"] = (w >> 9) & 0x0F
            track["nic"]   = (w >> 5) & 0x0F
            track["nac_v"] = (w >> 1) & 0x0F
            pos += 2
        elif frn == 7:                  # I021/210  Link Technology (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            lt = []
            if b & 0x80: lt.append("VDL4")
            if b & 0x40: lt.append("UAT")
            if b & 0x20: lt.append("1090ES")
            if lt: track["link_tech"] = lt
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
        elif frn == 8:                  # I021/230  Roll Angle (2 bytes, s16, 45/512 deg)
            if pos + 2 > len(data): return track, len(data)
            track["roll_deg"] = round(_s16(data[pos:pos + 2]) * 45.0 / 512.0, 1)
            pos += 2
        elif frn == 9:                  # I021/145  Barometric FL (1/4 FL × 100 ft)
            if pos + 2 > len(data): return track, len(data)
            track["alt_baro_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 10:                 # I021/150  Air Speed (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _u16(data[pos:pos + 2])
            im  = (w >> 15) & 1
            val = w & 0x7FFF
            if im:
                track["mach"] = round(val * 2.0 / (2 ** 14), 3)
            else:
                track["ias_kt"] = round(val * 3600.0 / 16384.0 * 0.539957, 1)
            pos += 2
        elif frn == 11:                 # I021/151  True Airspeed (2 bytes, u15 kt)
            if pos + 2 > len(data): return track, len(data)
            w = _u16(data[pos:pos + 2]) & 0x7FFF
            if w: track["tas_kt"] = w
            pos += 2
        elif frn == 12:                 # I021/152  Magnetic Heading (2 bytes, u16)
            if pos + 2 > len(data): return track, len(data)
            track["mag_hdg_deg"] = round(_u16(data[pos:pos + 2]) * 360.0 / 65536.0, 2)
            pos += 2
        elif frn == 13:                 # I021/155  Barometric Vertical Rate (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _u16(data[pos:pos + 2])
            re  = (w >> 15) & 1
            if not re:
                val = w & 0x7FFF
                if val >= 0x4000: val -= 0x8000
                track["baro_vr_fpm"] = round(val * 6.25)
            pos += 2
        elif frn == 14:                 # I021/157  Geometric Vertical Rate (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _u16(data[pos:pos + 2])
            re  = (w >> 15) & 1
            if not re:
                val = w & 0x7FFF
                if val >= 0x4000: val -= 0x8000
                track["geo_vr_fpm"] = round(val * 6.25)
            pos += 2
        elif frn == 15:                 # I021/160  Ground Vector (GS 2B + TA 2B)
            if pos + 4 > len(data): return track, len(data)
            gs_raw = _u16(data[pos:pos + 2]) & 0x7FFF
            ta_raw = _u16(data[pos + 2:pos + 4])
            track["speed_ms"]    = round(gs_raw * (3600.0 / 16384.0) * 0.514444, 2)
            track["heading_deg"] = round(ta_raw * 360.0 / 65536.0, 2)
            pos += 4
        elif frn == 16:                 # I021/165  Track Angle Rate (2 bytes, s16)
            if pos + 2 > len(data): return track, len(data)
            track["track_angle_rate_degs"] = round(_s16(data[pos:pos + 2]) * 360.0 / 65536.0, 3)
            pos += 2
        elif frn == 17:                 # I021/170  Target Identification (callsign)
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 18:                 # I021/095  Velocity Accuracy (1 byte)
            if pos + 1 > len(data): return track, len(data)
            nac_v = (data[pos] >> 4) & 0x0F
            if nac_v and "nac_v" not in track: track["nac_v"] = nac_v
            pos += 1
        elif frn == 19: pos += 1        # I021/032  Time Accuracy (skip)
        elif frn == 20:                 # I021/200  Target Status (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x40: track["intent_change"]   = True
            if b & 0x20: track["tcas_operational"] = True
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
        elif frn == 21:                 # I021/020  Emitter Category (1 byte)
            if pos + 1 > len(data): return track, len(data)
            ec = data[pos]; pos += 1
            track["emitter_category"]     = ec
            track["emitter_category_str"] = _EMITTER_CATEGORY.get(ec, "cat{}".format(ec))
        elif frn == 22:                 # I021/220  Met Information (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80:              # wind speed (2 bytes, 0.5 kt)
                if pos + 2 > len(data): return track, len(data)
                track["wind_speed_kt"] = round(_u16(data[pos:pos + 2]) * 0.5, 1); pos += 2
            if psf & 0x40:              # wind direction (2 bytes, 360/65536)
                if pos + 2 > len(data): return track, len(data)
                track["wind_dir_deg"] = round(_u16(data[pos:pos + 2]) * 360.0 / 65536.0, 1); pos += 2
            if psf & 0x20:              # temperature (2 bytes, s16, 0.25°C)
                if pos + 2 > len(data): return track, len(data)
                track["temp_c"] = round(_s16(data[pos:pos + 2]) * 0.25, 1); pos += 2
            if psf & 0x10:              # turbulence (1 byte)
                if pos + 1 > len(data): return track, len(data)
                track["turbulence"] = data[pos]; pos += 1
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 23:                 # I021/146  Selected Altitude (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w   = _u16(data[pos:pos + 2])
            src = (w >> 13) & 0x03
            val = w & 0x1FFF
            if val >= 0x1000: val -= 0x2000
            track["selected_alt_ft"]     = val * 25
            track["selected_alt_source"] = ("MCP/FCU", "FMS", "FMS2", "reserved")[src]
            pos += 2
        elif frn == 24:                 # I021/148  Final State Selected Altitude
            if pos + 2 > len(data): return track, len(data)
            w   = _u16(data[pos:pos + 2]) & 0x1FFF
            if w >= 0x1000: w -= 0x2000
            track["final_alt_ft"] = w * 25; pos += 2
        elif frn == 25:                 # I021/110  Trajectory Intent (compound, skip)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80: pos += 1     # TIS sub-field (1 byte)
            if psf & 0x40:              # TID: REP × 15 bytes
                if pos < len(data):
                    rep = data[pos]; pos += 1 + rep * 15
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 26:                 # I021/070  Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 27:                 # I021/131  High-Res WGS-84 (4+4, 180/2^31)
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 31)
            track["lat_deg"] = round(_s32(data[pos:pos + 4]) * scale, 7)
            track["lon_deg"] = round(_s32(data[pos + 4:pos + 8]) * scale, 7)
            pos += 8
        else: break
    return track, pos


# ---------------------------------------------------------------------------
# CAT-062 compound helpers
# ---------------------------------------------------------------------------

def _decode_i062_380(data: bytes, pos: int) -> tuple[dict, int]:
    """I062/380 Aircraft Derived Data — PSF-gated sub-fields."""
    out: dict = {}
    if pos >= len(data):
        return out, pos
    psf = data[pos]; pos += 1
    # Sub 01: Aircraft Address (3 bytes)
    if psf & 0x80:
        if pos + 3 > len(data): return out, pos
        addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
        out["icao24"] = "{:06x}".format(addr); pos += 3
    # Sub 02: Aircraft ID (1B flags + 6B callsign)
    if psf & 0x40:
        if pos + 7 > len(data): return out, pos
        pos += 1
        out["callsign"] = _decode_callsign(data[pos:pos+6]); pos += 6
    # Sub 03: Roll Angle (2 bytes, s16, 45/512 deg)
    if psf & 0x20:
        if pos + 2 > len(data): return out, pos
        out["roll_deg"] = round(_s16(data[pos:pos+2]) * 45.0 / 512.0, 1); pos += 2
    # Sub 04: Track Angle (2 bytes, u16, 360/65536)
    if psf & 0x10:
        if pos + 2 > len(data): return out, pos
        out["true_track_deg"] = round(_u16(data[pos:pos+2]) * 360.0 / 65536.0, 2); pos += 2
    # Sub 05: Airspeed (2 bytes, IM + 15-bit)
    if psf & 0x08:
        if pos + 2 > len(data): return out, pos
        w = _u16(data[pos:pos+2])
        im = (w >> 15) & 1; val = w & 0x7FFF
        if im: out["mach"]   = round(val * 2.0 / (2**14), 3)
        else:  out["ias_kt"] = round(val * 3600.0 / 16384.0 * 0.539957, 1)
        pos += 2
    # Sub 06: TAS (2 bytes, u16, 1 kt/LSB)
    if psf & 0x04:
        if pos + 2 > len(data): return out, pos
        w = _u16(data[pos:pos+2]) & 0x7FFF
        if w: out["tas_kt"] = w; pos += 2
    # Sub 07: SSR modes (2 bytes, skip)
    if psf & 0x02:
        pos += 2
    # FX bit → read next PSF byte
    if not (psf & 0x01):
        return out, pos
    if pos >= len(data):
        return out, pos
    psf2 = data[pos]; pos += 1
    # Sub 08: Emergency (1 byte)
    if psf2 & 0x80:
        if pos + 1 > len(data): return out, pos
        ec = data[pos] & 0x07; pos += 1
        out["emergency_code"] = ec
        s = _EMERGENCY_CODES.get(ec)
        if s: out["emergency_str"] = s
    # Sub 09: Met (wind 2B + dir 2B + temp 2B + turb 2B = 8 bytes)
    if psf2 & 0x40:
        if pos + 8 > len(data): return out, pos
        out["wind_speed_kt"] = round(_u16(data[pos:pos+2]) * 0.5, 1)
        out["wind_dir_deg"]  = round(_u16(data[pos+2:pos+4]) * 360.0 / 65536.0, 1)
        out["temp_c"]        = round(_s16(data[pos+4:pos+6]) * 0.25, 1)
        pos += 8
    # Sub 10: ACAS RA (7 bytes, BDS 3,0)
    if psf2 & 0x20:
        if pos + 7 > len(data): return out, pos
        out.update(_decode_bds30(data[pos:pos+7])); pos += 7
    # Sub 11: Barometric Alt (2 bytes, s16, 0.25 FL)
    if psf2 & 0x10:
        if pos + 2 > len(data): return out, pos
        out["alt_baro_ft"] = round(_s16(data[pos:pos+2]) * 0.25 * 100); pos += 2
    # Sub 12: Mode-C code (2 bytes, Gillham)
    if psf2 & 0x08:
        if pos + 2 > len(data): return out, pos
        alt = _gillham_to_ft(_u16(data[pos:pos+2]))
        if alt is not None: out["mode_c_alt_ft"] = alt
        pos += 2
    # Sub 13: ICAO address (3 bytes, fallback)
    if psf2 & 0x04:
        if pos + 3 > len(data): return out, pos
        addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
        out.setdefault("icao24", "{:06x}".format(addr)); pos += 3
    # Sub 14: Mode-S MB data (REP × 8 bytes)
    if psf2 & 0x02:
        if pos >= len(data): return out, pos
        rep = data[pos]; pos += 1
        for _ in range(rep):
            if pos + 8 > len(data): break
            mb = bytes(data[pos:pos+7]); bds = data[pos+7]; pos += 8
            b1 = (bds >> 4) & 0xF; b2 = bds & 0xF
            if b1 == 3 and b2 == 0: out.update(_decode_bds30(mb))
            elif b1 == 5 and b2 == 0: out.update(_decode_bds50(mb))
            elif b1 == 6 and b2 == 0: out.update(_decode_bds60(mb))
    # Consume any further FX extension bytes
    while psf2 & 0x01:
        if pos >= len(data): break
        psf2 = data[pos]; pos += 1
    return out, pos


_WTC_MAP = {76: "L", 77: "M", 72: "H", 74: "J"}   # ASCII: L M H J


def _decode_i062_390(data: bytes, pos: int) -> tuple[dict, int]:
    """I062/390 Flight Plan Data — PSF-gated sub-fields."""
    out: dict = {}
    if pos >= len(data):
        return out, pos
    psf = data[pos]; pos += 1
    # CS: 1B quality + 6B callsign
    if psf & 0x80:
        if pos + 7 > len(data): return out, pos
        pos += 1
        out["fp_callsign"] = _decode_callsign(data[pos:pos+6]); pos += 6
    # IFI: 4 bytes
    if psf & 0x40: pos += 4
    # FCT: 1 byte
    if psf & 0x20:
        if pos + 1 > len(data): return out, pos
        pos += 1
    # TAC: 4 ASCII bytes (ICAO type designator)
    if psf & 0x10:
        if pos + 4 > len(data): return out, pos
        out["aircraft_type"] = data[pos:pos+4].decode("ascii", errors="replace").strip("\x00 ")
        pos += 4
    # WTC: 1 byte ASCII
    if psf & 0x08:
        if pos + 1 > len(data): return out, pos
        out["wake_turb_cat"] = _WTC_MAP.get(data[pos], chr(data[pos])); pos += 1
    # DEP: 4 ASCII bytes
    if psf & 0x04:
        if pos + 4 > len(data): return out, pos
        out["departure_icao"] = data[pos:pos+4].decode("ascii", errors="replace").strip("\x00 ")
        pos += 4
    # DST: 4 ASCII bytes
    if psf & 0x02:
        if pos + 4 > len(data): return out, pos
        out["destination_icao"] = data[pos:pos+4].decode("ascii", errors="replace").strip("\x00 ")
        pos += 4
    if not (psf & 0x01):
        return out, pos
    if pos >= len(data):
        return out, pos
    psf2 = data[pos]; pos += 1
    # CFL: 2 bytes, s16, 0.25 FL
    if psf2 & 0x80:
        if pos + 2 > len(data): return out, pos
        out["cleared_fl"] = round(_s16(data[pos:pos+2]) * 0.25, 0); pos += 2
    # Consume any further PSF bytes
    while psf2 & 0x01:
        if pos >= len(data): break
        psf2 = data[pos]; pos += 1
    return out, pos


# ---------------------------------------------------------------------------
# CAT-062 decoder  (System Track Updates)
# ---------------------------------------------------------------------------

def decode_cat62_record(data: bytes, pos: int):
    """Decode one CAT-062 system track record. Returns (track_dict, new_pos)."""
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-62"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I062/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I062/015  Service Identification
            if pos + 1 > len(data): return track, len(data)
            track["service_id"] = data[pos]; pos += 1
        elif frn == 2:                  # I062/070  Time Of Track (1/128 s)
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I062/105  WGS-84 (2 × s32, 180/2^25)
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 25)
            track["lat_deg"] = round(_s32(data[pos:pos + 4]) * scale, 6)
            track["lon_deg"] = round(_s32(data[pos + 4:pos + 8]) * scale, 6)
            pos += 8
        elif frn == 4:                  # I062/100  Cartesian position (0.5 m/LSB)
            if pos + 4 > len(data): return track, len(data)
            track["x_m"] = _s16(data[pos:pos + 2]) * 0.5
            track["y_m"] = _s16(data[pos + 2:pos + 4]) * 0.5
            pos += 4
        elif frn == 5:                  # I062/185  Cartesian velocity (0.25 m/s/LSB)
            if pos + 4 > len(data): return track, len(data)
            vx = _s16(data[pos:pos + 2]) * 0.25
            vy = _s16(data[pos + 2:pos + 4]) * 0.25
            track["vx_ms"] = vx; track["vy_ms"] = vy
            track["speed_ms"]    = round(math.hypot(vx, vy), 2)
            track["heading_deg"] = round(math.degrees(math.atan2(vx, vy)) % 360.0, 2)
            pos += 4
        elif frn == 6:                  # I062/210  Acceleration (0.25 m/s²/LSB)
            if pos + 4 > len(data): return track, len(data)
            track["ax_ms2"] = _s16(data[pos:pos + 2]) * 0.25
            track["ay_ms2"] = _s16(data[pos + 2:pos + 4]) * 0.25
            pos += 4
        elif frn == 7:                  # I062/060  Mode-3/A
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 8:                  # I062/040  Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 9:                  # I062/080  Track Status (FX)
            start = pos
            pos = _skip_fx_field(data, pos)
            if start < len(data):
                track["track_confirmed"] = not bool(data[start] & 0x80)
        elif frn == 10:                 # I062/290  System Track Update Ages (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            age_map = [(0x80,"psr"),(0x40,"ssr"),(0x20,"mds"),(0x10,"ads"),
                       (0x08,"es"),(0x04,"vdl"),(0x02,"uat")]
            for mask, label in age_map:
                if psf & mask:
                    if pos + 1 > len(data): return track, len(data)
                    track["track_age_{}_s".format(label)] = round(data[pos] * 0.25, 2); pos += 1
            if psf & 0x01:              # FX: LOP, MLT (1 byte each)
                if pos >= len(data): return track, len(data)
                psf2 = data[pos]; pos += 1
                for mask in (0x80, 0x40):
                    if psf2 & mask:
                        if pos + 1 > len(data): return track, len(data)
                        pos += 1
                while psf2 & 0x01:
                    if pos >= len(data): break
                    psf2 = data[pos]; pos += 1
        elif frn == 11: pos += 2        # I062/200  Mode of Movement
        elif frn == 13:                 # I062/136  Measured Flight Level
            if pos + 2 > len(data): return track, len(data)
            track["measured_fl"] = _s16(data[pos:pos + 2]) * 0.25; pos += 2
        elif frn == 14:                 # I062/130  Calculated Altitude
            if pos + 2 > len(data): return track, len(data)
            track["calc_alt_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 18: pos += 1        # I062/300  Vehicle Fleet ID
        elif frn == 12:                 # I062/295  Track Data Ages (compound, skip sub-fields)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
                if psf & mask:
                    if pos + 1 > len(data): return track, len(data)
                    pos += 1
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
                for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
                    if psf & mask:
                        if pos + 1 > len(data): return track, len(data)
                        pos += 1
        elif frn == 15:                 # I062/380  Aircraft Derived Data
            extra, pos = _decode_i062_380(data, pos)
            track.update(extra)
        elif frn == 16:                 # I062/390  Flight Plan Data
            extra, pos = _decode_i062_390(data, pos)
            track.update(extra)
        elif frn == 17:                 # I062/270  Target Size/Orientation (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["target_length_m"] = (b >> 1) & 0x7F
            if b & 0x01:
                if pos < len(data):
                    b2 = data[pos]; pos += 1
                    track["target_orientation_deg"] = round((b2 >> 1) * 360.0 / 128.0, 1)
                    if b2 & 0x01 and pos < len(data):
                        b3 = data[pos]; pos += 1
                        track["target_width_m"] = (b3 >> 1) & 0x7F
        elif frn == 22:                 # I062/500  Estimated Accuracies (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            # Apc sub-field: 2 × u16 (lat/lon accuracy, 0.5m/LSB)
            if psf & 0x80:
                if pos + 4 <= len(data):
                    track["pos_accuracy_lat_m"] = round(_u16(data[pos:pos+2]) * 0.5, 1)
                    track["pos_accuracy_lon_m"] = round(_u16(data[pos+2:pos+4]) * 0.5, 1)
                    pos += 4
            for mask in (0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
                if psf & mask: pos += 2
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
                for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
                    if psf & mask: pos += 2
        elif frn == 23:                 # I062/340  Measured Information (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            measured_by = []
            if psf & 0x80:              # SID: SAC/SIC (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                measured_by.append("{}/{}".format(data[pos], data[pos+1])); pos += 2
            if psf & 0x40: pos += 4    # range + azimuth
            if psf & 0x20: pos += 2    # height (mode-C)
            if psf & 0x10: pos += 4    # mode-2 + confidence
            if psf & 0x08: pos += 2    # mode-1
            if psf & 0x04: pos += 4    # mode-5 + confidence (skip)
            if psf & 0x02: pos += 2    # spare
            if measured_by: track["measured_by"] = measured_by
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn in (19, 20, 21, 24, 25):
            break                       # Mode-5/hidden/composed track — skip unrecoverable
        else:
            break
    return track, pos


# ---------------------------------------------------------------------------
# Generic publish helper
# ---------------------------------------------------------------------------

def _pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    pub.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        ident = (track.get("icao24") or track.get("callsign") or
                 track.get("radar_id") or track.get("track_num") or "?")
        print("PUB {} {} lat={:.4f} lon={:.4f} sq={} alt={}".format(
            label, ident,
            track.get("lat_deg", 0), track.get("lon_deg", 0),
            track.get("squawk", "----"),
            track.get("alt_baro_ft") or track.get("alt_geom_ft") or
            track.get("calc_alt_ft") or "---",
        ), flush=True)


# ---------------------------------------------------------------------------
# Per-category handler factories
# ---------------------------------------------------------------------------

def _make_cat020_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat020_record(data, pos)
            if len(track) > 2:
                _pub(pub, track, "cat20", verbose)
    return _h


def _make_cat021_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat021_record(data, pos)
            if len(track) > 2:
                _pub(pub, track, "cat21", verbose)
    return _h


def _make_cat034_handler(pub_sensor, radar_lat, radar_lon, radar_name):
    def _h(data: bytes, verbose: bool):
        msg = decode_cat034(data)
        if not msg:
            return
        mtype = msg.get("msg_type", "?")
        tod   = msg.get("tod_s")
        rot   = msg.get("rotation_s")
        if verbose:
            print("CAT-034 {} tod={} rot={}s psr={} ssr={} mds={} "
                  "cal_az={} cal_rng={}".format(
                mtype,
                "{:.2f}".format(tod) if tod else "-",
                "{:.2f}".format(rot) if rot else "-",
                msg.get("psr_status", "-"), msg.get("ssr_status", "-"),
                msg.get("mds_status", "-"),
                msg.get("collimation_az_deg", "-"),
                msg.get("collimation_rng_nm", "-"),
            ), flush=True)
        if mtype == "north_marker" and pub_sensor and radar_lat and radar_lon:
            sac = msg.get("sac", 0); sic = msg.get("sic", 0)
            status = {
                "_src":        "ASTERIX CAT-34",
                "_ts":         time.time(),
                "sensor_type": "radar",
                "sensor_id":   "CAT34-{}-{}".format(sac, sic),
                "sensor_name": radar_name or "RADAR SAC{}/SIC{}".format(sac, sic),
                "lat_deg":     radar_lat,
                "lon_deg":     radar_lon,
            }
            status.update(msg)
            pub_sensor.put(json.dumps(status).encode(),
                           encoding=zenoh.Encoding.APPLICATION_JSON)
    return _h


def _make_cat048_handler(pub, radar_lat, radar_lon):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat048_record(data, pos, radar_lat, radar_lon)
            if len(track) > 2:
                if verbose and "lat_deg" not in track:
                    ident = track.get("icao24") or track.get("radar_id") or "PSR"
                    print("cat48 {} no-position (set --radar-lat/--radar-lon)".format(
                        ident), flush=True)
                _pub(pub, track, "cat48", verbose)
    return _h


def _make_cat062_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat62_record(data, pos)
            if len(track) > 2:
                _pub(pub, track, "cat62", verbose)
    return _h


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _process_stream(iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)


def _run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("{} TCP server on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("{} TCP connected: {}".format(label, addr), flush=True)
            threading.Thread(
                target=_process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _process_stream(iter_frames_udp(sock), handlers, verbose)


def _run_cat62(host: str, port: int, udp: bool, handler, verbose: bool):
    """CAT-062: outbound TCP connect with auto-reconnect, or inbound UDP."""
    if udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-62 UDP on 0.0.0.0:{}".format(port), flush=True)
        _process_stream(iter_frames_udp(sock), {CAT_062: handler}, verbose)
        return
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            print("CAT-62 TCP connected to {}:{}".format(host, port), flush=True)
            _process_stream(iter_frames_tcp(sock), {CAT_062: handler}, verbose)
        except (EOFError, ConnectionRefusedError, OSError) as exc:
            print("CAT-62 error: {} — reconnecting in {}s".format(
                exc, RECONNECT_DELAY_S), flush=True)
            if sock:
                try: sock.close()
                except Exception: pass
        time.sleep(RECONNECT_DELAY_S)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Unified ASTERIX CAT-020/021/034/048/062 → Zenoh bridge")

    # CAT-048/034 inbound (shared stream from primary radar)
    ap.add_argument("--cat48-port", type=int,
                    default=int(os.environ.get("CAT48_PORT", 0) or 0),
                    help="Inbound port for CAT-034/048 stream")
    ap.add_argument("--cat48-tcp", action="store_true",
                    help="TCP server mode for CAT-034/048 (default: UDP)")
    ap.add_argument("--radar-lat", type=float, default=_env_float("CAT48_RADAR_LAT"),
                    help="Radar antenna latitude (polar→WGS-84)")
    ap.add_argument("--radar-lon", type=float, default=_env_float("CAT48_RADAR_LON"),
                    help="Radar antenna longitude")
    ap.add_argument("--radar-name", default=os.environ.get("CAT48_RADAR_NAME", ""),
                    help="Radar site name for ATAK marker label")

    # CAT-021 ADS-B inbound
    ap.add_argument("--cat21-port", type=int,
                    default=int(os.environ.get("CAT21_PORT", 0) or 0),
                    help="Inbound port for CAT-021 ADS-B stream")
    ap.add_argument("--cat21-tcp", action="store_true",
                    help="TCP server mode for CAT-021 (default: UDP)")

    # CAT-020 MLAT inbound
    ap.add_argument("--cat20-port", type=int,
                    default=int(os.environ.get("CAT20_PORT", 0) or 0),
                    help="Inbound port for CAT-020 MLAT stream")
    ap.add_argument("--cat20-tcp", action="store_true",
                    help="TCP server mode for CAT-020 (default: UDP)")

    # CAT-062 outbound TCP (or inbound UDP)
    ap.add_argument("--cat62-host", default=os.environ.get("CAT62_HOST", ""),
                    help="Radar host for CAT-062 outbound TCP")
    ap.add_argument("--cat62-port", type=int,
                    default=int(os.environ.get("CAT62_PORT", 30002) or 30002),
                    help="Radar port for CAT-062 (default: 30002)")
    ap.add_argument("--cat62-udp", action="store_true",
                    help="Inbound UDP mode for CAT-062 (use --cat62-port for the port)")
    ap.add_argument("--cat62-topic", default=TOPIC_062,
                    help="Zenoh topic override for CAT-062 tracks")

    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    radar_lat  = args.radar_lat  if args.radar_lat  != 0.0 else None
    radar_lon  = args.radar_lon  if args.radar_lon  != 0.0 else None
    radar_name = args.radar_name or None

    active = any([args.cat48_port, args.cat21_port, args.cat20_port,
                  args.cat62_host, args.cat62_udp])
    if not active:
        print("No ASTERIX ports configured. Set at least one of:", flush=True)
        print("  --cat48-port  --cat21-port  --cat20-port  --cat62-host/--cat62-udp",
              flush=True)
        return

    session = zenoh.open(make_config())
    pubs    = []
    threads = []

    try:
        if args.cat48_port:
            pub_048    = session.declare_publisher(TOPIC_048); pubs.append(pub_048)
            pub_sensor = None
            if radar_lat and radar_lon:
                pub_sensor = session.declare_publisher(TOPIC_SENSOR)
                pubs.append(pub_sensor)
                print("Zenoh CAT-34 topic:", TOPIC_SENSOR, flush=True)
            else:
                print("WARNING: --radar-lat/--radar-lon not set — "
                      "polar plots will have no WGS-84 position", flush=True)
            print("Zenoh CAT-48 topic:", TOPIC_048, flush=True)
            h034 = _make_cat034_handler(pub_sensor, radar_lat, radar_lon, radar_name)
            h048 = _make_cat048_handler(pub_048, radar_lat, radar_lon)
            threads.append(threading.Thread(
                target=_run_inbound,
                args=(args.cat48_port, args.cat48_tcp, "CAT-48/34",
                      {CAT_034: h034, CAT_048: h048}, args.verbose),
                daemon=True))

        if args.cat21_port:
            pub_021 = session.declare_publisher(TOPIC_021); pubs.append(pub_021)
            print("Zenoh CAT-21 topic:", TOPIC_021, flush=True)
            threads.append(threading.Thread(
                target=_run_inbound,
                args=(args.cat21_port, args.cat21_tcp, "CAT-21",
                      {CAT_021: _make_cat021_handler(pub_021)}, args.verbose),
                daemon=True))

        if args.cat20_port:
            pub_020 = session.declare_publisher(TOPIC_020); pubs.append(pub_020)
            print("Zenoh CAT-20 topic:", TOPIC_020, flush=True)
            threads.append(threading.Thread(
                target=_run_inbound,
                args=(args.cat20_port, args.cat20_tcp, "CAT-20",
                      {CAT_020: _make_cat020_handler(pub_020)}, args.verbose),
                daemon=True))

        if args.cat62_host or args.cat62_udp:
            pub_062 = session.declare_publisher(args.cat62_topic); pubs.append(pub_062)
            print("Zenoh CAT-62 topic:", args.cat62_topic, flush=True)
            threads.append(threading.Thread(
                target=_run_cat62,
                args=(args.cat62_host, args.cat62_port, args.cat62_udp,
                      _make_cat062_handler(pub_062), args.verbose),
                daemon=True))

        for t in threads:
            t.start()

        threading.Event().wait()   # block until KeyboardInterrupt

    except KeyboardInterrupt:
        pass
    finally:
        for p in pubs:
            try: p.undeclare()
            except Exception: pass
        session.close()


if __name__ == "__main__":
    main()
