#!/usr/bin/env python3

"""EUROCONTROL ASTERIX CAT-010 Edition 1.1 airport surface protocol."""



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


ORG       = os.environ.get("PARTNER_NAMESPACE", "")

TOPIC_ROOT = topic_root()

HERE      = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)

_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

TOPIC_010_AIR = "{}/air/asterix/cat10/civ/aircraft/tracks/v1".format(TOPIC_ROOT)

TOPIC_010_GROUND = "{}/land/asterix/cat10/unknown/vehicle/tracks/v1".format(TOPIC_ROOT)

TOPIC_010_SENSOR = "{}/land/asterix/cat10/neutral/radar/status/v1".format(TOPIC_ROOT)
RAW_INPUT_TOPIC = "{}/raw/asterix/cat10".format(TOPIC_ROOT)

CAT_010 = 0x0A

_CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_CAT010_MESSAGE_TYPES = {
    1: "target_report",
    2: "start_update_cycle",
    3: "periodic_status",
    4: "event_status",
}

_CAT010_SENSOR_TYPES = {
    0: "ssr_mlat", 1: "mode_s_mlat", 2: "adsb", 3: "psr",
    4: "magnetic_loop", 5: "hf_mlat", 6: "undefined", 7: "other",
}

_CAT010_TARGET_TYPES = {0: "undetermined", 1: "aircraft", 2: "ground_vehicle", 3: "helicopter"}

_CAT010_FLEETS = {
    0: "unknown", 1: "atc_maintenance", 2: "airport_maintenance", 3: "fire",
    4: "bird_scarer", 5: "snow_plough", 6: "runway_sweeper", 7: "emergency",
    8: "police", 9: "bus", 10: "tug", 11: "grass_cutter", 12: "fuel",
    13: "baggage", 14: "catering", 15: "aircraft_maintenance", 16: "follow_me",
}

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

def _cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_m: float, y_m: float):
    """Local CAT-010 X=east/Y=north metres to WGS-84 for short airport ranges."""
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon

def _signed_bits(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value

def decode_cat010_record(data: bytes, pos: int, site_lat=None, site_lon=None):
    """Decode exactly the public EUROCONTROL CAT-010 Edition 1.1 UAP."""
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-10 Ed.1.1"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I010/010 SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I010/000 Message Type
            if pos >= len(data): return track, len(data)
            track["msg_type"] = _CAT010_MESSAGE_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I010/020 Target Report Descriptor
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["sensor_type"] = _CAT010_SENSOR_TYPES[(b >> 5) & 0x07]
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
                track["target_type"] = _CAT010_TARGET_TYPES[target_type]
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
            track["lat_deg"] = round(_s32(data[pos:pos + 4]) * 180.0 / 2**31, 7)
            track["lon_deg"] = round(_s32(data[pos + 4:pos + 8]) * 180.0 / 2**31, 7); pos += 8
        elif frn == 5:                  # I010/040 Polar Position
            if pos + 4 > len(data): return track, len(data)
            range_m = _u16(data[pos:pos + 2]); azimuth = _u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0
            track["range_m"] = range_m; track["azimuth_deg"] = round(azimuth, 3); pos += 4
            if site_lat is not None and site_lon is not None:
                lat, lon = _polar_to_wgs84(site_lat, site_lon, range_m / 1852.0, azimuth)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 6:                  # I010/042 Cartesian Position
            if pos + 4 > len(data): return track, len(data)
            x_m, y_m = _s16(data[pos:pos + 2]), _s16(data[pos + 2:pos + 4]); pos += 4
            track["cart_x_m"], track["cart_y_m"] = x_m, y_m
            if site_lat is not None and site_lon is not None:
                lat, lon = _cartesian_to_wgs84(site_lat, site_lon, x_m, y_m)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 7:                  # I010/200 Polar Velocity
            if pos + 4 > len(data): return track, len(data)
            track["speed_ms"] = round(_u16(data[pos:pos + 2]) * 1852.0 / 16384.0, 2)
            track["heading_deg"] = round(_u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0, 2); pos += 4
        elif frn == 8:                  # I010/202 Cartesian Velocity
            if pos + 4 > len(data): return track, len(data)
            vx = _s16(data[pos:pos + 2]) * 0.25; vy = _s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
            track["velocity_east_ms"] = round(vx, 2); track["velocity_north_ms"] = round(vy, 2)
            track.setdefault("speed_ms", round(math.hypot(vx, vy), 2))
            if vx or vy: track.setdefault("heading_deg", round((math.degrees(math.atan2(vx, vy)) + 360) % 360, 2))
        elif frn == 9:                  # I010/161 Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
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
            track["squawk"] = "{:04o}".format(_u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 12:                 # I010/220 Target Address
            if pos + 3 > len(data): return track, len(data)
            track["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif frn == 13:                 # I010/245 Target Identification
            if pos + 7 > len(data): return track, len(data)
            callsign = _decode_callsign(data[pos + 1:pos + 7]); pos += 7
            if callsign: track["callsign"] = callsign
        elif frn == 14:                 # I010/250 Mode-S MB Data
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): return track, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = ((bds >> 4) & 0x0F, bds & 0x0F)
                if code == (3, 0): track.update(_decode_bds30(mb))
                elif code == (4, 0): track.update(_decode_bds40(mb))
                elif code == (5, 0): track.update(_decode_bds50(mb))
                elif code == (6, 0): track.update(_decode_bds60(mb))
        elif frn == 15:                 # I010/300 Vehicle Fleet ID
            if pos >= len(data): return track, len(data)
            track["vehicle_fleet"] = _CAT010_FLEETS.get(data[pos], "fleet_{}".format(data[pos])); pos += 1
            track.setdefault("target_type", "ground_vehicle"); track["on_ground"] = True
        elif frn == 16:                 # I010/090 Flight Level
            if pos + 2 > len(data): return track, len(data)
            raw = _u16(data[pos:pos + 2]); pos += 2
            fl = _signed_bits(raw & 0x3FFF, 14) * 0.25
            track["flight_level"] = round(fl, 2); track["baro_alt_m"] = round(fl * 100 * 0.3048, 2)
        elif frn == 17:                 # I010/091 Measured Height
            if pos + 2 > len(data): return track, len(data)
            feet = _s16(data[pos:pos + 2]) * 6.25; pos += 2
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
            track["sigma_xy_m2"] = _s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
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
            pos = _skip_len_field(data, pos)
        else:
            return track, len(data)
    return track, pos

def _make_cat010_handler(session, site, site_name):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = decode_cat010_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if track.get("msg_type") == "target_report":
                if "lat_deg" not in track or "lon_deg" not in track:
                    if verbose:
                        print("cat10 target without map position; configure CAT10_SITE_LAT/LON for local coordinates", flush=True)
                    continue
                ground = track.get("target_type") == "ground_vehicle" or "vehicle_fleet" in track
                topic = TOPIC_010_GROUND if ground else TOPIC_010_AIR
                session.put(
                    topic, json.dumps(track).encode(),
                    encoding=zenoh.Encoding.APPLICATION_JSON,
                )
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
                session.put(
                    TOPIC_010_SENSOR, json.dumps(status).encode(),
                    encoding=zenoh.Encoding.APPLICATION_JSON,
                )
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
    parser = argparse.ArgumentParser(description="ASTERIX CAT-010 Ed.1.1 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT10_PORT", "50010") or 50010))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT10_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT10_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT10_INPUT_TOPIC", RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_env_float("CAT10_SITE_LAT"))
    parser.add_argument("--site-lon", type=float, default=_env_float("CAT10_SITE_LON"))
    parser.add_argument("--site-name", default=os.environ.get("CAT10_SITE_NAME", ""))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT10_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = zenoh.open(make_config())
    try:
        print("Zenoh CAT-10 topics:", TOPIC_010_AIR, TOPIC_010_GROUND, TOPIC_010_SENSOR, flush=True)
        handler = _make_cat010_handler(session, site, args.site_name)
        if args.zenoh_raw:
            _run_zenoh_raw(session, args.input_topic, CAT_010, handler, args.verbose)
        else:
            _run_inbound(args.port, args.tcp, "CAT-10 Ed.1.1", {CAT_010: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()



if __name__ == "__main__":

    main()
