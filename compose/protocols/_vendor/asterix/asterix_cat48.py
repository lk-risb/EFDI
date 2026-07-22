#!/usr/bin/env python3

"""EUROCONTROL ASTERIX CAT-048 Edition 1.32 radar target protocol."""



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
from protocols.asterix_cat48_pb2 import AsterixCat48Track
from protocols.protobuf_codec import publish_dual


ORG       = os.environ.get("PARTNER_NAMESPACE", "")

TOPIC_ROOT = topic_root()

HERE      = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)

_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

TOPIC_048    = "{}/air/asterix/cat48/unknown/aircraft/tracks/v1".format(TOPIC_ROOT)
RAW_INPUT_TOPIC = "{}/raw/asterix/cat48".format(TOPIC_ROOT)

CAT_048 = 0x30

_CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

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

def _skip_len_field(data: bytes, pos: int) -> int:
    """Skip an ASTERIX length-prefixed RE/SP item (length includes itself)."""
    if pos >= len(data):
        return len(data)
    length = data[pos]
    if length < 1 or pos + length > len(data):
        return len(data)
    return pos + length

def _s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

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
            typ = (b >> 5) & 0x07
            _TYP048 = ("no_det","ssr_mlat","am","psr","psr_ssr","psr_ssr_am","all_no_det","all")
            if typ: track["antenna_type"] = _TYP048[typ]
            if b & 0x10: track["simulated"]       = True   # SIM
            if b & 0x08: track["rdp_chain"]       = True   # RDP
            if b & 0x04: track["spi"]             = True
            if b & 0x02: track["surface_vehicle"] = True   # RAB
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["test_target"]    = True   # TST
                if b & 0x40: track["extended_range"] = True   # ERR
                if b & 0x20: track["x_pulse"]        = True   # XPP
                if b & 0x10: track["mil_emergency"]  = True   # ME
                if b & 0x08: track["mil_ident"]      = True   # MI
                foe = (b >> 1) & 0x03
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
                if psf & 0x80:                          # SRL: SSR plot runlength
                    if pos < len(data):
                        track["ssr_runlength_deg"] = round(data[pos] * 360.0 / 8192.0, 3)
                    pos += 1
                if psf & 0x40:                          # SRR: SSR replies received
                    if pos < len(data):
                        track["ssr_reply_count"] = data[pos]
                    pos += 1
                if psf & 0x20:                          # SAM: SSR amplitude (dBm)
                    if pos < len(data):
                        track["ssr_amplitude_dbm"] = struct.unpack_from("b", data, pos)[0]
                    pos += 1
                if psf & 0x10:                          # PRL: PSR plot runlength
                    if pos < len(data):
                        track["psr_runlength_deg"] = round(data[pos] * 360.0 / 8192.0, 3)
                    pos += 1
                if psf & 0x08:                          # PAM: PSR amplitude (dBm)
                    if pos < len(data):
                        track["psr_amplitude_dbm"] = struct.unpack_from("b", data, pos)[0]
                    pos += 1
                if psf & 0x04:                          # RPD: PSR-SSR range diff (s8, 1/256 NM)
                    if pos < len(data):
                        track["psr_ssr_range_diff_nm"] = round(struct.unpack_from("b", data, pos)[0] / 256.0, 4)
                    pos += 1
                if psf & 0x02:                          # APD: PSR-SSR azimuth diff (s8, 360/16384°)
                    if pos < len(data):
                        track["psr_ssr_az_diff_deg"] = round(struct.unpack_from("b", data, pos)[0] * 360.0 / 16384.0, 4)
                    pos += 1
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
                if   bds1 == 3 and bds2 == 0: track.update(_decode_bds30(mb))
                elif bds1 == 4 and bds2 == 0: track.update(_decode_bds40(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_decode_bds50(mb))
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
            track["speed_ms"]    = round(spd_raw * 1852.0 / 16384.0, 2)   # LSB = 2^-14 NM/s
            track["heading_deg"] = round(hdg_raw * 360.0 / 65536.0, 2)
            pos += 4
        elif frn == 13:                 # I048/170  Track Status (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["track_tentative"] = True
            rad = (b >> 5) & 0x03
            track["track_sensor"] = ("combined", "psr", "ssr_mode_s", "invalid")[rad]
            if b & 0x10: track["track_doubtful"]  = True
            if b & 0x08: track["track_manoeuvre"] = True
            cdm = (b >> 1) & 0x03
            if   cdm == 1: track["vertical_trend"] = "climbing"
            elif cdm == 2: track["vertical_trend"] = "descending"
            elif cdm == 3: track["vertical_trend"] = "unknown"
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["track_end"]   = True
                if b & 0x40: track["track_ghost"]  = True
                track["track_suppression"] = (b >> 3) & 0x07
                if b & 0x04: track["slant_range_correction"] = True
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 14:                 # I048/210  Track Quality (4 bytes)
            if pos + 4 > len(data): return track, len(data)
            track["track_sigma_x_nm"] = round(data[pos]     / 128.0, 4)
            track["track_sigma_y_nm"] = round(data[pos + 1] / 128.0, 4)
            track["track_sigma_v_kt"] = round(data[pos + 2] * (2 ** -14) * 3600.0, 2)
            track["track_sigma_heading_deg"] = round(data[pos + 3] * 360.0 / 4096.0, 2)
            pos += 4
        elif frn == 15:                 # I048/030  Warning/Error Conditions (FX repeating type codes)
            # Each octet: bits 7-1 = condition code (1-127), bit 0 = FX
            _WE048 = {1:"spi",2:"pai",3:"stc",4:"apw",5:"msaw",
                      6:"apw2",7:"cld",8:"track_doubtful"}
            codes = []
            while True:
                if pos >= len(data): break
                b = data[pos]; pos += 1
                code = (b >> 1) & 0x7F
                codes.append(code)
                _f = _WE048.get(code)
                if _f: track[_f] = True
                if not (b & 0x01): break
            if codes: track["we_conditions"] = codes
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
            com  = (b0 >> 5) & 0x07
            stat = (b0 >> 2) & 0x07
            track["com_capability"] = com
            _STAT230 = (
                "no_alert_no_spi_airborne", "no_alert_no_spi_ground",
                "alert_no_spi_airborne", "alert_no_spi_ground",
                "alert_spi", "no_alert_spi", "unassigned", "unknown",
            )
            track["transponder_status"] = _STAT230[stat]
            if stat in (1, 3): track["on_ground"] = True
            if stat in (2, 3, 4): track["alert"] = True
            if stat in (4, 5): track["spi"] = True
            track["interrogator_code_capability"] = "II" if (b0 & 0x02) else "SI"
            if b1 & 0x80: track["mssc"]          = True
            if b1 & 0x40: track["altitude_25ft"] = True
            if b1 & 0x20: track["aic"]           = True
            track["bds10_b1a"] = bool(b1 & 0x10)
            track["bds10_b1b"] = b1 & 0x0F
            pos += 2
        elif frn == 21:                 # I048/260  ACAS RA (7 bytes = BDS 3,0)
            if pos + 7 > len(data): return track, len(data)
            track.update(_decode_bds30(data[pos:pos + 7]))
            pos += 7
        elif frn == 22:                 # I048/055  Mode-1
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["mode1"] = "{:02o}".format(b & 0x1F)
            if b & 0x80: track["mode1_invalid"]  = True
            if b & 0x40: track["mode1_garbled"]  = True
            if b & 0x20: track["mode1_smoothed"] = True
        elif frn == 23:                 # I048/050  Mode-2 Code (2 bytes, lower 12 bits)
            if pos + 2 > len(data): return track, len(data)
            w = _u16(data[pos:pos + 2]); pos += 2
            track["mode2"] = "{:04o}".format(w & 0x0FFF)
            if w & 0x8000: track["mode2_invalid"]  = True
            if w & 0x4000: track["mode2_garbled"]  = True
            if w & 0x2000: track["mode2_smoothed"] = True
        elif frn == 24:                 # I048/065  Mode-1 confidence/quality
            if pos >= len(data): return track, len(data)
            track["mode1_quality_mask"] = data[pos]; pos += 1
        elif frn == 25:                 # I048/060  Mode-2 confidence/quality
            if pos + 2 > len(data): return track, len(data)
            track["mode2_quality_mask"] = _u16(data[pos:pos + 2]); pos += 2
        elif frn == 26:                 # I048/RE  Reserved Expansion Field
            pos = _skip_len_field(data, pos)
        elif frn == 27:                 # I048/SP  Special Purpose Field
            pos = _skip_len_field(data, pos)
        else: break

    if "icao24" not in track:
        sac = track.get("sac", 0); sic = track.get("sic", 0)
        track["radar_id"] = "CAT48-{:03d}-{:03d}-{:04d}".format(
            sac, sic, track.get("track_num", 0))
    return track, pos

def _pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, TOPIC_048, track, AsterixCat48Track, zenoh)
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

def _make_cat048_handler(pub, site):
    # site = [lat, lon] — shared mutable reference; updated live from CAT-34 I034/120
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat048_record(data, pos, site[0], site[1])
            if len(track) > 2:
                if verbose and "lat_deg" not in track:
                    ident = track.get("icao24") or track.get("radar_id") or "PSR"
                    print("cat48 {} no-position (awaiting I034/120 or set --radar-lat/--radar-lon)".format(
                        ident), flush=True)
                _pub(pub, track, "cat48", verbose)
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

def _process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _process_stream(iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
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



def main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-048 Ed.1.32 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT48_PORT", "50048") or 50048))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT48_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT48_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT48_INPUT_TOPIC", RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_env_float("CAT48_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_env_float("CAT48_RADAR_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT48_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    if None in site: print("INFO: set CAT48_RADAR_LAT/LON for local polar positions", flush=True)
    session = zenoh.open(make_config()); publisher = session.declare_publisher(TOPIC_048)
    handler = _make_cat048_handler(publisher, site)
    try:
        if args.zenoh_raw: _run_zenoh_raw(session, args.input_topic, CAT_048, handler, args.verbose)
        else: _run_inbound(args.port, args.tcp, "CAT-48 Ed.1.32", {CAT_048: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: publisher.undeclare(); session.close()



if __name__ == "__main__":

    main()
