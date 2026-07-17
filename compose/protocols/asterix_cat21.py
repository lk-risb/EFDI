#!/usr/bin/env python3

"""Legacy ASTERIX CAT-021 compatibility protocol; not CAT-021 Edition 2.2+."""



import argparse

import json

import math

import os

import socket

import struct

import threading

import time

import zenoh

from namespace_prefix import prefix


ORG       = os.environ.get("PARTNER_NAMESPACE", "")

TOPIC_ROOT = prefix() + "/" + ORG

HERE      = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)

_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

TOPIC_021    = "{}/air/asterix/cat21/civ/aircraft/tracks/v1".format(TOPIC_ROOT)
RAW_INPUT_TOPIC = "{}/raw/asterix/cat21".format(TOPIC_ROOT)

CAT_021 = 0x15

_CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_EMITTER_CATEGORY = {
    0: "no info",    1: "light",      2: "small",       3: "medium",
    4: "high vortex large", 5: "heavy", 6: "manoeuvrable/high speed",
    10: "glider",    11: "airship",   12: "UAV",        13: "space vehicle",
    14: "emergency vehicle", 15: "service vehicle",    16: "ground obstruction",
}

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

def _s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

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
                track["ias_kt"] = round(val * 3600.0 / 16384.0, 1)
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
                if   bds1 == 3 and bds2 == 0: track.update(_decode_bds30(mb))
                elif bds1 == 4 and bds2 == 0: track.update(_decode_bds40(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_decode_bds60(mb))
        elif frn == 30:                 # I021/260  ACAS Resolution Advisory (7 bytes, BDS 3,0)
            if pos + 7 > len(data): return track, len(data)
            track.update(_decode_bds30(data[pos:pos + 7])); pos += 7
        elif frn == 31:                 # I021/400  Receiver ID (1 byte)
            if pos + 1 > len(data): return track, len(data)
            track["receiver_id"] = data[pos]; pos += 1
        elif frn == 32: pos += 3        # I021/008  ACAS Capability/Operational Status (3 bytes)
        elif frn == 33: pos += 2        # I021/271  Surface Capabilities and Status (2 bytes)
        else: break
    return track, pos

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

def _make_cat021_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat021_record(data, pos)
            if len(track) > 2:
                _pub(pub, track, "cat21", verbose)
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
    parser = argparse.ArgumentParser(description="ASTERIX CAT-021 legacy profile -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT21_PORT", "50021") or 50021))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT21_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT21_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT21_INPUT_TOPIC", RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT21_PORT is required unless --zenoh-raw is selected")
    print("WARNING: CAT-21 uses a legacy pre-2.2 UAP; CAT-21 2.2+ is not supported", flush=True)
    session = zenoh.open(make_config()); publisher = session.declare_publisher(TOPIC_021)
    handler = _make_cat021_handler(publisher)
    try:
        if args.zenoh_raw: _run_zenoh_raw(session, args.input_topic, CAT_021, handler, args.verbose)
        else: _run_inbound(args.port, args.tcp, "CAT-21 legacy", {CAT_021: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: publisher.undeclare(); session.close()



if __name__ == "__main__":

    main()
