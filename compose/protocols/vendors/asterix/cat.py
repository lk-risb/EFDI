#!/usr/bin/env python3
"""EUROCONTROL ASTERIX CAT-010/020/021/034/048/062 protocol implementation.

The module keeps each edition-scoped decoder isolated while exposing one source
entrypoint. Run without ``--category`` to launch the configured CAT processes,
or pass ``--category NN`` for one translator process.
"""

from __future__ import annotations

import os
import sys

from protocols.vendor_bundle import run_bundle
from protocols.vendors.asterix.cat_pb2 import (
    AsterixCat10SensorStatus,
    AsterixCat10Track,
    AsterixCat20Track,
    AsterixCat21Track,
    AsterixCat34Status,
    AsterixCat48Track,
    Cat62Track,
)

# ==========================================================================
# CAT-010
# ==========================================================================

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
from protocols.protobuf_codec import publish_dual


_cat10_ORG       = os.environ.get("PARTNER_NAMESPACE", "")

_cat10_TOPIC_ROOT = topic_root()

_cat10_HERE      = os.path.dirname(os.path.abspath(__file__))

_cat10__CERT_DIR = os.environ.get("EFDI_CERT_DIR", _cat10_HERE)

_cat10__ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

_cat10_TOPIC_010_AIR = "{}/air/asterix/cat10/civ/aircraft/tracks/v1".format(_cat10_TOPIC_ROOT)

_cat10_TOPIC_010_GROUND = "{}/land/asterix/cat10/unknown/vehicle/tracks/v1".format(_cat10_TOPIC_ROOT)

_cat10_TOPIC_010_SENSOR = "{}/land/asterix/cat10/neutral/radar/status/v1".format(_cat10_TOPIC_ROOT)
_cat10_RAW_INPUT_TOPIC = "{}/raw/asterix/cat10".format(_cat10_TOPIC_ROOT)

_cat10_CAT_010 = 0x0A

_cat10__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat10__CAT010_MESSAGE_TYPES = {
    1: "target_report",
    2: "start_update_cycle",
    3: "periodic_status",
    4: "event_status",
}

_cat10__CAT010_SENSOR_TYPES = {
    0: "ssr_mlat", 1: "mode_s_mlat", 2: "adsb", 3: "psr",
    4: "magnetic_loop", 5: "hf_mlat", 6: "undefined", 7: "other",
}

_cat10__CAT010_TARGET_TYPES = {0: "undetermined", 1: "aircraft", 2: "ground_vehicle", 3: "helicopter"}

_cat10__CAT010_FLEETS = {
    0: "unknown", 1: "atc_maintenance", 2: "airport_maintenance", 3: "fire",
    4: "bird_scarer", 5: "snow_plough", 6: "runway_sweeper", 7: "emergency",
    8: "police", 9: "bus", 10: "tug", 11: "grass_cutter", 12: "fuel",
    13: "baggage", 14: "catering", 15: "aircraft_maintenance", 16: "follow_me",
}

def _cat10__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0

def _cat10__netbird_ip() -> str:
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

def _cat10_make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_cat10__ENDPOINT]))
    apply_zenoh_auth(conf)
    if _cat10__ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_cat10__CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_cat10__CERT_DIR, _cat10_ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_cat10__CERT_DIR, _cat10_ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf

def _cat10__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat10_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat10__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat10__recv_exact(sock, length - 3)
        yield cat, data

def _cat10_iter_frames_udp(sock: socket.socket):
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

def _cat10_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat10__skip_len_field(data: bytes, pos: int) -> int:
    """Skip an ASTERIX length-prefixed RE/SP item (length includes itself)."""
    if pos >= len(data):
        return len(data)
    length = data[pos]
    if length < 1 or pos + length > len(data):
        return len(data)
    return pos + length

def _cat10__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat10__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat10__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat10__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat10__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat10__decode_bds50(mb: bytes) -> dict:
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

def _cat10__decode_bds60(mb: bytes) -> dict:
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

def _cat10__decode_bds40(mb: bytes) -> dict:
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

def _cat10__decode_bds30(mb: bytes) -> dict:
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

def _cat10__polar_to_wgs84(radar_lat: float, radar_lon: float,
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

def _cat10__cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_m: float, y_m: float):
    """Local CAT-010 X=east/Y=north metres to WGS-84 for short airport ranges."""
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon

def _cat10__signed_bits(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value

def _cat10_decode_cat010_record(data: bytes, pos: int, site_lat=None, site_lon=None):
    """Decode exactly the public EUROCONTROL CAT-010 Edition 1.1 UAP."""
    fspec, pos = _cat10_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-10 Ed.1.1"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I010/010 SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I010/000 Message Type
            if pos >= len(data): return track, len(data)
            track["msg_type"] = _cat10__CAT010_MESSAGE_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I010/020 Target Report Descriptor
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["sensor_type"] = _cat10__CAT010_SENSOR_TYPES[(b >> 5) & 0x07]
            if b & 0x10: track["differential_correction"] = True
            track["channel"] = 2 if b & 0x08 else 1
            if b & 0x04: track["transponder_ground_bit"] = True
            if b & 0x02: track["corrupted_reply"] = True
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["simulated"] = True
                if b & 0x40: track["test_target"] = True
                if b & 0x20: track["field_monitor"] = True
                loop = (b >> 3) & 0x03
                if loop: track["loop_status"] = ("", "start", "finish", "reserved")[loop]
                target_type = (b >> 1) & 0x03
                track["target_type"] = _cat10__CAT010_TARGET_TYPES[target_type]
                track["on_ground"] = target_type == 2
                while b & 0x01:
                    if pos >= len(data): return track, len(data)
                    b = data[pos]; pos += 1
                    if b & 0x80: track["spi"] = True
        elif frn == 3:                  # I010/140 Time of Day
            if pos + 3 > len(data): return track, len(data)
            track["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 4:                  # I010/041 WGS-84 Position
            if pos + 8 > len(data): return track, len(data)
            track["lat_deg"] = round(_cat10__s32(data[pos:pos + 4]) * 180.0 / 2**31, 7)
            track["lon_deg"] = round(_cat10__s32(data[pos + 4:pos + 8]) * 180.0 / 2**31, 7); pos += 8
        elif frn == 5:                  # I010/040 Polar Position
            if pos + 4 > len(data): return track, len(data)
            range_m = _cat10__u16(data[pos:pos + 2]); azimuth = _cat10__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0
            track["range_m"] = range_m; track["azimuth_deg"] = round(azimuth, 3); pos += 4
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat10__polar_to_wgs84(site_lat, site_lon, range_m / 1852.0, azimuth)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 6:                  # I010/042 Cartesian Position
            if pos + 4 > len(data): return track, len(data)
            x_m, y_m = _cat10__s16(data[pos:pos + 2]), _cat10__s16(data[pos + 2:pos + 4]); pos += 4
            track["cart_x_m"], track["cart_y_m"] = x_m, y_m
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat10__cartesian_to_wgs84(site_lat, site_lon, x_m, y_m)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 7:                  # I010/200 Polar Velocity
            if pos + 4 > len(data): return track, len(data)
            track["speed_ms"] = round(_cat10__u16(data[pos:pos + 2]) * 1852.0 / 16384.0, 2)
            track["heading_deg"] = round(_cat10__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0, 2); pos += 4
        elif frn == 8:                  # I010/202 Cartesian Velocity
            if pos + 4 > len(data): return track, len(data)
            vx = _cat10__s16(data[pos:pos + 2]) * 0.25; vy = _cat10__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
            track["velocity_east_ms"] = round(vx, 2); track["velocity_north_ms"] = round(vy, 2)
            track.setdefault("speed_ms", round(math.hypot(vx, vy), 2))
            if vx or vy: track.setdefault("heading_deg", round((math.degrees(math.atan2(vx, vy)) + 360) % 360, 2))
        elif frn == 9:                  # I010/161 Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat10__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 10:                 # I010/170 Track Status
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["confirmed"] = not bool(b & 0x80)
            if b & 0x40: track["_delete"] = True
            coast = (b >> 4) & 0x03
            if coast: track["coasting"] = coast
            if b & 0x08: track["manoeuvring"] = True
            if b & 0x02: track["smoothed_position"] = True
            extent = 0
            while b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1:
                    movement = (b >> 6) & 0x03
                    if movement: track["movement"] = ("", "taking_off", "landing", "other")[movement]
                elif extent == 2 and b & 0x80:
                    track["ghost_track"] = True
        elif frn == 11:                 # I010/060 Mode 3/A
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_cat10__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 12:                 # I010/220 Target Address
            if pos + 3 > len(data): return track, len(data)
            track["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif frn == 13:                 # I010/245 Target Identification
            if pos + 7 > len(data): return track, len(data)
            callsign = _cat10__decode_callsign(data[pos + 1:pos + 7]); pos += 7
            if callsign: track["callsign"] = callsign
        elif frn == 14:                 # I010/250 Mode-S MB Data
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): return track, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = ((bds >> 4) & 0x0F, bds & 0x0F)
                if code == (3, 0): track.update(_cat10__decode_bds30(mb))
                elif code == (4, 0): track.update(_cat10__decode_bds40(mb))
                elif code == (5, 0): track.update(_cat10__decode_bds50(mb))
                elif code == (6, 0): track.update(_cat10__decode_bds60(mb))
        elif frn == 15:                 # I010/300 Vehicle Fleet ID
            if pos >= len(data): return track, len(data)
            track["vehicle_fleet"] = _cat10__CAT010_FLEETS.get(data[pos], "fleet_{}".format(data[pos])); pos += 1
            track.setdefault("target_type", "ground_vehicle"); track["on_ground"] = True
        elif frn == 16:                 # I010/090 Flight Level
            if pos + 2 > len(data): return track, len(data)
            raw = _cat10__u16(data[pos:pos + 2]); pos += 2
            fl = _cat10__signed_bits(raw & 0x3FFF, 14) * 0.25
            track["flight_level"] = round(fl, 2); track["baro_alt_m"] = round(fl * 100 * 0.3048, 2)
        elif frn == 17:                 # I010/091 Measured Height
            if pos + 2 > len(data): return track, len(data)
            feet = _cat10__s16(data[pos:pos + 2]) * 6.25; pos += 2
            track["measured_height_ft"] = round(feet, 2); track["geo_alt_m"] = round(feet * 0.3048, 2)
        elif frn == 18:                 # I010/270 Target Size / Orientation
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1; track["target_length_m"] = b >> 1
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1; track["orientation_deg"] = round((b >> 1) * 360.0 / 128.0, 2)
                if b & 0x01:
                    if pos >= len(data): return track, len(data)
                    b = data[pos]; pos += 1; track["target_width_m"] = b >> 1
                    while b & 0x01:
                        if pos >= len(data): return track, len(data)
                        b = data[pos]; pos += 1
        elif frn == 19:                 # I010/550 System Status
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["system_status"] = ("operational", "degraded", "nogo", "reserved")[(b >> 6) & 0x03]
            if b & 0x20: track["overload"] = True
            if b & 0x10: track["time_source_invalid"] = True
            if b & 0x08: track["diversity_degraded"] = True
            if b & 0x04: track["test_target_failure"] = True
        elif frn == 20:                 # I010/310 Pre-programmed Message
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["in_trouble"] = True
            track["preprogrammed_message"] = b & 0x7F
        elif frn == 21:                 # I010/500 Position Standard Deviation
            if pos + 4 > len(data): return track, len(data)
            track["sigma_x_m"] = data[pos] * 0.25; track["sigma_y_m"] = data[pos + 1] * 0.25
            track["sigma_xy_m2"] = _cat10__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
        elif frn == 22:                 # I010/280 Presence (REP x 2)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            if pos + rep * 2 > len(data): return track, len(data)
            pos += rep * 2
        elif frn == 23:                 # I010/131 Primary Plot Amplitude
            if pos >= len(data): return track, len(data)
            track["primary_amplitude"] = data[pos]; pos += 1
        elif frn == 24:                 # I010/210 Calculated Acceleration
            if pos + 2 > len(data): return track, len(data)
            track["accel_east_ms2"] = struct.unpack_from("b", data, pos)[0] * 0.25
            track["accel_north_ms2"] = struct.unpack_from("b", data, pos + 1)[0] * 0.25; pos += 2
        elif frn == 25:                 # Spare (no encoded item)
            continue
        elif frn in (26, 27):           # SP / RE
            pos = _cat10__skip_len_field(data, pos)
        else:
            return track, len(data)
    return track, pos

def _cat10__make_cat010_handler(session, site, site_name):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat10_decode_cat010_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if track.get("msg_type") == "target_report":
                if "lat_deg" not in track or "lon_deg" not in track:
                    if verbose:
                        print("cat10 target without map position; configure CAT10_SITE_LAT/LON for local coordinates", flush=True)
                    continue
                ground = track.get("target_type") == "ground_vehicle" or "vehicle_fleet" in track
                topic = _cat10_TOPIC_010_GROUND if ground else _cat10_TOPIC_010_AIR
                publish_dual(session, topic, track, AsterixCat10Track, zenoh)
                if verbose:
                    print("cat10 {} -> {}".format(track.get("track_num", "target"), topic), flush=True)
            elif track.get("msg_type") in ("start_update_cycle", "periodic_status", "event_status"):
                if site[0] is None or site[1] is None:
                    continue
                status = dict(track)
                status.update(
                    {
                        "sensor_id": "CAT10-{}-{}".format(track.get("sac", 0), track.get("sic", 0)),
                        "sensor_name": site_name or "Airport surface sensor",
                        "sensor_type": "airport_surface_surveillance",
                        "lat_deg": site[0],
                        "lon_deg": site[1],
                    }
                )
                publish_dual(
                    session,
                    _cat10_TOPIC_010_SENSOR,
                    status,
                    AsterixCat10SensorStatus,
                    zenoh,
                    wrapper_field="sensor",
                )
    return _h

def _cat10__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat10__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat10__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat10__raw_frame_payload(bytes(sample.payload), category)
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

def _cat10__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat10__process_stream(_cat10_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat10__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat10__netbird_ip()
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
                target=_cat10__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat10__process_stream(_cat10_iter_frames_udp(sock), handlers, verbose)



def _cat10_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-010 Ed.1.1 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT10_PORT", "50010") or 50010))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT10_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT10_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT10_INPUT_TOPIC", _cat10_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat10__env_float("CAT10_SITE_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat10__env_float("CAT10_SITE_LON"))
    parser.add_argument("--site-name", default=os.environ.get("CAT10_SITE_NAME", ""))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT10_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = zenoh.open(_cat10_make_config())
    try:
        print("Zenoh CAT-10 topics:", _cat10_TOPIC_010_AIR, _cat10_TOPIC_010_GROUND, _cat10_TOPIC_010_SENSOR, flush=True)
        handler = _cat10__make_cat010_handler(session, site, args.site_name)
        if args.zenoh_raw:
            _cat10__run_zenoh_raw(session, args.input_topic, _cat10_CAT_010, handler, args.verbose)
        else:
            _cat10__run_inbound(args.port, args.tcp, "CAT-10 Ed.1.1", {_cat10_CAT_010: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat010_record = _cat10_decode_cat010_record


# ==========================================================================
# CAT-020
# ==========================================================================

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
from protocols.protobuf_codec import publish_dual


_cat20_ORG       = os.environ.get("PARTNER_NAMESPACE", "")

_cat20_TOPIC_ROOT = topic_root()

_cat20_HERE      = os.path.dirname(os.path.abspath(__file__))

_cat20__CERT_DIR = os.environ.get("EFDI_CERT_DIR", _cat20_HERE)

_cat20__ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

_cat20_TOPIC_020    = "{}/air/asterix/cat20/civ/aircraft/tracks/v1".format(_cat20_TOPIC_ROOT)
_cat20_RAW_INPUT_TOPIC = "{}/raw/asterix/cat20".format(_cat20_TOPIC_ROOT)

_cat20_CAT_020 = 0x14

_cat20__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

def _cat20__netbird_ip() -> str:
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

def _cat20_make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_cat20__ENDPOINT]))
    apply_zenoh_auth(conf)
    if _cat20__ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_cat20__CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_cat20__CERT_DIR, _cat20_ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_cat20__CERT_DIR, _cat20_ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf

def _cat20__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat20_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat20__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat20__recv_exact(sock, length - 3)
        yield cat, data

def _cat20_iter_frames_udp(sock: socket.socket):
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

def _cat20_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat20__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat20__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat20__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat20__s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

def _cat20__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)

def _cat20__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat20__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat20__decode_bds50(mb: bytes) -> dict:
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

def _cat20__decode_bds60(mb: bytes) -> dict:
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

def _cat20__gillham_to_ft(code: int) -> int | None:
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

def _cat20__decode_bds40(mb: bytes) -> dict:
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

def _cat20__decode_bds30(mb: bytes) -> dict:
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

def _cat20_decode_cat020_record(data: bytes, pos: int):
    """Decode one EUROCONTROL CAT-020 Edition 1.11 MLAT record."""
    fspec, pos = _cat20_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-20 Ed.1.11"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I020/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I020/020 Target Report Descriptor
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            technologies = [name for mask, name in (
                (0x80, "non_mode_s_1090_mlat"), (0x40, "mode_s_1090_mlat"),
                (0x20, "hf_mlat"), (0x10, "vdl4_mlat"), (0x08, "uat_mlat"),
                (0x04, "dme_tacan_mlat"), (0x02, "other_mlat")) if b & mask]
            if technologies: track["mlat_technologies"] = technologies
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["field_monitor"] = True
                if b & 0x40: track["spi"] = True
                track["channel"] = 2 if b & 0x20 else 1
                if b & 0x10: track["on_ground"] = True
                if b & 0x08: track["corrupted_reply"] = True
                if b & 0x04: track["simulated"] = True
                if b & 0x02: track["test_target"] = True
                if b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
                    track["address_format"] = (
                        "icao24", "non_icao24", "non_adsb", "unavailable")[(b >> 6) & 0x03]
                    while b & 0x01:
                        if pos >= len(data): break
                        b = data[pos]; pos += 1
        elif frn == 2:                  # I020/140  Time of Day (1/128 s)
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I020/041  WGS-84 (4+4 bytes, 180/2^25)
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 25)
            track["lat_deg"] = round(_cat20__s32(data[pos:pos + 4]) * scale, 6)
            track["lon_deg"] = round(_cat20__s32(data[pos + 4:pos + 8]) * scale, 6)
            pos += 8
        elif frn == 4:                  # I020/042  Cartesian (x 3B + y 3B, 0.5 m)
            if pos + 6 > len(data): return track, len(data)
            track["x_m"] = round(_cat20__s24(data[pos:pos + 3]) * 0.5, 1)
            track["y_m"] = round(_cat20__s24(data[pos + 3:pos + 6]) * 0.5, 1)
            pos += 6
        elif frn == 5:                  # I020/161 Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat20__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 6:                  # I020/170 Track Status
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["confirmed"] = not bool(b & 0x80)
            if b & 0x40: track["_delete"] = True
            if b & 0x20: track["coasting"] = True
            trend = (b >> 3) & 0x03
            if trend: track["vertical_trend"] = ("", "climbing", "descending", "invalid")[trend]
            if b & 0x04: track["manoeuvring"] = True
            if b & 0x02: track["smoothed_position"] = True
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["ghost_track"] = True
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 7:                  # I020/070 Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            w = _cat20__u16(data[pos:pos + 2]); pos += 2
            track["squawk"] = "{:04o}".format(w & 0x0FFF)
            if w & 0x8000: track["squawk_invalid"] = True
            if w & 0x4000: track["squawk_garbled"] = True
            if w & 0x2000: track["squawk_from_track"] = True
        elif frn == 8:                  # I020/202 Cartesian Velocity
            if pos + 4 > len(data): return track, len(data)
            vx = _cat20__s16(data[pos:pos + 2]) * 0.25
            vy = _cat20__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
            track["velocity_east_ms"], track["velocity_north_ms"] = vx, vy
            track["speed_ms"] = round(math.hypot(vx, vy), 2)
            if vx or vy: track["heading_deg"] = round(math.degrees(math.atan2(vx, vy)) % 360, 2)
        elif frn == 9:                  # I020/090 Barometric FL
            if pos + 2 > len(data): return track, len(data)
            w = _cat20__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x3FFF
            if raw & 0x2000: raw -= 0x4000
            track["alt_baro_ft"] = raw * 25
            if w & 0x8000: track["alt_baro_invalid"] = True
            if w & 0x4000: track["alt_baro_garbled"] = True
        elif frn == 10:                 # I020/100 Mode-C + Confidence
            if pos + 4 > len(data): return track, len(data)
            alt = _cat20__gillham_to_ft(_cat20__u16(data[pos:pos + 2]))
            if alt is not None: track["mode_c_alt_ft"] = alt
            pos += 4
        elif frn == 11:                 # I020/220 ICAO 24-bit
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 12:                 # I020/245 Target ID
            if pos + 7 > len(data): return track, len(data)
            pos += 1
            track["callsign"] = _cat20__decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 13:                 # I020/110 Measured Height
            if pos + 2 > len(data): return track, len(data)
            track["measured_height_ft"] = _cat20__s16(data[pos:pos + 2]) * 6.25; pos += 2
        elif frn == 14:                 # I020/105 Geometric Height
            if pos + 2 > len(data): return track, len(data)
            track["alt_geom_ft"] = round(_cat20__s16(data[pos:pos + 2]) * 6.25); pos += 2
        elif frn == 15:                 # I020/210 Calculated Acceleration
            if pos + 2 > len(data): return track, len(data)
            track["accel_east_ms2"] = struct.unpack_from("b", data, pos)[0] * 0.25
            track["accel_north_ms2"] = struct.unpack_from("b", data, pos + 1)[0] * 0.25; pos += 2
        elif frn == 16:                 # I020/300 Vehicle Fleet ID
            if pos + 1 > len(data): return track, len(data)
            track["fleet_id"] = data[pos]; pos += 1
        elif frn == 17:                 # I020/310 Pre-programmed Message
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["in_trouble"] = True
            msg_type = b & 0x7F
            _MSG310 = (
                "",
                "towing_aircraft",
                "follow_me",
                "runway_check",
                "emergency_operation",
                "work_in_progress",
            )
            if msg_type:
                track["preprog_msg"] = (
                    _MSG310[msg_type]
                    if msg_type < len(_MSG310)
                    else "type_{}".format(msg_type)
                )
        elif frn == 18:                 # I020/500 Position Accuracy
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80:
                if pos + 6 > len(data): return track, len(data)
                track["dop_x"] = _cat20__u16(data[pos:pos + 2]) * 0.25
                track["dop_y"] = _cat20__u16(data[pos + 2:pos + 4]) * 0.25
                track["dop_xy"] = _cat20__s16(data[pos + 4:pos + 6]) * 0.25
                pos += 6
            if psf & 0x40:
                if pos + 6 > len(data): return track, len(data)
                track["pos_accuracy_x_m"] = _cat20__u16(data[pos:pos + 2]) * 0.25
                track["pos_accuracy_y_m"] = _cat20__u16(data[pos + 2:pos + 4]) * 0.25
                track["pos_correlation"] = _cat20__s16(data[pos + 4:pos + 6]) * 0.25
                pos += 6
            if psf & 0x20:
                if pos + 2 > len(data): return track, len(data)
                track["height_accuracy_m"] = _cat20__u16(data[pos:pos + 2]) * 0.5; pos += 2
        elif frn == 19:                 # I020/400 Contributing Devices
            if pos + 1 > len(data): return track, len(data)
            count = data[pos]; pos += 1
            if pos + count > len(data): return track, len(data)
            track["contributing_device_masks"] = ["{:08b}".format(v) for v in data[pos:pos + count]]
            pos += count
        elif frn == 20:                 # I020/250 BDS Register Data
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb   = bytes(data[pos:pos + 7])
                bds  = data[pos + 7]; pos += 8
                bds1 = (bds >> 4) & 0x0F; bds2 = bds & 0x0F
                if   bds1 == 3 and bds2 == 0: track.update(_cat20__decode_bds30(mb))
                elif bds1 == 4 and bds2 == 0: track.update(_cat20__decode_bds40(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_cat20__decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_cat20__decode_bds60(mb))
        elif frn == 21:                 # I020/230 Comms/ACAS capability
            if pos + 2 > len(data): return track, len(data)
            track["comms_acas_raw"] = data[pos:pos + 2].hex(); pos += 2
        elif frn == 22:                 # I020/260 ACAS RA
            if pos + 7 > len(data): return track, len(data)
            track.update(_cat20__decode_bds30(data[pos:pos + 7])); pos += 7
        elif frn == 23:                 # I020/030 Warning/Error Conditions
            codes = []
            while pos < len(data):
                b = data[pos]; pos += 1; codes.append(b >> 1)
                if not b & 1: break
            if codes: track["warning_error_codes"] = codes
        elif frn == 24:                 # I020/055 Mode-1
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["mode1"] = "{:02o}".format(b & 0x1F)
        elif frn == 25:                 # I020/050 Mode-2
            if pos + 2 > len(data): return track, len(data)
            w = _cat20__u16(data[pos:pos + 2]); pos += 2
            track["mode2"] = "{:04o}".format(w & 0x0FFF)
        elif frn in (26, 27):           # RE / SP
            pos = _cat20__skip_len_field(data, pos)
        else:
            break
    return track, pos

def _cat20__pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, _cat20_TOPIC_020, track, AsterixCat20Track, zenoh)
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

def _cat20__make_cat020_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = _cat20_decode_cat020_record(data, pos)
            if len(track) > 2:
                _cat20__pub(pub, track, "cat20", verbose)
    return _h

def _cat20__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat20__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat20__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat20__raw_frame_payload(bytes(sample.payload), category)
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

def _cat20__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat20__process_stream(_cat20_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat20__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat20__netbird_ip()
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
                target=_cat20__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat20__process_stream(_cat20_iter_frames_udp(sock), handlers, verbose)



def _cat20_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-020 Ed.1.11 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT20_PORT", "50020") or 50020))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT20_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT20_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT20_INPUT_TOPIC", _cat20_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT20_PORT is required unless --zenoh-raw is selected")
    session = zenoh.open(_cat20_make_config())
    handler = _cat20__make_cat020_handler(session)
    try:
        if args.zenoh_raw: _cat20__run_zenoh_raw(session, args.input_topic, _cat20_CAT_020, handler, args.verbose)
        else: _cat20__run_inbound(args.port, args.tcp, "CAT-20 Ed.1.11", {_cat20_CAT_020: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat020_record = _cat20_decode_cat020_record


# ==========================================================================
# CAT-021
# ==========================================================================

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
from protocols.protobuf_codec import publish_dual


_cat21_ORG       = os.environ.get("PARTNER_NAMESPACE", "")

_cat21_TOPIC_ROOT = topic_root()

_cat21_HERE      = os.path.dirname(os.path.abspath(__file__))

_cat21__CERT_DIR = os.environ.get("EFDI_CERT_DIR", _cat21_HERE)

_cat21__ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

_cat21_TOPIC_021    = "{}/air/asterix/cat21/civ/aircraft/tracks/v1".format(_cat21_TOPIC_ROOT)
_cat21_RAW_INPUT_TOPIC = "{}/raw/asterix/cat21".format(_cat21_TOPIC_ROOT)

_cat21_CAT_021 = 0x15

_cat21__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat21__EMITTER_CATEGORY = {
    0: "no info",    1: "light",      2: "small",       3: "medium",
    4: "high vortex large", 5: "heavy", 6: "manoeuvrable/high speed",
    10: "glider",    11: "airship",   12: "UAV",        13: "space vehicle",
    14: "emergency vehicle", 15: "service vehicle",    16: "ground obstruction",
}

def _cat21__netbird_ip() -> str:
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

def _cat21_make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_cat21__ENDPOINT]))
    apply_zenoh_auth(conf)
    if _cat21__ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_cat21__CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_cat21__CERT_DIR, _cat21_ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_cat21__CERT_DIR, _cat21_ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf

def _cat21__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat21_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat21__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat21__recv_exact(sock, length - 3)
        yield cat, data

def _cat21_iter_frames_udp(sock: socket.socket):
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

def _cat21_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat21__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat21__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat21__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat21__s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

def _cat21__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat21__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat21__decode_bds50(mb: bytes) -> dict:
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

def _cat21__decode_bds60(mb: bytes) -> dict:
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

def _cat21__decode_bds40(mb: bytes) -> dict:
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

def _cat21__decode_bds30(mb: bytes) -> dict:
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

def _cat21_decode_cat021_record(data: bytes, pos: int):
    """Decode one CAT-021 ADS-B record. Returns (track_dict, new_pos)."""
    fspec, pos = _cat21_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-21"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I021/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I021/040  Target Report Descriptor (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            atp = (b >> 5) & 0x07
            arc = (b >> 3) & 0x03
            track["addr_type"] = ("icao24","icao24_dup","surface","anonymous","non_icao","","","")[atp]
            track["alt_res"]   = ("unknown","25ft","100ft","n/a")[arc]
            if not (b & 0x04): track["range_check_fail"] = True
            if b & 0x02:       track["surface_vehicle"]  = True   # RAB
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["diff_correction"] = True
                if b & 0x40: track["on_ground"]       = True
                if b & 0x20: track["simulated"]       = True
                if b & 0x10: track["test_target"]     = True
                if b & 0x08: track["mil_emergency"]   = True
                if b & 0x04: track["mil_ident"]       = True
                foe = (b >> 1) & 0x03
                if foe: track["iff"] = ("","friendly","unknown","no_reply")[foe]
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 2:                  # I021/030  Time of Day (1/128 s)
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I021/130  WGS-84 (3+3 bytes, 180/2^23)
            if pos + 6 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 23)
            track["lat_deg"] = round(_cat21__s24(data[pos:pos + 3]) * scale, 6)
            track["lon_deg"] = round(_cat21__s24(data[pos + 3:pos + 6]) * scale, 6)
            pos += 6
        elif frn == 4:                  # I021/080  ICAO 24-bit
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 5:                  # I021/140  Geometric Altitude (6.25 ft/LSB)
            if pos + 2 > len(data): return track, len(data)
            track["alt_geom_ft"] = round(_cat21__s16(data[pos:pos + 2]) * 6.25); pos += 2
        elif frn == 6:                  # I021/090  Figure of Merit (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2])
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
            track["roll_deg"] = round(_cat21__s16(data[pos:pos + 2]) * 45.0 / 512.0, 1)
            pos += 2
        elif frn == 9:                  # I021/145  Barometric FL (1/4 FL × 100 ft)
            if pos + 2 > len(data): return track, len(data)
            track["alt_baro_ft"] = _cat21__s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 10:                 # I021/150  Air Speed (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2])
            im  = (w >> 15) & 1
            val = w & 0x7FFF
            if im:
                track["mach"] = round(val * 2.0 / (2 ** 14), 3)
            else:
                track["ias_kt"] = round(val * 3600.0 / 16384.0, 1)
            pos += 2
        elif frn == 11:                 # I021/151  True Airspeed (2 bytes, u15 kt)
            if pos + 2 > len(data): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]) & 0x7FFF
            if w: track["tas_kt"] = w
            pos += 2
        elif frn == 12:                 # I021/152  Magnetic Heading (2 bytes, u16)
            if pos + 2 > len(data): return track, len(data)
            track["mag_hdg_deg"] = round(_cat21__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 2)
            pos += 2
        elif frn == 13:                 # I021/155  Barometric Vertical Rate (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2])
            re  = (w >> 15) & 1
            if not re:
                val = w & 0x7FFF
                if val >= 0x4000: val -= 0x8000
                track["baro_vr_fpm"] = round(val * 6.25)
            pos += 2
        elif frn == 14:                 # I021/157  Geometric Vertical Rate (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2])
            re  = (w >> 15) & 1
            if not re:
                val = w & 0x7FFF
                if val >= 0x4000: val -= 0x8000
                track["geo_vr_fpm"] = round(val * 6.25)
            pos += 2
        elif frn == 15:                 # I021/160  Ground Vector (GS 2B + TA 2B)
            if pos + 4 > len(data): return track, len(data)
            gs_raw = _cat21__u16(data[pos:pos + 2]) & 0x7FFF
            ta_raw = _cat21__u16(data[pos + 2:pos + 4])
            track["speed_ms"]    = round(gs_raw * (3600.0 / 16384.0) * 0.514444, 2)
            track["heading_deg"] = round(ta_raw * 360.0 / 65536.0, 2)
            pos += 4
        elif frn == 16:                 # I021/165  Track Angle Rate (2 bytes, s16)
            if pos + 2 > len(data): return track, len(data)
            track["track_angle_rate_degs"] = round(_cat21__s16(data[pos:pos + 2]) * 360.0 / 65536.0, 3)
            pos += 2
        elif frn == 17:                 # I021/170  Target Identification (callsign)
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _cat21__decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 18:                 # I021/095  Velocity Accuracy (1 byte)
            if pos + 1 > len(data): return track, len(data)
            nac_v = (data[pos] >> 4) & 0x0F
            if nac_v and "nac_v" not in track: track["nac_v"] = nac_v
            pos += 1
        elif frn == 19:                 # I021/032  Time of Day Accuracy (1 byte, 1/128 s)
            if pos + 1 > len(data): return track, len(data)
            track["tod_accuracy_s"] = round(data[pos] / 128.0, 4); pos += 1
        elif frn == 20:                 # I021/200  Target Status (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            ss = (b >> 6) & 0x03
            if ss == 1: track["alert"] = "permanent"
            elif ss == 2: track["alert"] = "temporary"
            elif ss == 3: track["spi"]  = True
            if b & 0x20: track["lnav_engaged"]     = True
            if b & 0x10: track["mil_emergency"]    = True
            if b & 0x08: track["tcas_operational"] = True
            if b & 0x04: track["intent_change"]    = True
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
                if b & 0x80: track["autopilot"]    = True
                if b & 0x40: track["vnav_active"]  = True
                if b & 0x20: track["alt_hold"]     = True
                if b & 0x10: track["approach_mode"]= True
                if b & 0x08: track["tcas_ra"]      = True
                if b & 0x04: track["ident_switch"]  = True
        elif frn == 21:                 # I021/020  Emitter Category (1 byte)
            if pos + 1 > len(data): return track, len(data)
            ec = data[pos]; pos += 1
            track["emitter_category"]     = ec
            track["emitter_category_str"] = _cat21__EMITTER_CATEGORY.get(ec, "cat{}".format(ec))
        elif frn == 22:                 # I021/220  Met Information (compound)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80:              # wind speed (2 bytes, 0.5 kt)
                if pos + 2 > len(data): return track, len(data)
                track["wind_speed_kt"] = round(_cat21__u16(data[pos:pos + 2]) * 0.5, 1); pos += 2
            if psf & 0x40:              # wind direction (2 bytes, 360/65536)
                if pos + 2 > len(data): return track, len(data)
                track["wind_dir_deg"] = round(_cat21__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 1); pos += 2
            if psf & 0x20:              # temperature (2 bytes, s16, 0.25°C)
                if pos + 2 > len(data): return track, len(data)
                track["temp_c"] = round(_cat21__s16(data[pos:pos + 2]) * 0.25, 1); pos += 2
            if psf & 0x10:              # turbulence (1 byte)
                if pos + 1 > len(data): return track, len(data)
                track["turbulence"] = data[pos]; pos += 1
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 23:                 # I021/146  Selected Altitude (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            w   = _cat21__u16(data[pos:pos + 2])
            src = (w >> 13) & 0x03
            val = w & 0x1FFF
            if val >= 0x1000: val -= 0x2000
            track["selected_alt_ft"]     = val * 25
            track["selected_alt_source"] = ("MCP/FCU", "FMS", "FMS2", "reserved")[src]
            pos += 2
        elif frn == 24:                 # I021/148  Final State Selected Altitude
            if pos + 2 > len(data): return track, len(data)
            w   = _cat21__u16(data[pos:pos + 2]) & 0x1FFF
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
            track["squawk"] = "{:04o}".format(_cat21__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 27:                 # I021/131  High-Res WGS-84 (4+4, 180/2^31)
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 31)
            track["lat_deg"] = round(_cat21__s32(data[pos:pos + 4]) * scale, 7)
            track["lon_deg"] = round(_cat21__s32(data[pos + 4:pos + 8]) * scale, 7)
            pos += 8
        elif frn == 28:                 # I021/132  Message Amplitude (s8, 1 dBm/LSB)
            if pos + 1 > len(data): return track, len(data)
            track["signal_amplitude_dbm"] = struct.unpack_from("b", data, pos)[0]; pos += 1
        elif frn == 29:                 # I021/250  Mode-S MB Data (REP × 8 bytes)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb = bytes(data[pos:pos + 7]); bds = data[pos + 7]; pos += 8
                bds1 = (bds >> 4) & 0x0F; bds2 = bds & 0x0F
                if   bds1 == 3 and bds2 == 0: track.update(_cat21__decode_bds30(mb))
                elif bds1 == 4 and bds2 == 0: track.update(_cat21__decode_bds40(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_cat21__decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_cat21__decode_bds60(mb))
        elif frn == 30:                 # I021/260  ACAS Resolution Advisory (7 bytes, BDS 3,0)
            if pos + 7 > len(data): return track, len(data)
            track.update(_cat21__decode_bds30(data[pos:pos + 7])); pos += 7
        elif frn == 31:                 # I021/400  Receiver ID (1 byte)
            if pos + 1 > len(data): return track, len(data)
            track["receiver_id"] = data[pos]; pos += 1
        elif frn == 32: pos += 3        # I021/008  ACAS Capability/Operational Status (3 bytes)
        elif frn == 33: pos += 2        # I021/271  Surface Capabilities and Status (2 bytes)
        else: break
    return track, pos

def _cat21__pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, _cat21_TOPIC_021, track, AsterixCat21Track, zenoh)
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

def _cat21__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat21__presence(data: bytes, pos: int, maximum: int) -> tuple[list[bool], int]:
    """Read all compound primary-subfield octets before their payloads."""
    present: list[bool] = []
    for _ in range(maximum):
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 1:
            return present, pos
    if data[pos - 1] & 1:
        raise ValueError("unsupported ASTERIX compound presence extension")
    return present, pos


def _cat21_decode_cat021_record(data: bytes, pos: int):
    """Decode one EUROCONTROL CAT-021 Edition 2.7 ADS-B record."""
    fspec, pos = _cat21_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-21 Ed.2.7"}

    def need(size: int) -> bool:
        return pos + size <= len(data)

    def time24(name: str) -> bool:
        nonlocal pos
        if not need(3): return False
        track[name] = int.from_bytes(data[pos:pos + 3], "big") / 128.0
        pos += 3
        return True

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I021/010
            if not need(2): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I021/040
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["addr_type"] = ("icao24", "duplicate", "surface", "anonymous",
                                  "reserved", "reserved", "reserved", "reserved")[(b >> 5) & 7]
            track["alt_res"] = ("25ft", "100ft", "unknown", "invalid")[(b >> 3) & 3]
            if b & 0x04: track["range_check_passed_cpr_pending"] = True
            if b & 0x02: track["field_monitor"] = True
            extent = 0
            while b & 1:
                if not need(1): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1:
                    if b & 0x80: track["differential_correction"] = True
                    if b & 0x40: track["on_ground"] = True
                    if b & 0x20: track["simulated"] = True
                    if b & 0x10: track["test_target"] = True
                    if b & 0x08: track["selected_altitude_unavailable"] = True
                    track["confidence_level"] = (b >> 1) & 3
                elif extent == 2:
                    for mask, name in ((0x40, "list_lookup_suspect"),
                                       (0x20, "independent_position_check_failed"),
                                       (0x10, "ground_station_nogo"),
                                       (0x08, "cpr_validation_failed"),
                                       (0x04, "local_position_jump"),
                                       (0x02, "range_check_failed")):
                        if b & mask: track[name] = True
                elif extent == 3 and b & 0x80:
                    track["total_bits_corrected"] = (b >> 1) & 0x3F
                elif extent == 4 and b & 0x80:
                    track["maximum_bits_corrected"] = (b >> 1) & 0x3F
        elif frn == 2:                  # I021/161
            if not need(2): return track, len(data)
            track["track_num"] = _cat21__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 3:                  # I021/015
            if not need(1): return track, len(data)
            track["service_id"] = data[pos]; pos += 1
        elif frn == 4:                  # I021/071
            if not time24("position_time_s"): return track, len(data)
        elif frn == 5:                  # I021/130
            if not need(6): return track, len(data)
            scale = 180.0 / 2**23
            track["lat_deg"] = round(_cat21__s24(data[pos:pos + 3]) * scale, 7)
            track["lon_deg"] = round(_cat21__s24(data[pos + 3:pos + 6]) * scale, 7); pos += 6
        elif frn == 6:                  # I021/131
            if not need(8): return track, len(data)
            scale = 180.0 / 2**30
            track["lat_deg"] = round(_cat21__s32(data[pos:pos + 4]) * scale, 8)
            track["lon_deg"] = round(_cat21__s32(data[pos + 4:pos + 8]) * scale, 8); pos += 8
        elif frn == 7:                  # I021/072
            if not time24("velocity_time_s"): return track, len(data)
        elif frn == 8:                  # I021/150
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            if w & 0x8000: track["mach"] = round((w & 0x7FFF) * 0.001, 3)
            else: track["ias_kt"] = round((w & 0x7FFF) * 3600.0 / 16384.0, 2)
        elif frn == 9:                  # I021/151
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            track["tas_kt"] = w & 0x7FFF
            if w & 0x8000: track["tas_range_exceeded"] = True
        elif frn == 10:                 # I021/080
            if not need(3): return track, len(data)
            track["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif frn in (11, 13, 27):       # I021/073, /075, /077
            names = {11: "position_reception_time_s", 13: "velocity_reception_time_s",
                     27: "report_transmission_time_s"}
            if not time24(names[frn]): return track, len(data)
        elif frn in (12, 14):           # I021/074, /076 high precision time
            if not need(4): return track, len(data)
            w = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
            track["position_reception_time_hp" if frn == 12 else "velocity_reception_time_hp"] = \
                round((w & 0x3FFFFFFF) / 2**30, 9)
        elif frn == 15:                 # I021/140
            if not need(2): return track, len(data)
            track["alt_geom_ft"] = _cat21__s16(data[pos:pos + 2]) * 6.25; pos += 2
        elif frn == 16:                 # I021/090
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["nac_v"] = (b >> 5) & 7; track["nic"] = (b >> 1) & 15
            extent = 0
            while b & 1:
                if not need(1): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1:
                    track["nic_baro"] = bool(b & 0x80)
                    track["sil"] = (b >> 5) & 3; track["nac_p"] = (b >> 1) & 15
                elif extent == 2:
                    track["sil_per_sample"] = bool(b & 0x20)
                    track["sda"] = (b >> 3) & 3; track["gva"] = (b >> 1) & 3
                elif extent == 3:
                    track["pic"] = (b >> 4) & 15; track["pic_direct"] = bool(b & 0x08)
        elif frn == 17:                 # I021/210
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["mops_version_unsupported"] = bool(b & 0x40)
            track["mops_version"] = (b >> 3) & 7
            track["link_technology"] = ("other", "uat", "1090es", "vdl4",
                                        "unassigned", "unassigned", "unassigned", "unassigned")[b & 7]
        elif frn == 18:                 # I021/070
            if not need(2): return track, len(data)
            track["squawk"] = "{:04o}".format(_cat21__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 19:                 # I021/230
            if not need(2): return track, len(data)
            track["roll_deg"] = round(_cat21__s16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif frn == 20:                 # I021/145
            if not need(2): return track, len(data)
            track["alt_baro_ft"] = _cat21__s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 21:                 # I021/152
            if not need(2): return track, len(data)
            track["mag_hdg_deg"] = round(_cat21__u16(data[pos:pos + 2]) * 360 / 65536, 3); pos += 2
        elif frn == 22:                 # I021/200
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["intent_change"] = True
            track["lnav_engaged"] = not bool(b & 0x40)
            if b & 0x20: track["mil_emergency"] = True
            track["priority_status"] = (b >> 2) & 7
            status = b & 3
            if status == 1: track["alert"] = "permanent"
            elif status == 2: track["alert"] = "temporary"
            elif status == 3: track["spi"] = True
        elif frn in (23, 24):           # I021/155, /157
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x7FFF
            if raw & 0x4000: raw -= 0x8000
            track["baro_vr_fpm" if frn == 23 else "geo_vr_fpm"] = raw * 6.25
            if w & 0x8000: track["vertical_rate_range_exceeded"] = True
        elif frn == 25:                 # I021/160
            if not need(4): return track, len(data)
            speed = _cat21__u16(data[pos:pos + 2]); angle = _cat21__u16(data[pos + 2:pos + 4]); pos += 4
            track["speed_ms"] = round((speed & 0x7FFF) * 2**-14 * 1852, 3)
            track["heading_deg"] = round(angle * 360 / 65536, 3)
            if speed & 0x8000: track["ground_speed_range_exceeded"] = True
        elif frn == 26:                 # I021/165
            if not need(2): return track, len(data)
            raw = _cat21__u16(data[pos:pos + 2]) & 0x03FF; pos += 2
            if raw & 0x0200: raw -= 0x0400
            track["track_angle_rate_degs"] = round(raw / 32.0, 3)
        elif frn == 28:                 # I021/170
            if not need(6): return track, len(data)
            track["callsign"] = _cat21__decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 29:                 # I021/020
            if not need(1): return track, len(data)
            ec = data[pos]; pos += 1
            track["emitter_category"] = ec
            track["emitter_category_str"] = _cat21__EMITTER_CATEGORY.get(ec, "cat{}".format(ec))
        elif frn == 30:                 # I021/220
            try: flags, pos = _cat21__presence(data, pos, 1)
            except ValueError: return track, len(data)
            sizes = (2, 2, 2, 1)
            for index, size in enumerate(sizes):
                if index < len(flags) and flags[index]:
                    if not need(size): return track, len(data)
                    if index == 0: track["wind_speed_kt"] = _cat21__u16(data[pos:pos + 2])
                    elif index == 1: track["wind_dir_deg"] = _cat21__u16(data[pos:pos + 2])
                    elif index == 2: track["temp_c"] = _cat21__s16(data[pos:pos + 2]) * 0.25
                    else: track["turbulence"] = data[pos]
                    pos += size
        elif frn in (31, 32):           # I021/146, /148
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x1FFF
            if raw & 0x1000: raw -= 0x2000
            track["selected_alt_ft" if frn == 31 else "final_alt_ft"] = raw * 25
            if frn == 31:
                source = (w >> 13) & 3
                track["selected_alt_source_available"] = bool(w & 0x8000)
                track["selected_alt_source"] = (
                    "unknown",
                    "aircraft_altitude",
                    "mcp_fcu",
                    "fms",
                )[source]
            else:
                track["managed_vertical_mode"] = bool(w & 0x8000)
                track["altitude_hold_mode"] = bool(w & 0x4000)
                track["approach_mode"] = bool(w & 0x2000)
        elif frn == 33:                 # I021/110
            try: flags, pos = _cat21__presence(data, pos, 1)
            except ValueError: return track, len(data)
            if flags[0]:
                if not need(1): return track, len(data)
                pos += 1
            if flags[1]:
                if not need(1): return track, len(data)
                rep = data[pos]; pos += 1
                if not need(rep * 15): return track, len(data)
                pos += rep * 15
        elif frn == 34:                 # I021/016
            if not need(1): return track, len(data)
            track["report_period_s"] = data[pos] * 0.5; pos += 1
        elif frn == 35:                 # I021/008
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["aircraft_operational_status_raw"] = b
            track["acas_ra_active"] = bool(b & 0x80)
            track["trajectory_change_capability"] = (
                "none",
                "tc_plus_zero",
                "multiple",
                "reserved",
            )[(b >> 5) & 0x03]
            track["target_state_report_capable"] = bool(b & 0x10)
            track["air_ref_velocity_capable"] = bool(b & 0x08)
            track["cdti_airborne_operational"] = bool(b & 0x04)
            track["tcas_operational"] = not bool(b & 0x02)
            track["single_antenna"] = bool(b & 0x01)
        elif frn == 36:                 # I021/271
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x20: track["position_offset_applied"] = True
            if b & 0x10: track["cdti_surface_operational"] = True
            if b & 0x08: track["class_b2_low_power"] = True
            if b & 0x04: track["receiving_atc_services"] = True
            if b & 0x02: track["ident_switch"] = True
            if b & 1:
                if not need(1): return track, len(data)
                b = data[pos]; pos += 1
                track["surface_length_width_code"] = (b >> 4) & 15
                while b & 1:
                    if not need(1): return track, len(data)
                    b = data[pos]; pos += 1
        elif frn == 37:                 # I021/132
            if not need(1): return track, len(data)
            track["message_amplitude_dbm"] = struct.unpack_from("b", data, pos)[0]; pos += 1
        elif frn == 38:                 # I021/250
            if not need(1): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if not need(8): return track, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = (bds >> 4, bds & 15)
                if code == (3, 0): track.update(_cat21__decode_bds30(mb))
                elif code == (4, 0): track.update(_cat21__decode_bds40(mb))
                elif code == (5, 0): track.update(_cat21__decode_bds50(mb))
                elif code == (6, 0): track.update(_cat21__decode_bds60(mb))
        elif frn == 39:                 # I021/260
            if not need(7): return track, len(data)
            track.update(_cat21__decode_bds30(data[pos:pos + 7])); pos += 7
        elif frn == 40:                 # I021/400
            if not need(1): return track, len(data)
            track["receiver_id"] = data[pos]; pos += 1
        elif frn == 41:                 # I021/295
            try: flags, pos = _cat21__presence(data, pos, 4)
            except ValueError: return track, len(data)
            names = ("aircraft_status", "target_descriptor", "mode3a", "quality", "trajectory",
                     "amplitude", "geometric_height", "flight_level", "selected_altitude",
                     "final_altitude", "air_speed", "true_air_speed", "mag_heading", "baro_vr",
                     "geo_vr", "ground_vector", "track_angle_rate", "target_id", "target_status",
                     "met", "roll", "acas_ra", "surface_capabilities")
            for index, name in enumerate(names):
                if index < len(flags) and flags[index]:
                    if not need(1): return track, len(data)
                    track["data_age_{}_s".format(name)] = round(data[pos] * 0.1, 1); pos += 1
        elif frn in (47, 48):           # RE / SP
            pos = _cat21__skip_len_field(data, pos)
        elif frn >= 42:
            continue                    # explicitly unused FRNs 43..47
        else:
            break
    return track, pos


def _cat21__make_cat021_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = _cat21_decode_cat021_record(data, pos)
            if len(track) > 2:
                _cat21__pub(pub, track, "cat21", verbose)
    return _h

def _cat21__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat21__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat21__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat21__raw_frame_payload(bytes(sample.payload), category)
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

def _cat21__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat21__process_stream(_cat21_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat21__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat21__netbird_ip()
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
                target=_cat21__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat21__process_stream(_cat21_iter_frames_udp(sock), handlers, verbose)



def _cat21_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-021 Ed.2.7 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT21_PORT", "50021") or 50021))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT21_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT21_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT21_INPUT_TOPIC", _cat21_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT21_PORT is required unless --zenoh-raw is selected")
    session = zenoh.open(_cat21_make_config())
    handler = _cat21__make_cat021_handler(session)
    try:
        if args.zenoh_raw: _cat21__run_zenoh_raw(session, args.input_topic, _cat21_CAT_021, handler, args.verbose)
        else: _cat21__run_inbound(args.port, args.tcp, "CAT-21 Ed.2.7", {_cat21_CAT_021: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat021_record = _cat21_decode_cat021_record


# ==========================================================================
# CAT-034
# ==========================================================================

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
from protocols.protobuf_codec import publish_dual


_cat34_ORG       = os.environ.get("PARTNER_NAMESPACE", "")

_cat34_TOPIC_ROOT = topic_root()

_cat34_HERE      = os.path.dirname(os.path.abspath(__file__))

_cat34__CERT_DIR = os.environ.get("EFDI_CERT_DIR", _cat34_HERE)

_cat34__ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

_cat34_TOPIC_SENSOR = "{}/land/asterix/cat34/neutral/radar/status/v1".format(_cat34_TOPIC_ROOT)
_cat34_RAW_INPUT_TOPIC = "{}/raw/asterix/cat34".format(_cat34_TOPIC_ROOT)

_cat34_CAT_034 = 0x22

_cat34__MSG_TYPES_034 = {1: "north_marker", 2: "sector_crossing",
                  3: "geo_filter",   4: "jamming_strobe"}

_cat34__COUNT_LABELS = {
    0: "no_detection", 1: "psr",        2: "ssr",         3: "psr_ssr",
    4: "all",          5: "no_det_psr", 6: "no_det_ssr",  7: "mode5",
    11: "mil_id",
}

def _cat34__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0

def _cat34__netbird_ip() -> str:
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

def _cat34_make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_cat34__ENDPOINT]))
    apply_zenoh_auth(conf)
    if _cat34__ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_cat34__CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_cat34__CERT_DIR, _cat34_ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_cat34__CERT_DIR, _cat34_ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf

def _cat34__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat34_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat34__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat34__recv_exact(sock, length - 3)
        yield cat, data

def _cat34_iter_frames_udp(sock: socket.socket):
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

def _cat34_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat34__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat34__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat34__s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

def _cat34__decode_i034_050(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/050 System Configuration and Status — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    primary = []
    while pos < len(data):
        sf = data[pos]; pos += 1
        primary.append(sf)
        if not sf & 0x01:
            break
    if not primary:
        return out, pos
    sf = primary[0]
    if sf & 0x80:
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        out["sys_nogo"] = bool(com & 0x80)
        out["rdp_chain_2"] = bool(com & 0x40)
        out["rdp_restart"] = bool(com & 0x20)
        out["sys_ovl_rdp"] = bool(com & 0x10)
        out["sys_ovl_xmt"] = bool(com & 0x08)
        out["sys_msc_connected"] = not bool(com & 0x04)
        out["sys_tsv_invalid"] = bool(com & 0x02)
    channel_names = ("none", "a", "b", "diversity")
    for prefix, mask, size in (("psr", 0x10, 1), ("ssr", 0x08, 1),
                               ("mds", 0x04, 2)):
        if sf & mask:
            if pos + size > len(data): return out, len(data)
            first = data[pos]; pos += 1
            out[prefix + "_antenna"] = 2 if first & 0x80 else 1
            out[prefix + "_channel"] = channel_names[(first >> 5) & 0x03]
            overload = bool(first & 0x10)
            msc_disconnected = bool(first & 0x08)
            if size == 2:
                second = data[pos]
                pos += 1
                out["mds_scf_channel"] = 2 if first & 0x04 else 1
                out["mds_dlf_channel"] = 2 if first & 0x02 else 1
                out["mds_scf_overload"] = bool(first & 0x01)
                out["mds_dlf_overload"] = bool(second & 0x80)
            out[prefix + "_status"] = "overload" if overload else "operational"
            out[prefix + "_overload"] = overload
            out[prefix + "_msc_connected"] = not msc_disconnected
    return out, pos

def _cat34__decode_i034_060(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/060 System Processing Mode — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    primary = []
    while pos < len(data):
        sf = data[pos]; pos += 1
        primary.append(sf)
        if not sf & 0x01:
            break
    if not primary:
        return out, pos
    sf = primary[0]
    if sf & 0x80:               # COM sub-field
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        out["rdp_reduction_level"] = (com >> 4) & 0x07
        out["xmt_reduction_level"] = (com >> 1) & 0x07
    if sf & 0x10:               # PSR sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        out["psr_polarization"]    = "circular" if (b & 0x80) else "linear"
        out["psr_reduction_level"] = (b >> 4) & 0x07
        out["psr_stc_map"] = (b >> 2) & 0x03
    if sf & 0x08:               # SSR sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        out["ssr_reduction_level"] = (b >> 5) & 0x07
    if sf & 0x04:               # MDS sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        out["mds_reduction_level"] = (b >> 5) & 0x07
        if b & 0x10: out["mds_cluster_state"] = True
    return out, pos

def _cat34_decode_cat034(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat34_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I034/010  SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I034/000  Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _cat34__MSG_TYPES_034.get(data[pos], data[pos]); pos += 1
        elif frn == 2:                  # I034/030  Time of Day (1/128 s)
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I034/020  Sector Number (360/256 °)
            if pos + 1 > len(data): break
            msg["sector_deg"] = round(data[pos] * 360.0 / 256.0, 2); pos += 1
        elif frn == 4:                  # I034/041  Antenna Rotation Period (1/128 s)
            if pos + 2 > len(data): break
            msg["rotation_s"] = round(_cat34__u16(data[pos:pos+2]) / 128.0, 2); pos += 2
        elif frn == 5:                  # I034/050  System Configuration (compound)
            extra, pos = _cat34__decode_i034_050(data, pos)
            msg.update(extra)
        elif frn == 6:                  # I034/060  System Processing Mode (compound)
            extra, pos = _cat34__decode_i034_060(data, pos)
            msg.update(extra)
        elif frn == 7:                  # I034/070  Message Count Values (REP × 2 bytes)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            counts = {}
            for _ in range(rep):
                if pos + 2 > len(data): break
                word = _cat34__u16(data[pos:pos+2]); pos += 2
                counts[_cat34__COUNT_LABELS.get((word >> 11) & 0x1F,
                                          "type{}".format((word >> 11) & 0x1F))] = word & 0x7FF
            if counts: msg["msg_counts"] = counts
        elif frn == 8:                  # I034/100  Generic Polar Window (8 bytes)
            if pos + 8 > len(data): break
            msg["coverage_rho_start_nm"] = round(_cat34__u16(data[pos:pos+2]) / 256.0, 3)
            msg["coverage_rho_end_nm"]   = round(_cat34__u16(data[pos+2:pos+4]) / 256.0, 3)
            msg["coverage_az_start_deg"] = round(_cat34__u16(data[pos+4:pos+6]) * 360.0 / 65536.0, 2)
            msg["coverage_az_end_deg"]   = round(_cat34__u16(data[pos+6:pos+8]) * 360.0 / 65536.0, 2)
            pos += 8
        elif frn == 9:                  # I034/110  Data Filter (1 byte)
            if pos + 1 > len(data): break
            _FILT034 = {0: "invalid", 1: "weather", 2: "jamming", 3: "psr",
                        4: "ssr_mode_s", 5: "combined", 6: "enhanced_surveillance",
                        7: "psr_enhanced", 8: "psr_enhanced_ssr_not_aoi", 9: "all"}
            msg["data_filter"] = _FILT034.get(data[pos], "type_{}".format(data[pos]))
            pos += 1
        elif frn == 10:                 # I034/120 3D Position of Data Source (fixed 8 bytes)
            if pos + 8 > len(data): break
            msg["site_alt_m"] = _cat34__s16(data[pos:pos + 2])
            msg["site_lat"] = round(_cat34__s24(data[pos + 2:pos + 5]) * (180.0 / 2**23), 7)
            msg["site_lon"] = round(_cat34__s24(data[pos + 5:pos + 8]) * (180.0 / 2**23), 7)
            pos += 8
        elif frn == 11:                 # I034/090 Collimation Error (fixed 2 bytes)
            if pos + 2 > len(data): break
            msg["collimation_rng_nm"] = round(struct.unpack_from("b", data, pos)[0] / 128.0, 4)
            msg["collimation_az_deg"] = round(struct.unpack_from("b", data, pos + 1)[0] * 360.0 / 16384.0, 4)
            pos += 2
        else:
            break
    return msg if msg else None

def _cat34__make_cat034_handler(pub_sensor, site, radar_name):
    # site = [lat, lon] — mutable so CAT-34 I034/120 can self-configure the radar position
    _first_seen:   dict[str, float] = {}
    _sweep:        dict[str, dict]  = {}   # key → {north_ts, rotation_s, status}
    _sweep_lock    = threading.Lock()
    _sweep_active: set[str]         = set()
    _keepalive:    dict[str, dict]  = {}   # key → last full status
    _keepalive_active: set[str]     = set()
    _ka_lock       = threading.Lock()
    _pos_hist:     dict[str, tuple] = {}   # key → (ts, lat, lon) for speed/course
    KEEPALIVE_S    = 60   # republish site marker every 60 s so ATAK never loses it

    def _keepalive_thread(key: str):
        """Republish the last known full status every KEEPALIVE_S seconds.
        This keeps the ATAK radar site marker alive even when the radar is offline."""
        while True:
            time.sleep(KEEPALIVE_S)
            with _ka_lock:
                status = _keepalive.get(key)
            if status is None:
                return
            payload = dict(status)
            payload["_ts"] = time.time()
            publish_dual(
                pub_sensor,
                _cat34_TOPIC_SENSOR,
                payload,
                AsterixCat34Status,
                zenoh,
                wrapper_field="sensor",
            )

    def _sweep_thread(key: str):
        """Publish radar beam CoT at 5 Hz using dead-reckoned antenna azimuth."""
        while True:
            time.sleep(0.2)
            with _sweep_lock:
                s = _sweep.get(key)
            if s is None:
                return
            rot = s.get("rotation_s")
            if not rot:
                continue
            # Stop animating if no north marker for > 3 rotations (radar offline)
            if time.time() - s["north_ts"] > rot * 3:
                continue
            az = (time.time() - s["north_ts"]) / rot * 360.0 % 360.0
            payload = dict(s["status"])
            payload["sweep_azimuth_deg"] = round(az, 1)
            payload["_ts"] = time.time()
            publish_dual(
                pub_sensor,
                _cat34_TOPIC_SENSOR,
                payload,
                AsterixCat34Status,
                zenoh,
                wrapper_field="sensor",
            )

    def _h(data: bytes, verbose: bool):
        msg = _cat34_decode_cat034(data)
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

        # Self-configure: update radar site position from I034/120 when the radar transmits it
        if msg.get("site_lat") is not None:
            site[0] = msg["site_lat"]
            site[1] = msg["site_lon"]

        if not (pub_sensor and site[0] is not None and site[1] is not None):
            return

        sac = msg.get("sac", 0); sic = msg.get("sic", 0)
        key = "{}-{}".format(sac, sic)
        now = time.time()

        if mtype == "north_marker":
            _first_seen.setdefault(key, now)

            # Compute speed and course from successive position reports (mobile platform support)
            speed_ms = heading_deg = None
            prev = _pos_hist.get(key)
            if prev and site[0] is not None and site[1] is not None:
                dt = now - prev[0]
                if 0 < dt < 3600:
                    dlat = (site[0] - prev[1]) * 111320.0
                    dlon = (site[1] - prev[2]) * 111320.0 * math.cos(math.radians(site[0]))
                    dist_m = math.hypot(dlat, dlon)
                    speed_ms = round(dist_m / dt, 2)
                    if dist_m > 1.0:
                        heading_deg = round((math.degrees(math.atan2(dlon, dlat)) + 360) % 360, 1)
            _pos_hist[key] = (now, site[0], site[1])

            status = {
                "_src":        "ASTERIX CAT-34 Ed.1.29",
                "_ts":         now,
                "sensor_type": "radar",
                "sensor_id":   "CAT34-{}-{}".format(sac, sic),
                "sensor_name": radar_name or "RADAR SAC{}/SIC{}".format(sac, sic),
                "lat_deg":     site[0],
                "lon_deg":     site[1],
                "online_since": _first_seen[key],
            }
            if speed_ms is not None:
                status["speed_ms"]    = speed_ms
            if heading_deg is not None:
                status["heading_deg"] = heading_deg
            for k, v in msg.items():
                if k == "tod_s":
                    status["radar_clock_s"] = v
                else:
                    status[k] = v

            with _sweep_lock:
                existing = _sweep.get(key, {})
                _sweep[key] = {
                    "north_ts":   now,
                    "rotation_s": rot or existing.get("rotation_s", 4.0),
                    "status":     status,
                }
                start_thread = key not in _sweep_active
                _sweep_active.add(key)

            # Update keepalive store and start keepalive thread on first north marker
            with _ka_lock:
                _keepalive[key] = status
                start_ka = key not in _keepalive_active
                _keepalive_active.add(key)

            # Publish the full status update (no sweep azimuth — just the site marker)
            publish_dual(
                pub_sensor,
                _cat34_TOPIC_SENSOR,
                status,
                AsterixCat34Status,
                zenoh,
                wrapper_field="sensor",
            )

            if start_thread:
                threading.Thread(target=_sweep_thread, args=(key,),
                                  daemon=True).start()
            if start_ka:
                threading.Thread(target=_keepalive_thread, args=(key,),
                                  daemon=True).start()

        elif mtype == "sector_crossing":
            # Re-sync virtual north_ts so azimuth stays accurate between north markers
            sector_deg = msg.get("sector_deg", 0.0)
            with _sweep_lock:
                s = _sweep.get(key)
                if s and s.get("rotation_s"):
                    s["north_ts"] = now - sector_deg / 360.0 * s["rotation_s"]

    return _h

def _cat34__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat34__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat34__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat34__raw_frame_payload(bytes(sample.payload), category)
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

def _cat34__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat34__process_stream(_cat34_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat34__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat34__netbird_ip()
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
                target=_cat34__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat34__process_stream(_cat34_iter_frames_udp(sock), handlers, verbose)



def _cat34_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-034 Ed.1.29 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT34_PORT", "50034") or 50034))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT34_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT34_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT34_INPUT_TOPIC", _cat34_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat34__env_float("CAT34_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat34__env_float("CAT34_RADAR_LON"))
    parser.add_argument("--site-name", default=os.environ.get("CAT34_RADAR_NAME", ""))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT34_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = zenoh.open(_cat34_make_config())
    handler = _cat34__make_cat034_handler(session, site, args.site_name or None)
    try:
        if args.zenoh_raw: _cat34__run_zenoh_raw(session, args.input_topic, _cat34_CAT_034, handler, args.verbose)
        else: _cat34__run_inbound(args.port, args.tcp, "CAT-34 Ed.1.29", {_cat34_CAT_034: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat034 = _cat34_decode_cat034


# ==========================================================================
# CAT-048
# ==========================================================================

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
from protocols.protobuf_codec import publish_dual


_cat48_ORG       = os.environ.get("PARTNER_NAMESPACE", "")

_cat48_TOPIC_ROOT = topic_root()

_cat48_HERE      = os.path.dirname(os.path.abspath(__file__))

_cat48__CERT_DIR = os.environ.get("EFDI_CERT_DIR", _cat48_HERE)

_cat48__ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

_cat48_TOPIC_048    = "{}/air/asterix/cat48/unknown/aircraft/tracks/v1".format(_cat48_TOPIC_ROOT)
_cat48_RAW_INPUT_TOPIC = "{}/raw/asterix/cat48".format(_cat48_TOPIC_ROOT)

_cat48_CAT_048 = 0x30

_cat48__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

def _cat48__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0

def _cat48__netbird_ip() -> str:
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

def _cat48_make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_cat48__ENDPOINT]))
    apply_zenoh_auth(conf)
    if _cat48__ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_cat48__CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_cat48__CERT_DIR, _cat48_ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_cat48__CERT_DIR, _cat48_ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf

def _cat48__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat48_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat48__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat48__recv_exact(sock, length - 3)
        yield cat, data

def _cat48_iter_frames_udp(sock: socket.socket):
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

def _cat48_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat48__skip_len_field(data: bytes, pos: int) -> int:
    """Skip an ASTERIX length-prefixed RE/SP item (length includes itself)."""
    if pos >= len(data):
        return len(data)
    length = data[pos]
    if length < 1 or pos + length > len(data):
        return len(data)
    return pos + length

def _cat48__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat48__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat48__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat48__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat48__decode_bds50(mb: bytes) -> dict:
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

def _cat48__decode_bds60(mb: bytes) -> dict:
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

def _cat48__gillham_to_ft(code: int) -> int | None:
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

def _cat48__decode_bds40(mb: bytes) -> dict:
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

def _cat48__decode_bds30(mb: bytes) -> dict:
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

def _cat48__polar_to_wgs84(radar_lat: float, radar_lon: float,
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

def _cat48__compound_presence(data: bytes, pos: int) -> tuple[list[bool], int]:
    """Read all primary-subfield octets before any compound payload."""
    present: list[bool] = []
    while True:
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 0x01:
            return present, pos

def _cat48_decode_cat048_record(data: bytes, pos: int,
                         radar_lat: float | None, radar_lon: float | None):
    """Decode one CAT-048 record. Returns (track_dict, new_pos)."""
    fspec, pos = _cat48_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-48 Ed.1.32"}

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
            _TYP048 = ("no_detection", "psr", "ssr", "ssr_psr", "mode_s_all_call",
                       "mode_s_roll_call", "mode_s_all_call_psr", "mode_s_roll_call_psr")
            track["detection_type"] = _TYP048[typ]
            if b & 0x10: track["simulated"]       = True   # SIM
            if b & 0x08: track["rdp_chain"]       = True   # RDP
            if b & 0x04: track["spi"]             = True
            if b & 0x02: track["field_monitor"] = True     # RAB
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
            range_raw = _cat48__u16(data[pos:pos+2])
            az_raw    = _cat48__u16(data[pos+2:pos+4])
            range_nm  = range_raw / 256.0
            az_deg    = az_raw * 360.0 / 65536.0
            track["range_nm"]    = round(range_nm, 3)
            track["azimuth_deg"] = round(az_deg, 3)
            if radar_lat is not None and radar_lon is not None and range_nm > 0:
                lat, lon = _cat48__polar_to_wgs84(radar_lat, radar_lon, range_nm, az_deg)
                track.setdefault("lat_deg", round(lat, 6))
                track.setdefault("lon_deg", round(lon, 6))
            pos += 4
        elif frn == 4:                  # I048/070  Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            w = _cat48__u16(data[pos:pos + 2]); pos += 2
            track["squawk"] = "{:04o}".format(w & 0x0FFF)
            if w & 0x8000: track["squawk_invalid"] = True
            if w & 0x4000: track["squawk_garbled"] = True
            if w & 0x2000: track["squawk_not_extracted"] = True
        elif frn == 5:                  # I048/090  Flight Level (1/4 FL, signed)
            if pos + 2 > len(data): return track, len(data)
            w = _cat48__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x3FFF
            if raw & 0x2000: raw -= 0x4000
            track["alt_baro_ft"] = raw * 25
            if w & 0x8000: track["alt_baro_invalid"] = True
            if w & 0x4000: track["alt_baro_garbled"] = True
        elif frn == 6:                  # I048/130  Radar Plot Characteristics (compound)
            try:
                present, pos = _cat48__compound_presence(data, pos)
            except ValueError:
                return track, len(data)
            if any(present[7:]):
                return track, len(data)
            names = (
                "ssr_runlength_deg",
                "ssr_reply_count",
                "ssr_amplitude_dbm",
                "psr_runlength_deg",
                "psr_amplitude_dbm",
                "psr_ssr_range_diff_nm",
                "psr_ssr_az_diff_deg",
            )
            for index, name in enumerate(names):
                if not present[index]:
                    continue
                if pos >= len(data):
                    return track, len(data)
                value = data[pos]; pos += 1
                if index in (0, 3):
                    track[name] = round(value * 360.0 / 8192.0, 3)
                elif index in (2, 4):
                    track[name] = struct.unpack("b", bytes((value,)))[0]
                elif index == 5:
                    track[name] = round(struct.unpack("b", bytes((value,)))[0] / 256.0, 4)
                elif index == 6:
                    track[name] = round(
                        struct.unpack("b", bytes((value,)))[0] * 360.0 / 16384.0,
                        4,
                    )
                else:
                    track[name] = value
        elif frn == 7:                  # I048/220  Aircraft Address (ICAO 24-bit)
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 8:                  # I048/240  Aircraft Identification
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _cat48__decode_callsign(data[pos:pos+6]); pos += 6
        elif frn == 9:                  # I048/250  Mode-S MB Data (REP × 8-byte records)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb   = bytes(data[pos:pos + 7])
                bds  = data[pos + 7]; pos += 8
                bds1 = (bds >> 4) & 0x0F; bds2 = bds & 0x0F
                if   bds1 == 3 and bds2 == 0: track.update(_cat48__decode_bds30(mb))
                elif bds1 == 4 and bds2 == 0: track.update(_cat48__decode_bds40(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_cat48__decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_cat48__decode_bds60(mb))
        elif frn == 10:                 # I048/161  Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat48__u16(data[pos:pos+2]) & 0x0FFF; pos += 2
        elif frn == 11:                 # I048/042  Cartesian Position (1/128 NM, s16×2)
            if pos + 4 > len(data): return track, len(data)
            track["cart_x_nm"] = round(_cat48__s16(data[pos:pos+2]) / 128.0, 3)
            track["cart_y_nm"] = round(_cat48__s16(data[pos+2:pos+4]) / 128.0, 3)
            pos += 4
        elif frn == 12:                 # I048/200  Track Velocity Polar
            if pos + 4 > len(data): return track, len(data)
            spd_raw = _cat48__u16(data[pos:pos+2])
            hdg_raw = _cat48__u16(data[pos+2:pos+4])
            track["speed_ms"]    = round(spd_raw * 1852.0 / 16384.0, 2)   # LSB = 2^-14 NM/s
            track["heading_deg"] = round(hdg_raw * 360.0 / 65536.0, 2)
            pos += 4
        elif frn == 13:                 # I048/170  Track Status (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["confirmed"] = not bool(b & 0x80)
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
                if b & 0x20: track["supported_by_neighbour_node"] = True
                if b & 0x10: track["slant_range_correction"] = True
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
            _WE048 = {
                1: "multipath_reply", 2: "sidelobe_reply", 3: "split_plot",
                4: "second_time_around", 5: "angel", 6: "terrestrial_vehicle",
                7: "fixed_psr_plot", 8: "slow_psr_target", 9: "low_quality_psr",
                10: "phantom_ssr", 11: "mode3a_mismatch", 12: "abnormal_altitude",
                13: "clutter", 14: "max_zero_filter_doppler", 15: "transponder_anomaly",
                16: "duplicate_illegal_address", 17: "mode_s_error_correction",
                18: "undecodable_altitude", 19: "bird", 20: "flock_of_birds",
                21: "mode1_present", 22: "mode2_present", 23: "wind_turbine",
                24: "helicopter", 25: "max_surveillance_reinterrogations",
                26: "max_bds_reinterrogations", 27: "bds_overlay_incoherence",
                28: "potential_bds_swap", 29: "zenithal_gap_update",
                30: "mode_s_track_reacquired", 31: "duplicate_mode5_pair",
                32: "wrong_df_format", 33: "mode_ac_all_call_anomaly",
                34: "si_capability_anomaly", 35: "potential_ic_conflict",
                36: "ic_conflict_detection_possible", 37: "duplicate_mode5_pin",
            }
            codes = []
            while True:
                if pos >= len(data): break
                b = data[pos]; pos += 1
                code = (b >> 1) & 0x7F
                codes.append(code)
                name = _WE048.get(code)
                if name: track.setdefault("warning_error_names", []).append(name)
                if code == 6:
                    track["target_type"] = "ground_vehicle"; track["on_ground"] = True
                if not (b & 0x01): break
            if codes: track["we_conditions"] = codes
        elif frn == 16:                 # I048/080  Mode-3/A Confidence (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            track["squawk_quality_mask"] = (
                _cat48__u16(data[pos:pos + 2]) & 0x0FFF
            )
            pos += 2
        elif frn == 17:                 # I048/100  Mode-C Gillham (2 + 2 confidence bytes)
            if pos + 4 > len(data): return track, len(data)
            alt = _cat48__gillham_to_ft(_cat48__u16(data[pos:pos + 2]))
            if alt is not None: track["mode_c_alt_ft"] = alt
            pos += 4
        elif frn == 18:                 # I048/110  3D Height (25 ft/LSB, signed)
            if pos + 2 > len(data): return track, len(data)
            track["alt_3d_ft"] = _cat48__s16(data[pos:pos+2]) * 25; pos += 2
        elif frn == 19:                 # I048/120  Radial Doppler (compound)
            try:
                present, pos = _cat48__compound_presence(data, pos)
            except ValueError:
                return track, len(data)
            if any(present[2:]):
                return track, len(data)
            if present[0]:              # CAL: doubtful bit + signed 10-bit m/s
                if pos + 2 > len(data): return track, len(data)
                w = _cat48__u16(data[pos:pos + 2])
                raw = w & 0x03FF
                if raw & 0x0200: raw -= 0x0400
                track["doppler_ms"] = raw
                track["doppler_kt"] = round(raw * 1.943844, 1)
                if w & 0x8000: track["doppler_doubtful"] = True
                pos += 2
            if present[1]:              # RDS sub-field: REP × 6 bytes
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                if pos + rep * 6 > len(data): return track, len(data)
                if rep > 0:
                    speed = _cat48__s16(data[pos:pos + 2])
                    track["doppler_raw_ms"] = speed
                    track["doppler_ambiguity_ms"] = _cat48__u16(data[pos + 2:pos + 4])
                    track["doppler_frequency_mhz"] = _cat48__u16(data[pos + 4:pos + 6])
                pos += rep * 6
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
            track.update(_cat48__decode_bds30(data[pos:pos + 7]))
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
            w = _cat48__u16(data[pos:pos + 2]); pos += 2
            track["mode2"] = "{:04o}".format(w & 0x0FFF)
            if w & 0x8000: track["mode2_invalid"]  = True
            if w & 0x4000: track["mode2_garbled"]  = True
            if w & 0x2000: track["mode2_smoothed"] = True
        elif frn == 24:                 # I048/065  Mode-1 confidence/quality
            if pos >= len(data): return track, len(data)
            track["mode1_quality_mask"] = data[pos]; pos += 1
        elif frn == 25:                 # I048/060  Mode-2 confidence/quality
            if pos + 2 > len(data): return track, len(data)
            track["mode2_quality_mask"] = _cat48__u16(data[pos:pos + 2]); pos += 2
        elif frn == 26:                 # I048/SP Special Purpose Field
            pos = _cat48__skip_len_field(data, pos)
        elif frn == 27:                 # I048/RE Reserved Expansion Field
            pos = _cat48__skip_len_field(data, pos)
        else: break

    if "icao24" not in track:
        sac = track.get("sac", 0); sic = track.get("sic", 0)
        track["radar_id"] = "CAT48-{:03d}-{:03d}-{:04d}".format(
            sac, sic, track.get("track_num", 0))
    return track, pos

def _cat48__pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, _cat48_TOPIC_048, track, AsterixCat48Track, zenoh)
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

def _cat48__make_cat048_handler(pub, site):
    # site = [lat, lon] — shared mutable reference; updated live from CAT-34 I034/120
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = _cat48_decode_cat048_record(data, pos, site[0], site[1])
            if len(track) > 2:
                if verbose and "lat_deg" not in track:
                    ident = track.get("icao24") or track.get("radar_id") or "PSR"
                    print("cat48 {} no-position (awaiting I034/120 or set --radar-lat/--radar-lon)".format(
                        ident), flush=True)
                _cat48__pub(pub, track, "cat48", verbose)
    return _h

def _cat48__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat48__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat48__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat48__raw_frame_payload(bytes(sample.payload), category)
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

def _cat48__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat48__process_stream(_cat48_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat48__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat48__netbird_ip()
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
                target=_cat48__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat48__process_stream(_cat48_iter_frames_udp(sock), handlers, verbose)



def _cat48_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-048 Ed.1.32 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT48_PORT", "50048") or 50048))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT48_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT48_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT48_INPUT_TOPIC", _cat48_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat48__env_float("CAT48_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat48__env_float("CAT48_RADAR_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT48_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    if None in site: print("INFO: set CAT48_RADAR_LAT/LON for local polar positions", flush=True)
    session = zenoh.open(_cat48_make_config())
    handler = _cat48__make_cat048_handler(session, site)
    try:
        if args.zenoh_raw: _cat48__run_zenoh_raw(session, args.input_topic, _cat48_CAT_048, handler, args.verbose)
        else: _cat48__run_inbound(args.port, args.tcp, "CAT-48 Ed.1.32", {_cat48_CAT_048: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat048_record = _cat48_decode_cat048_record


# ==========================================================================
# CAT-062
# ==========================================================================

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
from protocols.protobuf_codec import publish_dual


_cat62_ORG       = os.environ.get("PARTNER_NAMESPACE", "")

_cat62_TOPIC_ROOT = topic_root()

_cat62_HERE      = os.path.dirname(os.path.abspath(__file__))

_cat62__CERT_DIR = os.environ.get("EFDI_CERT_DIR", _cat62_HERE)

_cat62__ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

_cat62_TOPIC_062    = "{}/air/asterix/cat62/unknown/aircraft/tracks/v1".format(_cat62_TOPIC_ROOT)
_cat62_RAW_INPUT_TOPIC = "{}/raw/asterix/cat62".format(_cat62_TOPIC_ROOT)

_cat62_CAT_062 = 0x3E

_cat62_RECONNECT_DELAY_S = 5.0
_cat62_ZENOH_RETRY_S = 5.0

_cat62__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat62__EMERGENCY_CODES = {
    0: None,
    1: "GENERAL EMERGENCY",
    2: "LIFEGUARD/MEDICAL",
    3: "MIN FUEL",
    4: "NO COMMS",
    5: "UNLAWFUL INTERFERENCE",
    6: "DOWNED AIRCRAFT",
}

_cat62__WTC_MAP = {76: "L", 77: "M", 72: "H", 74: "J"}

def _cat62_make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_cat62__ENDPOINT]))
    apply_zenoh_auth(conf)
    if _cat62__ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_cat62__CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_cat62__CERT_DIR, _cat62_ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_cat62__CERT_DIR, _cat62_ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf

def _cat62__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat62_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat62__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat62__recv_exact(sock, length - 3)
        yield cat, data

def _cat62_iter_frames_udp(sock: socket.socket):
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

def _cat62_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat62__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat62__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat62__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat62__s24(b: bytes) -> int:
    raw = int.from_bytes(b, "big")
    return raw - (1 << 24) if raw & (1 << 23) else raw

def _cat62__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat62__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat62__decode_bds50(mb: bytes) -> dict:
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

def _cat62__decode_bds60(mb: bytes) -> dict:
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

def _cat62__gillham_to_ft(code: int) -> int | None:
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

def _cat62__decode_bds40(mb: bytes) -> dict:
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

def _cat62__decode_bds30(mb: bytes) -> dict:
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

def _cat62__decode_i062_380(data: bytes, pos: int) -> tuple[dict, int]:
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
        out["callsign"] = _cat62__decode_callsign(data[pos:pos+6]); pos += 6
    # Sub 03: Roll Angle (2 bytes, s16, 45/512 deg)
    if psf & 0x20:
        if pos + 2 > len(data): return out, pos
        out["roll_deg"] = round(_cat62__s16(data[pos:pos+2]) * 45.0 / 512.0, 1); pos += 2
    # Sub 04: Track Angle (2 bytes, u16, 360/65536)
    if psf & 0x10:
        if pos + 2 > len(data): return out, pos
        out["true_track_deg"] = round(_cat62__u16(data[pos:pos+2]) * 360.0 / 65536.0, 2); pos += 2
    # Sub 05: Airspeed (2 bytes, IM + 15-bit)
    if psf & 0x08:
        if pos + 2 > len(data): return out, pos
        w = _cat62__u16(data[pos:pos+2])
        im = (w >> 15) & 1; val = w & 0x7FFF
        if im: out["mach"]   = round(val * 2.0 / (2**14), 3)
        else:  out["ias_kt"] = round(val * 3600.0 / 16384.0, 1)
        pos += 2
    # Sub 06: TAS (2 bytes, u16, 1 kt/LSB)
    if psf & 0x04:
        if pos + 2 > len(data): return out, pos
        w = _cat62__u16(data[pos:pos+2]) & 0x7FFF
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
        s = _cat62__EMERGENCY_CODES.get(ec)
        if s: out["emergency_str"] = s
    # Sub 09: Met (wind 2B + dir 2B + temp 2B + turb 2B = 8 bytes)
    if psf2 & 0x40:
        if pos + 8 > len(data): return out, pos
        out["wind_speed_kt"] = round(_cat62__u16(data[pos:pos+2]) * 0.5, 1)
        out["wind_dir_deg"]  = round(_cat62__u16(data[pos+2:pos+4]) * 360.0 / 65536.0, 1)
        out["temp_c"]        = round(_cat62__s16(data[pos+4:pos+6]) * 0.25, 1)
        pos += 8
    # Sub 10: ACAS RA (7 bytes, BDS 3,0)
    if psf2 & 0x20:
        if pos + 7 > len(data): return out, pos
        out.update(_cat62__decode_bds30(data[pos:pos+7])); pos += 7
    # Sub 11: Barometric Alt (2 bytes, s16, 0.25 FL)
    if psf2 & 0x10:
        if pos + 2 > len(data): return out, pos
        out["alt_baro_ft"] = round(_cat62__s16(data[pos:pos+2]) * 0.25 * 100); pos += 2
    # Sub 12: Mode-C code (2 bytes, Gillham)
    if psf2 & 0x08:
        if pos + 2 > len(data): return out, pos
        alt = _cat62__gillham_to_ft(_cat62__u16(data[pos:pos+2]))
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
            if   b1 == 3 and b2 == 0: out.update(_cat62__decode_bds30(mb))
            elif b1 == 4 and b2 == 0: out.update(_cat62__decode_bds40(mb))
            elif b1 == 5 and b2 == 0: out.update(_cat62__decode_bds50(mb))
            elif b1 == 6 and b2 == 0: out.update(_cat62__decode_bds60(mb))
    # Consume any further FX extension bytes
    while psf2 & 0x01:
        if pos >= len(data): break
        psf2 = data[pos]; pos += 1
    return out, pos

def _cat62__decode_i062_390(data: bytes, pos: int) -> tuple[dict, int]:
    """I062/390 Flight Plan Data — PSF-gated sub-fields."""
    out: dict = {}
    if pos >= len(data):
        return out, pos
    psf = data[pos]; pos += 1
    # CS: 1B quality + 6B callsign
    if psf & 0x80:
        if pos + 7 > len(data): return out, pos
        pos += 1
        out["fp_callsign"] = _cat62__decode_callsign(data[pos:pos+6]); pos += 6
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
        out["wake_turb_cat"] = _cat62__WTC_MAP.get(data[pos], chr(data[pos])); pos += 1
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
        out["cleared_fl"] = round(_cat62__s16(data[pos:pos+2]) * 0.25, 0); pos += 2
    # CTL: Current Cleared Flight Level (2 bytes, u16, 0.25 FL)
    if psf2 & 0x40:
        if pos + 2 > len(data): return out, pos
        out["current_fl"] = round(_cat62__u16(data[pos:pos+2]) * 0.25, 0); pos += 2
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

def _cat62_decode_cat62_record(data: bytes, pos: int):
    """Decode one CAT-062 system track record. Returns (track_dict, new_pos)."""
    fspec, pos = _cat62_parse_fspec(data, pos)
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
            track["lat_deg"] = round(_cat62__s32(data[pos:pos + 4]) * scale, 6)
            track["lon_deg"] = round(_cat62__s32(data[pos + 4:pos + 8]) * scale, 6)
            pos += 8
        elif frn == 4:                  # I062/100  Cartesian position (0.5 m/LSB)
            if pos + 4 > len(data): return track, len(data)
            track["x_m"] = _cat62__s16(data[pos:pos + 2]) * 0.5
            track["y_m"] = _cat62__s16(data[pos + 2:pos + 4]) * 0.5
            pos += 4
        elif frn == 5:                  # I062/185  Cartesian velocity (0.25 m/s/LSB)
            if pos + 4 > len(data): return track, len(data)
            vx = _cat62__s16(data[pos:pos + 2]) * 0.25
            vy = _cat62__s16(data[pos + 2:pos + 4]) * 0.25
            track["vx_ms"] = vx; track["vy_ms"] = vy
            track["speed_ms"]    = round(math.hypot(vx, vy), 2)
            track["heading_deg"] = round(math.degrees(math.atan2(vx, vy)) % 360.0, 2)
            pos += 4
        elif frn == 6:                  # I062/210  Acceleration (0.25 m/s²/LSB)
            if pos + 4 > len(data): return track, len(data)
            track["ax_ms2"] = _cat62__s16(data[pos:pos + 2]) * 0.25
            track["ay_ms2"] = _cat62__s16(data[pos + 2:pos + 4]) * 0.25
            pos += 4
        elif frn == 7:                  # I062/060  Mode-3/A
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_cat62__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 8:                  # I062/040  Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat62__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
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
            track["measured_alt_ft"] = _cat62__s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 14:                 # I062/130  Calculated Altitude
            if pos + 2 > len(data): return track, len(data)
            track["calc_alt_ft"] = _cat62__s16(data[pos:pos + 2]) * 25; pos += 2
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
            extra, pos = _cat62__decode_i062_380(data, pos)
            track.update(extra)
        elif frn == 16:                 # I062/390  Flight Plan Data
            extra, pos = _cat62__decode_i062_390(data, pos)
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
                    track["pos_accuracy_lat_m"] = round(_cat62__u16(data[pos:pos+2]) * 0.5, 1)
                    track["pos_accuracy_lon_m"] = round(_cat62__u16(data[pos+2:pos+4]) * 0.5, 1)
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
                track["meas_range_nm"] = round(_cat62__u16(data[pos:pos+2]) / 256.0, 3)
                track["meas_az_deg"]   = round(_cat62__u16(data[pos+2:pos+4]) * 360.0 / 65536.0, 3)
                pos += 4
            if psf & 0x20:              # measured Mode-C height (Gillham)
                if pos + 2 > len(data): return track, len(data)
                alt = _cat62__gillham_to_ft(_cat62__u16(data[pos:pos+2]))
                if alt is not None: track["meas_alt_ft"] = alt
                pos += 2
            if psf & 0x10:              # MDC: Measured Mode C code + confidence (4 bytes)
                if pos + 4 > len(data): return track, len(data)
                alt = _cat62__gillham_to_ft(_cat62__u16(data[pos:pos+2]))
                if alt is not None: track.setdefault("meas_mode_c_ft", alt)
                pos += 4
            if psf & 0x08:              # MDA: Measured Mode 3/A code (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track.setdefault("squawk", "{:04o}".format(_cat62__u16(data[pos:pos+2]) & 0x0FFF))
                pos += 2
            if psf & 0x04: pos += 4    # MD5: Mode 5 code + confidence (4 bytes, skip)
            if psf & 0x02:              # MD1: Mode 1 code + quality (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track.setdefault("mode1", "{:02o}".format(_cat62__u16(data[pos:pos+2]) & 0x3F))
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
                track["mode1"] = "{:02o}".format(_cat62__u16(data[pos:pos+2]) & 0x3F); pos += 2
            if psf & 0x08:              # Mode 2 code (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track["mode2"] = "{:04o}".format(_cat62__u16(data[pos:pos+2]) & 0x0FFF); pos += 2
            if psf & 0x04:              # Mode 3/A (2 bytes)
                if pos + 2 > len(data): return track, len(data)
                track.setdefault("squawk", "{:04o}".format(_cat62__u16(data[pos:pos+2]) & 0x0FFF)); pos += 2
            if psf & 0x02:              # Mode C height (2 bytes, Gillham)
                if pos + 2 > len(data): return track, len(data)
                alt = _cat62__gillham_to_ft(_cat62__u16(data[pos:pos+2]))
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

def _cat62__pub(pub, track: dict, label: str, verbose: bool, topic: str = _cat62_TOPIC_062):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(
        pub,
        topic,
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

def _cat62__presence(data: bytes, pos: int, maximum: int) -> tuple[list[bool], int]:
    present: list[bool] = []
    for _ in range(maximum):
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 1:
            return present, pos
    if data[pos - 1] & 1:
        raise ValueError("unsupported ASTERIX compound presence extension")
    return present, pos


def _cat62__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data): return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat62__decode_380_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 4)
    fixed = (3, 6, 2, 2, 2, 2, 2, 1, None, 2, 2, 7, 2, 2,
             2, 2, 2, 2, 1, None, 1, 6, 2, 1, None, 2, 2, 2)
    for index, size in enumerate(fixed):
        if index >= len(flags) or not flags[index]: continue
        if index == 8:                  # trajectory intent: REP + 15 octets each
            if pos >= len(data): return out, len(data)
            size = 1 + data[pos] * 15
        elif index == 19:               # meteorological data, compound
            met_flags, payload_pos = _cat62__presence(data, pos, 1)
            pos = payload_pos
            for met_index, met_size in enumerate((2, 2, 2, 1)):
                if met_flags[met_index]:
                    if pos + met_size > len(data): return out, len(data)
                    if met_index == 0: out["wind_speed_kt"] = _cat62__u16(data[pos:pos + 2])
                    elif met_index == 1: out["wind_dir_deg"] = _cat62__u16(data[pos:pos + 2])
                    elif met_index == 2: out["temp_c"] = _cat62__s16(data[pos:pos + 2]) * 0.25
                    else: out["turbulence"] = data[pos]
                    pos += met_size
            continue
        elif index == 24:               # BDS data: REP + 8 octets each
            if pos >= len(data): return out, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): return out, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = (bds >> 4, bds & 15)
                if code == (3, 0): out.update(_cat62__decode_bds30(mb))
                elif code == (4, 0): out.update(_cat62__decode_bds40(mb))
                elif code == (5, 0): out.update(_cat62__decode_bds50(mb))
                elif code == (6, 0): out.update(_cat62__decode_bds60(mb))
            continue
        assert size is not None
        if pos + size > len(data): return out, len(data)
        raw = data[pos:pos + size]
        if index == 0: out["icao24"] = raw.hex()
        elif index == 1: out["callsign"] = _cat62__decode_callsign(raw)
        elif index == 2: out["mag_hdg_deg"] = round(_cat62__u16(raw) * 360 / 65536, 3)
        elif index == 3:
            w = _cat62__u16(raw)
            if w & 0x8000: out["mach"] = round((w & 0x7FFF) * 0.001, 3)
            else: out["ias_kt"] = round((w & 0x7FFF) * 3600 / 16384, 2)
        elif index == 4: out["tas_kt"] = _cat62__u16(raw) & 0x7FFF
        elif index in (5, 6):
            w = _cat62__u16(raw); value = w & 0x1FFF
            if value & 0x1000: value -= 0x2000
            out["selected_alt_ft" if index == 5 else "final_alt_ft"] = value * 25
        elif index in (12, 13):
            w = _cat62__u16(raw); value = w & 0x7FFF
            if value & 0x4000: value -= 0x8000
            out["baro_vr_fpm" if index == 12 else "geo_vr_fpm"] = value * 6.25
        elif index == 14: out["roll_deg"] = round(_cat62__s16(raw) * 0.01, 2)
        elif index == 15:
            value = _cat62__u16(raw) & 0x03FF
            if value & 0x0200: value -= 0x0400
            out["track_angle_rate_degs"] = round(value / 32, 3)
        elif index == 16: out["heading_deg"] = round(_cat62__u16(raw) * 360 / 65536, 3)
        elif index == 17: out["speed_ms"] = round(_cat62__s16(raw) * 2**-14 * 1852, 3)
        elif index == 20: out["emitter_category"] = raw[0]
        elif index == 21:
            scale = 180 / 2**23
            out["aircraft_lat_deg"] = round(_cat62__s24(raw[:3]) * scale, 7)
            out["aircraft_lon_deg"] = round(_cat62__s24(raw[3:]) * scale, 7)
        elif index == 22: out["aircraft_geo_alt_ft"] = _cat62__s16(raw) * 6.25
        elif index == 25: out["ias_kt"] = _cat62__u16(raw)
        elif index == 26: out["mach"] = round(_cat62__u16(raw) * 0.008, 3)
        elif index == 27: out["baro_setting_mb"] = 800 + (_cat62__u16(raw) & 0x0FFF) * 0.1
        pos += size
    return out, pos


def _cat62__skip_390_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 3)
    sizes = (2, 7, 4, 1, 4, 1, 4, 4, 3, 2, 2, None, 6, 1, 7, 7, 2, 7)
    names = (None, "flight_plan_callsign", None, None, "aircraft_type", "wake_turb_cat",
             "departure_icao", "destination_icao", "runway", None, None, None,
             "aircraft_stand", None, "sid", "star", None, None)
    for index, size in enumerate(sizes):
        if index >= len(flags) or not flags[index]: continue
        if index == 11:
            if pos >= len(data): return out, len(data)
            size = 1 + data[pos] * 4
        assert size is not None
        if pos + size > len(data): return out, len(data)
        name = names[index]
        if name:
            raw = data[pos:pos + size]
            if name == "wake_turb_cat": out[name] = chr(raw[0])
            else: out[name] = raw.decode("ascii", errors="replace").strip("\x00 ")
        if index == 9: out["cleared_fl"] = _cat62__s16(data[pos:pos + 2]) * 0.25
        pos += size
    return out, pos


def _cat62__decode_500_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 2)
    sizes = (4, 2, 4, 1, 1, 2, 2, 1)
    for index, size in enumerate(sizes):
        if index >= len(flags) or not flags[index]: continue
        if pos + size > len(data): return out, len(data)
        if index == 0:
            out["pos_accuracy_x_m"] = _cat62__u16(data[pos:pos + 2]) * 0.5
            out["pos_accuracy_y_m"] = _cat62__u16(data[pos + 2:pos + 4]) * 0.5
        elif index == 3: out["geo_alt_accuracy_ft"] = data[pos] * 6.25
        elif index == 4: out["baro_alt_accuracy_ft"] = data[pos] * 25
        pos += size
    return out, pos


def _cat62__decode_340_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 1)
    sizes = (2, 4, 2, 4, 2, 1)
    for index, size in enumerate(sizes):
        if index >= len(flags) or not flags[index]: continue
        if pos + size > len(data): return out, len(data)
        if index == 0: out["measured_by"] = "{}/{}".format(data[pos], data[pos + 1])
        elif index == 1:
            out["meas_range_nm"] = round(_cat62__u16(data[pos:pos + 2]) / 256, 3)
            out["meas_az_deg"] = round(_cat62__u16(data[pos + 2:pos + 4]) * 360 / 65536, 3)
        elif index == 2: out["meas_alt_ft"] = _cat62__s16(data[pos:pos + 2]) * 25
        elif index == 4: out["squawk"] = "{:04o}".format(_cat62__u16(data[pos:pos + 2]) & 0x0FFF)
        pos += size
    return out, pos


def _cat62_decode_cat62_record(data: bytes, pos: int):
    """Decode one EUROCONTROL CAT-062 Edition 1.21 system-track record."""
    fspec, pos = _cat62_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-62 Ed.1.21"}
    for frn, present in enumerate(fspec):
        if not present: continue
        if frn == 0:
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # reserved FRN 2; no bytes
            continue
        elif frn == 2:
            if pos >= len(data): return track, len(data)
            track["service_id"] = data[pos]; pos += 1
        elif frn == 3:
            if pos + 3 > len(data): return track, len(data)
            track["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128; pos += 3
        elif frn == 4:
            if pos + 8 > len(data): return track, len(data)
            scale = 180 / 2**25
            track["lat_deg"] = round(_cat62__s32(data[pos:pos + 4]) * scale, 7)
            track["lon_deg"] = round(_cat62__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
        elif frn == 5:
            if pos + 6 > len(data): return track, len(data)
            track["x_m"] = _cat62__s24(data[pos:pos + 3]) * 0.5
            track["y_m"] = _cat62__s24(data[pos + 3:pos + 6]) * 0.5; pos += 6
        elif frn == 6:
            if pos + 4 > len(data): return track, len(data)
            vx = _cat62__s16(data[pos:pos + 2]) * 0.25; vy = _cat62__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
            track["vx_ms"], track["vy_ms"] = vx, vy
            track["speed_ms"] = round(math.hypot(vx, vy), 2)
            if vx or vy: track["heading_deg"] = round(math.degrees(math.atan2(vx, vy)) % 360, 2)
        elif frn == 7:
            if pos + 2 > len(data): return track, len(data)
            track["ax_ms2"] = struct.unpack_from("b", data, pos)[0] * 0.25
            track["ay_ms2"] = struct.unpack_from("b", data, pos + 1)[0] * 0.25; pos += 2
        elif frn == 8:
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_cat62__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 9:
            if pos + 7 > len(data): return track, len(data)
            track["callsign"] = _cat62__decode_callsign(data[pos + 1:pos + 7]); pos += 7
        elif frn == 10:
            try: extra, pos = _cat62__decode_380_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn == 11:
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat62__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 12:                 # track status, FX
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["track_monosensor"] = bool(b & 0x80); track["confirmed"] = not bool(b & 0x02)
            extent = 0
            while b & 1:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1:
                    if b & 0x40: track["_delete"] = True
                    if b & 0x20: track["track_begin"] = True
        elif frn == 13:                 # I062/290
            try: flags, pos = _cat62__presence(data, pos, 2)
            except ValueError: return track, len(data)
            names = ("track", "psr", "ssr", "mode_s", "ads_c", "ads_b_es", "vdl4", "uat", "loop", "mlat")
            for index, name in enumerate(names):
                if index < len(flags) and flags[index]:
                    size = 2 if index == 4 else 1
                    if pos + size > len(data): return track, len(data)
                    track["track_age_{}_s".format(name)] = int.from_bytes(data[pos:pos + size], "big") * 0.25
                    pos += size
        elif frn == 14:
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["lateral_trend"] = ("constant", "right", "left", "undetermined")[(b >> 6) & 3]
            track["speed_trend"] = ("constant", "increasing", "decreasing", "undetermined")[(b >> 4) & 3]
            track["vertical_trend"] = ("level", "climb", "descent", "undetermined")[(b >> 2) & 3]
            if b & 0x02: track["alt_discrepancy"] = True
        elif frn == 15:                 # I062/295, 31 one-byte age fields
            try: flags, pos = _cat62__presence(data, pos, 5)
            except ValueError: return track, len(data)
            count = sum(flags[:31])
            if pos + count > len(data): return track, len(data)
            pos += count
        elif frn in (16, 17, 18, 19):
            if pos + 2 > len(data): return track, len(data)
            w = _cat62__u16(data[pos:pos + 2]); pos += 2
            if frn == 16: track["measured_alt_ft"] = _cat62__s16(w.to_bytes(2, "big")) * 25
            elif frn == 17: track["calc_geo_alt_ft"] = _cat62__s16(w.to_bytes(2, "big")) * 6.25
            elif frn == 18:
                raw = w & 0x7FFF
                if raw & 0x4000: raw -= 0x8000
                track["calc_baro_alt_ft"] = raw * 25; track["qnh_corrected"] = bool(w & 0x8000)
            else: track["vertical_rate_fpm"] = _cat62__s16(w.to_bytes(2, "big")) * 6.25
        elif frn == 20:
            try: extra, pos = _cat62__skip_390_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn == 21:                 # target dimensions FX
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1; track["target_length_m"] = b >> 1
            extent = 0
            while b & 1:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1: track["target_orientation_deg"] = round((b >> 1) * 360 / 128, 2)
                elif extent == 2: track["target_width_m"] = b >> 1
        elif frn == 22:
            if pos >= len(data): return track, len(data)
            track["fleet_id"] = data[pos]; pos += 1
        elif frn == 23:                 # I062/110
            try: flags, pos = _cat62__presence(data, pos, 1)
            except ValueError: return track, len(data)
            for index, size in enumerate((1, 4, 6, 2, 2, 1, 1)):
                if flags[index]:
                    if pos + size > len(data): return track, len(data)
                    if index == 4: track["mode1"] = "{:02o}".format(_cat62__u16(data[pos:pos + 2]) & 0x3F)
                    pos += size
        elif frn == 24:
            if pos + 2 > len(data): return track, len(data)
            track["mode2"] = "{:04o}".format(_cat62__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 25:                 # extendible 3-octet composed track numbers
            while True:
                if pos + 3 > len(data): return track, len(data)
                b = data[pos + 2]; pos += 3
                if not b & 1: break
        elif frn == 26:
            try: extra, pos = _cat62__decode_500_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn == 27:
            try: extra, pos = _cat62__decode_340_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn in (33, 34):
            pos = _cat62__skip_len_field(data, pos)
        elif frn >= 28:
            continue
        else:
            break
    return track, pos


def _cat62__make_cat062_handler(pub, topic: str = _cat62_TOPIC_062):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = _cat62_decode_cat62_record(data, pos)
            if len(track) > 2:
                _cat62__pub(pub, track, "cat62", verbose, topic)
    return _h

def _cat62__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat62__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat62__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat62__raw_frame_payload(bytes(sample.payload), category)
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

def _cat62__run_cat62(host: str, port: int, udp: bool, handler, verbose: bool):
    """CAT-062: outbound TCP connect with auto-reconnect, or inbound UDP."""
    if udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-62 UDP on 0.0.0.0:{}".format(port), flush=True)
        _cat62__process_stream(_cat62_iter_frames_udp(sock), {_cat62_CAT_062: handler}, verbose)
        return
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            print("CAT-62 TCP connected to {}:{}".format(host, port), flush=True)
            _cat62__process_stream(_cat62_iter_frames_tcp(sock), {_cat62_CAT_062: handler}, verbose)
        except (EOFError, ValueError, ConnectionRefusedError, OSError) as exc:
            print("CAT-62 error: {} — reconnecting in {}s".format(
                exc, _cat62_RECONNECT_DELAY_S), flush=True)
            if sock:
                try: sock.close()
                except Exception: pass
        time.sleep(_cat62_RECONNECT_DELAY_S)



def _cat62_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-062 Ed.1.21 -> Zenoh")
    parser.add_argument("--host", default=os.environ.get("CAT62_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT62_PORT", "50062") or 50062))
    parser.add_argument("--udp", action="store_true", default=os.environ.get("CAT62_UDP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT62_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT62_INPUT_TOPIC", _cat62_RAW_INPUT_TOPIC))
    parser.add_argument("--topic", default=_cat62_TOPIC_062)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.host and not args.udp: parser.error("--host is required unless --udp or --zenoh-raw is selected")
    while True:
        try:
            session = zenoh.open(_cat62_make_config())
            break
        except zenoh.ZError as exc:
            print("CAT-62 Zenoh connect failed: {} — retry in {}s".format(exc, _cat62_ZENOH_RETRY_S), flush=True)
            time.sleep(_cat62_ZENOH_RETRY_S)
    handler = _cat62__make_cat062_handler(session, args.topic)
    try:
        if args.zenoh_raw: _cat62__run_zenoh_raw(session, args.input_topic, _cat62_CAT_062, handler, args.verbose)
        else: _cat62__run_cat62(args.host, args.port, args.udp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat62_record = _cat62_decode_cat62_record

# ============================================================================
# Unified launcher and compatibility helpers
# ============================================================================

def _category_uses_raw(wanted: int) -> bool:
    if not os.environ.get("ASTERIX_PORT", "").strip():
        return False
    categories = [item.strip() for item in os.environ.get("ASTERIX_CATEGORIES", "34,48").split(",")]
    return str(wanted) in categories


def _bundle_main() -> None:
    children: list[tuple[str, str, list[str]]] = []
    script = "protocols/vendors/asterix/cat.py"
    if os.environ.get("ASTERIX_PORT", "").strip():
        children.append(("asterix-udp", "bridges/asterix_udp_bridge.py", []))

    for category in (10, 20, 21, 34, 48):
        port = os.environ.get(f"CAT{category}_PORT", "").strip()
        tcp = os.environ.get(f"CAT{category}_TCP", "") == "1"
        args = ["--category", str(category)]
        if _category_uses_raw(category):
            args.append("--zenoh-raw")
            children.append((f"asterix-cat{category}", script, args))
        elif port:
            args.extend(["--port", port])
            if tcp:
                args.append("--tcp")
            children.append((f"asterix-cat{category}", script, args))

    args = ["--category", "62"]
    if _category_uses_raw(62):
        children.append(("asterix-cat62", script, args + ["--zenoh-raw"]))
    elif os.environ.get("CAT62_UDP", "") == "1":
        children.append(("asterix-cat62", script, args + ["--udp", "--port", os.environ.get("CAT62_PORT", "50062")]))
    elif os.environ.get("CAT62_HOST", "").strip() or os.environ.get("RADAR_HOST", "").strip():
        children.append((
            "asterix-cat62",
            script,
            args + [
                "--host",
                os.environ.get("CAT62_HOST", "").strip() or os.environ.get("RADAR_HOST", "").strip(),
                "--port",
                os.environ.get("CAT62_PORT", "").strip() or os.environ.get("RADAR_PORT", "").strip() or "50062",
            ],
        ))

    run_bundle("asterix", children)


def _pop_category_argument() -> int | None:
    for index, argument in enumerate(sys.argv[1:], 1):
        if argument == "--category":
            if index + 1 >= len(sys.argv):
                raise SystemExit("--category requires one of: 10, 20, 21, 34, 48, 62")
            value = sys.argv[index + 1]
            del sys.argv[index:index + 2]
            break
        if argument.startswith("--category="):
            value = argument.split("=", 1)[1]
            del sys.argv[index]
            break
    else:
        return None
    try:
        category = int(value)
    except ValueError as exc:
        raise SystemExit("invalid ASTERIX category: {}".format(value)) from exc
    if category not in _CATEGORY_MAINS:
        raise SystemExit("unsupported ASTERIX category: {}".format(category))
    return category


_CATEGORY_MAINS = {
    10: _cat10_main,
    20: _cat20_main,
    21: _cat21_main,
    34: _cat34_main,
    48: _cat48_main,
    62: _cat62_main,
}


def _raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    """Validate one complete ASTERIX frame and return its record payload."""
    if len(frame) < 3:
        raise ValueError("ASTERIX frame shorter than header")
    declared = int.from_bytes(frame[1:3], "big")
    if frame[0] != expected_category:
        raise ValueError("unexpected ASTERIX category {}".format(frame[0]))
    if declared != len(frame):
        raise ValueError("ASTERIX frame length mismatch")
    return frame[3:]


def main() -> None:
    category = _pop_category_argument()
    if category is None:
        _bundle_main()
    else:
        _CATEGORY_MAINS[category]()


if __name__ == "__main__":
    main()
