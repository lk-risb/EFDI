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
        elif frn == 11:                 # I048/042  Cartesian Position (skip)
            if pos + 4 > len(data): return track, len(data)
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
        elif frn == 14: pos += 4        # I048/210  Track Quality
        elif frn == 15: pos = _skip_fx_field(data, pos)  # I048/030  Warnings
        elif frn == 16: pos += 2        # I048/080  Mode-3/A Confidence
        elif frn == 17: pos += 4        # I048/100  Mode-C + Confidence
        elif frn == 18:                 # I048/110  3D Height (25 ft/LSB, signed)
            if pos + 2 > len(data): return track, len(data)
            track["alt_3d_ft"] = _s16(data[pos:pos+2]) * 25; pos += 2
        elif frn == 19:                 # I048/120  Radial Doppler (compound, skip)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80: pos += 2
            if psf & 0x40:
                if pos < len(data): pos += 1 + data[pos] * 6
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 20: pos += 2        # I048/230  Comms/ACAS
        elif frn == 21: pos += 7        # I048/260  ACAS RA Report
        elif frn == 22:                 # I048/055  Mode-1
            if pos >= len(data): return track, len(data)
            track["mode1"] = "{:02o}".format(data[pos] & 0x3F); pos += 1
        elif frn == 23: pos += 2        # I048/050  Mode-2
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
        elif frn == 7: break            # I020/100  Mode-C + Confidence (compound)
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
        elif frn == 12: break           # I020/210  Track Quality (compound)
        elif frn == 13: pos += 1        # I020/300  Vehicle Fleet ID
        elif frn == 14: pos = _skip_fx_field(data, pos)  # I020/310
        elif frn == 15: break           # I020/500  Position Accuracy (compound)
        elif frn == 16:                 # I020/400  Contributing Receivers (variable)
            if pos + 1 > len(data): return track, len(data)
            count = data[pos]; pos += 1 + count
        elif frn == 17: break           # I020/250  Mode-S MB Data (compound)
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
        elif frn == 6:  pos += 2        # I021/090  Figure of Merit
        elif frn == 7:  pos = _skip_fx_field(data, pos)  # I021/210  Link Tech (FX)
        elif frn == 8:  pos += 2        # I021/230  Roll Angle
        elif frn == 9:                  # I021/145  Barometric FL (1/4 FL × 100 ft)
            if pos + 2 > len(data): return track, len(data)
            track["alt_baro_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 10: pos += 2        # I021/150  Air Speed
        elif frn == 11: pos += 2        # I021/151  True Airspeed
        elif frn == 12: pos += 2        # I021/152  Magnetic Heading
        elif frn == 13: pos += 2        # I021/155  Barometric Vertical Rate
        elif frn == 14: pos += 2        # I021/157  Geometric Vertical Rate
        elif frn == 15:                 # I021/160  Ground Vector (GS 2B + TA 2B)
            if pos + 4 > len(data): return track, len(data)
            gs_raw = _u16(data[pos:pos + 2]) & 0x7FFF
            ta_raw = _u16(data[pos + 2:pos + 4])
            track["speed_ms"]    = round(gs_raw * (3600.0 / 16384.0) * 0.514444, 2)
            track["heading_deg"] = round(ta_raw * 360.0 / 65536.0, 2)
            pos += 4
        elif frn == 16: pos += 2        # I021/165  Track Angle Rate
        elif frn == 17:                 # I021/170  Target Identification (callsign)
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 18: pos += 1        # I021/095  Velocity Accuracy
        elif frn == 19: pos += 1        # I021/032  Time Accuracy
        elif frn == 20: pos = _skip_fx_field(data, pos)  # I021/200  Target Status (FX)
        elif frn == 21: pos += 1        # I021/020  Emitter Category
        elif frn == 22: break           # I021/220  Met Info (compound)
        elif frn == 23: pos += 2        # I021/146  Selected Altitude
        elif frn == 24: pos += 2        # I021/148  Final State Selected Alt
        elif frn == 25: break           # I021/110  Trajectory Intent (compound)
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
        elif frn == 11: pos += 2        # I062/200  Mode of Movement
        elif frn == 13:                 # I062/136  Measured Flight Level
            if pos + 2 > len(data): return track, len(data)
            track["measured_fl"] = _s16(data[pos:pos + 2]) * 0.25; pos += 2
        elif frn == 14:                 # I062/130  Calculated Altitude
            if pos + 2 > len(data): return track, len(data)
            track["calc_alt_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 18: pos += 1        # I062/300  Vehicle Fleet ID
        elif frn in (10, 12, 15, 16, 17, 19, 20, 21, 22, 23, 24, 25):
            break                       # compound / variable — unrecoverable
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
