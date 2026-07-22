#!/usr/bin/env python3

"""Legacy ASTERIX CAT-062 compatibility protocol; requires the producer ICD."""



import argparse

import json

import math

import os

import socket

import struct

import threading

import time

import zenoh
from zenoh_auth import apply_zenoh_auth

from namespace_prefix import topic_root
from protocols.asterix_cat62_pb2 import Cat62Track
from protocols.protobuf_codec import publish_dual


ORG       = os.environ.get("PARTNER_NAMESPACE", "")

TOPIC_ROOT = topic_root()

HERE      = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)

_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

TOPIC_062    = "{}/air/asterix/cat62/unknown/aircraft/tracks/v1".format(TOPIC_ROOT)
RAW_INPUT_TOPIC = "{}/raw/asterix/cat62".format(TOPIC_ROOT)

CAT_062 = 0x3E

RECONNECT_DELAY_S = 5.0
ZENOH_RETRY_S = 5.0

_CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_EMERGENCY_CODES = {
    0: None,
    1: "GENERAL EMERGENCY",
    2: "LIFEGUARD/MEDICAL",
    3: "MIN FUEL",
    4: "NO COMMS",
    5: "UNLAWFUL INTERFERENCE",
    6: "DOWNED AIRCRAFT",
}

_WTC_MAP = {76: "L", 77: "M", 72: "H", 74: "J"}

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
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
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

def parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _decode_bds50(mb: bytes) -> dict:
    """BDS 5,0 Track and Turn Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["roll_deg"]         = round(_sgn(2, 11) * 45.0 / 256.0, 1)
    if _bit(12): out["true_track_deg"]   = round(_uns(13, 22) * 360.0 / 1024.0, 1)
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
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
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

def _decode_bds40(mb: bytes) -> dict:
    """BDS 4,0 Selected Vertical Intention."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    out = {}
    if _bit(1):  out["sel_alt_mcp_ft"]  = _uns(2, 13) * 16
    if _bit(14): out["sel_alt_fms_ft"]  = _uns(15, 26) * 16
    if _bit(27): out["baro_setting_mb"] = round(_uns(28, 39) * 0.1 + 800.0, 1)
    if _bit(40): out["vnav_active"]     = True
    if _bit(41): out["alt_hold"]        = True
    if _bit(42): out["approach_mode"]   = True
    return out

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
        else:  out["ias_kt"] = round(val * 3600.0 / 16384.0, 1)
        pos += 2
    # Sub 06: TAS (2 bytes, u16, 1 kt/LSB)
    if psf & 0x04:
        if pos + 2 > len(data): return out, pos
        w = _u16(data[pos:pos+2]) & 0x7FFF
        if w: out["tas_kt"] = w
        pos += 2
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
            if   b1 == 3 and b2 == 0: out.update(_decode_bds30(mb))
            elif b1 == 4 and b2 == 0: out.update(_decode_bds40(mb))
            elif b1 == 5 and b2 == 0: out.update(_decode_bds50(mb))
            elif b1 == 6 and b2 == 0: out.update(_decode_bds60(mb))
    # Consume any further FX extension bytes
    while psf2 & 0x01:
        if pos >= len(data): break
        psf2 = data[pos]; pos += 1
    return out, pos

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
    # FCT: Flight Category (1 byte) — GAT/OAT, flight rules, RVSM status
    if psf & 0x20:
        if pos + 1 > len(data): return out, pos
        fc   = data[pos]; pos += 1
        gat  = (fc >> 6) & 0x03
        fr   = (fc >> 4) & 0x03
        rvsm = (fc >> 2) & 0x03
        if gat:  out["flight_gat"]   = ("", "GAT", "OAT", "GAT+OAT")[gat]
        if fr:   out["flight_rules"] = ("", "IFR", "VFR", "IFR+VFR")[fr]
        if rvsm == 1: out["rvsm"] = "approved"
        elif rvsm == 2: out["rvsm"] = "exempt"
        elif rvsm == 3: out["rvsm"] = "not_approved"
        if fc & 0x02: out["high_priority"] = True
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
    # CFL: Cleared Flight Level (2 bytes, s16, 0.25 FL)
    if psf2 & 0x80:
        if pos + 2 > len(data): return out, pos
        out["cleared_fl"] = round(_s16(data[pos:pos+2]) * 0.25, 0); pos += 2
    # CTL: Current Cleared Flight Level (2 bytes, u16, 0.25 FL)
    if psf2 & 0x40:
        if pos + 2 > len(data): return out, pos
        out["current_fl"] = round(_u16(data[pos:pos+2]) * 0.25, 0); pos += 2
    # TOD: Time of Departure/Arrival — REP × 4 bytes (type 1B + time 3B), skip
    if psf2 & 0x20:
        if pos >= len(data): return out, pos
        rep = data[pos]; pos += 1
        pos += rep * 4
    # AST: Aircraft Stand (6 bytes ASCII)
    if psf2 & 0x10:
        if pos + 6 > len(data): return out, pos
        out["aircraft_stand"] = data[pos:pos+6].decode("ascii", errors="replace").strip("\x00 ")
        pos += 6
    # STS: Stand Status flags (1 byte)
    if psf2 & 0x08:
        if pos + 1 > len(data): return out, pos
        sts = data[pos]; pos += 1
        if sts & 0x80: out["stand_occupied"] = True
        if sts & 0x40: out["stand_docking"]  = True
    # STD: Standard Instrument Departure (5 bytes ASCII)
    if psf2 & 0x04:
        if pos + 5 > len(data): return out, pos
        out["sid"] = data[pos:pos+5].decode("ascii", errors="replace").strip("\x00 ")
        pos += 5
    # STA: Standard Instrument Arrival (5 bytes ASCII)
    if psf2 & 0x02:
        if pos + 5 > len(data): return out, pos
        out["star"] = data[pos:pos+5].decode("ascii", errors="replace").strip("\x00 ")
        pos += 5
    # Consume any further FX extension bytes
    while psf2 & 0x01:
        if pos >= len(data): break
        psf2 = data[pos]; pos += 1
    return out, pos

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
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["track_monosensor"] = True   # MON: 1=monosensor track
            if b & 0x40: track["spi"]              = True
            src = (b >> 3) & 0x07
            _SRC = ("", "GPS", "3D_radar", "triangulation", "pressure_alt",
                    "velocity_integration", "INS", "3D_radar_corrected")
            if src: track["height_src"] = _SRC[src]
            if b & 0x04: track["track_tentative"]  = True   # CNF: 1=tentative
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["simulated"]      = True   # SIM
                if b & 0x40: track["track_end"]      = True   # TSE: last plot of track
                if b & 0x20: track["track_begin"]    = True   # TSB: first detection
                if b & 0x10: track["fp_correlated"]  = True   # FRIFPSI
                if b & 0x08: track["mil_emergency"]  = True   # ME
                if b & 0x04: track["mil_ident"]      = True   # MI
                if b & 0x02: track["amalgamated"]    = True   # AMA
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
                    if b & 0x80: track["track_coasting"] = True   # STP
        elif frn == 10:                 # I062/290  System Track Update Ages (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            age_map = [(0x80,"psr"),(0x40,"ssr"),(0x20,"mds"),(0x10,"ads"),
                       (0x08,"es"),(0x04,"vdl"),(0x02,"uat")]
            for mask, label in age_map:
                if psf & mask:
                    if pos + 1 > len(data): return track, len(data)
                    track["track_age_{}_s".format(label)] = round(data[pos] * 0.25, 2); pos += 1
            if psf & 0x01:              # FX: LOP, MLT ages (1 byte each, 0.25s/LSB)
                if pos >= len(data): return track, len(data)
                psf2 = data[pos]; pos += 1
                if psf2 & 0x80:
                    if pos + 1 > len(data): return track, len(data)
                    track["track_age_lop_s"] = round(data[pos] * 0.25, 2); pos += 1
                if psf2 & 0x40:
                    if pos + 1 > len(data): return track, len(data)
                    track["track_age_mlt_s"] = round(data[pos] * 0.25, 2); pos += 1
                while psf2 & 0x01:
                    if pos >= len(data): break
                    psf2 = data[pos]; pos += 1
        elif frn == 11:                 # I062/200  Mode of Movement (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            b1 = data[pos]; b2 = data[pos + 1]; pos += 2
            _MOV = ("constant", "right", "left", "undetermined")
            _LON = ("constant", "increasing", "decreasing", "undetermined")
            _VRT = ("level", "climb", "descend", "undetermined")
            trans = (b1 >> 6) & 0x03
            long_ = (b1 >> 4) & 0x03
            vert  = (b1 >> 2) & 0x03
            if trans: track["lateral_trend"]  = _MOV[trans]
            if long_: track["speed_trend"]    = _LON[long_]
            if vert:  track["vertical_trend"] = _VRT[vert]
            if b1 & 0x02: track["alt_discrepancy"] = True   # ADF
        elif frn == 13:                 # I062/136  Measured Flight Level
            if pos + 2 > len(data): return track, len(data)
            track["measured_alt_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 14:                 # I062/130  Calculated Altitude
            if pos + 2 > len(data): return track, len(data)
            track["calc_alt_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 18:                 # I062/300  Vehicle Fleet ID
            if pos >= len(data): return track, len(data)
            track["fleet_id"] = data[pos]; pos += 1
        elif frn == 12:                 # I062/295  Track Data Ages (compound, 0.25s/LSB each)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            _AGE295 = ["psr", "ssr", "mds", "ads_b", "es", "vdl4", "uat"]
            for label, mask in zip(_AGE295, (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02)):
                if psf & mask:
                    if pos + 1 > len(data): return track, len(data)
                    track["data_age_{}_s".format(label)] = round(data[pos] * 0.25, 2); pos += 1
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
            if psf & 0x40:              # measured slant range (1/256 NM) + azimuth (360/65536°)
                if pos + 4 > len(data): return track, len(data)
                track["meas_range_nm"] = round(_u16(data[pos:pos+2]) / 256.0, 3)
                track["meas_az_deg"]   = round(_u16(data[pos+2:pos+4]) * 360.0 / 65536.0, 3)
                pos += 4
            if psf & 0x20:              # measured Mode-C height (Gillham)
                if pos + 2 > len(data): return track, len(data)
                alt = _gillham_to_ft(_u16(data[pos:pos+2]))
                if alt is not None: track["meas_alt_ft"] = alt
                pos += 2
            if psf & 0x10:              # MDC: Measured Mode C code + confidence (4 bytes)
                if pos + 4 > len(data): return track, len(data)
                alt = _gillham_to_ft(_u16(data[pos:pos+2]))
                if alt is not None: track.setdefault("meas_mode_c_ft", alt)
                pos += 4
            if psf & 0x08:              # MDA: Measured Mode 3/A code (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track.setdefault("squawk", "{:04o}".format(_u16(data[pos:pos+2]) & 0x0FFF))
                pos += 2
            if psf & 0x04: pos += 4    # MD5: Mode 5 code + confidence (4 bytes, skip)
            if psf & 0x02:              # MD1: Mode 1 code + quality (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track.setdefault("mode1", "{:02o}".format(_u16(data[pos:pos+2]) & 0x3F))
                pos += 2
            if measured_by: track["measured_by"] = measured_by
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 19:                 # I062/110  Mode 5 and Mode 1 Data (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80:              # Mode 5 Summary (3 bytes: flags + PIN hi + PIN lo)
                if pos + 3 > len(data): return track, len(data)
                b = data[pos]
                if b & 0x80: track["mode5_active"] = True
                if b & 0x40: track["mode5_iff"]    = True
                if b & 0x20: track["mode5_data"]   = True
                pos += 3
            if psf & 0x40: pos += 2    # National origin (2 bytes)
            if psf & 0x20: pos += 8    # Reported position (lat/lon s32 each)
            if psf & 0x10:              # Mode 1 code (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track["mode1"] = "{:02o}".format(_u16(data[pos:pos+2]) & 0x3F); pos += 2
            if psf & 0x08:              # Mode 2 code (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track["mode2"] = "{:04o}".format(_u16(data[pos:pos+2]) & 0x0FFF); pos += 2
            if psf & 0x04:              # Mode 3/A (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track.setdefault("squawk", "{:04o}".format(_u16(data[pos:pos+2]) & 0x0FFF)); pos += 2
            if psf & 0x02:              # Mode C height (2 bytes, Gillham)
                if pos + 2 > len(data): return track, len(data)
                alt = _gillham_to_ft(_u16(data[pos:pos+2]))
                if alt is not None: track.setdefault("mode_c_alt_ft", alt)
                pos += 2
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
                for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
                    if psf & mask: pos += 2
        elif frn == 20:                 # I062/120  Mode 5 Track Ages (compound, 1 byte each)
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
        elif frn == 21:                 # I062/510  Composed Track Number (REP + N×4)
            if pos >= len(data): return track, len(data)
            n = data[pos]; pos += 1 + n * 4
        elif frn in (24, 25):
            break                       # I062/SP / I062/RE — vendor-specific, unrecoverable
        else:
            break
    return track, pos

def _pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(
        pub,
        TOPIC_062,
        track,
        Cat62Track,
        zenoh,
        wrapper_field="normalized",
    )
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

def _make_cat062_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat62_record(data, pos)
            if len(track) > 2:
                _pub(pub, track, "cat62", verbose)
    return _h

def _process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _raw_frame_payload(bytes(sample.payload), category)
        except ValueError as exc:
            print("CAT-{} ignored invalid raw Zenoh frame: {}".format(category, exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = session.declare_subscriber(input_topic, _on_sample)
    print("CAT-{} raw Zenoh input: {}".format(category, input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()

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
        except (EOFError, ValueError, ConnectionRefusedError, OSError) as exc:
            print("CAT-62 error: {} — reconnecting in {}s".format(
                exc, RECONNECT_DELAY_S), flush=True)
            if sock:
                try: sock.close()
                except Exception: pass
        time.sleep(RECONNECT_DELAY_S)



def main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-062 legacy profile -> Zenoh")
    parser.add_argument("--host", default=os.environ.get("CAT62_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT62_PORT", "50062") or 50062))
    parser.add_argument("--udp", action="store_true", default=os.environ.get("CAT62_UDP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT62_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT62_INPUT_TOPIC", RAW_INPUT_TOPIC))
    parser.add_argument("--topic", default=TOPIC_062)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.host and not args.udp: parser.error("--host is required unless --udp or --zenoh-raw is selected")
    print("WARNING: CAT-62 uses a legacy compatibility UAP; Edition 1.21 is not supported", flush=True)
    while True:
        try:
            session = zenoh.open(make_config())
            break
        except zenoh.ZError as exc:
            print("CAT-62 Zenoh connect failed: {} — retry in {}s".format(exc, ZENOH_RETRY_S), flush=True)
            time.sleep(ZENOH_RETRY_S)
    publisher = session.declare_publisher(args.topic)
    handler = _make_cat062_handler(publisher)
    try:
        if args.zenoh_raw: _run_zenoh_raw(session, args.input_topic, CAT_062, handler, args.verbose)
        else: _run_cat62(args.host, args.port, args.udp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: publisher.undeclare(); session.close()



if __name__ == "__main__":

    main()
