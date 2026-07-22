#!/usr/bin/env python3

"""Legacy ASTERIX CAT-020 compatibility protocol; requires the producer ICD."""



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
from protocols.asterix_cat20_pb2 import AsterixCat20Track
from protocols.protobuf_codec import publish_dual


ORG       = os.environ.get("PARTNER_NAMESPACE", "")

TOPIC_ROOT = topic_root()

HERE      = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)

_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

TOPIC_020    = "{}/air/asterix/cat20/civ/aircraft/tracks/v1".format(TOPIC_ROOT)
RAW_INPUT_TOPIC = "{}/raw/asterix/cat20".format(TOPIC_ROOT)

CAT_020 = 0x14

_CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

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
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x10: track["simulated"]      = True   # SIM
            if b & 0x08: track["test_target"]    = True   # TSIM
            if b & 0x04: track["surface_vehicle"]= True   # RAB
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["spi"]        = True   # SPI
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
        elif frn == 13:                 # I020/300  Vehicle Fleet ID (1 byte)
            if pos + 1 > len(data): return track, len(data)
            track["fleet_id"] = data[pos]; pos += 1
        elif frn == 14:                 # I020/310  Pre-programmed Message (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["in_trouble"] = True
            msg_type = (b >> 4) & 0x07
            _MSG310 = ("", "go_around", "rvsm_failed", "tcas_ra_downlink",
                       "emergency", "maneuvering", "", "")
            if msg_type:
                track["preprog_msg"] = _MSG310[msg_type] or "type_{}".format(msg_type)
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
        elif frn == 15:                 # I020/500  Position Accuracy (compound, skip sub-fields)
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80: pos += 6   # DOP matrix: Dx + Dy + Dxy (3 × u16 = 6 bytes)
            if psf & 0x40:            # σ lat/lon (4 bytes each × 2 = 8 bytes, u16 units)
                if pos + 4 <= len(data):
                    track["pos_accuracy_lat_m"] = round(_u16(data[pos:pos+2]) * 0.5, 1)
                    track["pos_accuracy_lon_m"] = round(_u16(data[pos+2:pos+4]) * 0.5, 1)
                pos += 4
            if psf & 0x20: pos += 2   # σ height
            while psf & 0x01:
                if pos >= len(data): break
                psf = data[pos]; pos += 1
        elif frn == 16:                 # I020/400  Contributing Receivers (REP × 2-byte SAC/SIC)
            if pos + 1 > len(data): return track, len(data)
            count = data[pos]; pos += 1
            receivers = []
            for _ in range(count):
                if pos + 2 > len(data): break
                receivers.append("{}/{}".format(data[pos], data[pos+1])); pos += 2
            if receivers: track["mlat_receivers"] = receivers
        elif frn == 17:                 # I020/250  Mode-S MB Data (REP × 8 bytes)
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
        else: break
    return track, pos

def _pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, TOPIC_020, track, AsterixCat20Track, zenoh)
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

def _make_cat020_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            track, pos = decode_cat020_record(data, pos)
            if len(track) > 2:
                _pub(pub, track, "cat20", verbose)
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
    parser = argparse.ArgumentParser(description="ASTERIX CAT-020 legacy profile -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT20_PORT", "50020") or 50020))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT20_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT20_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT20_INPUT_TOPIC", RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT20_PORT is required unless --zenoh-raw is selected")
    print("WARNING: CAT-20 uses a legacy compatibility UAP; confirm the producer ICD", flush=True)
    session = zenoh.open(make_config()); publisher = session.declare_publisher(TOPIC_020)
    handler = _make_cat020_handler(publisher)
    try:
        if args.zenoh_raw: _run_zenoh_raw(session, args.input_topic, CAT_020, handler, args.verbose)
        else: _run_inbound(args.port, args.tcp, "CAT-20 legacy", {CAT_020: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: publisher.undeclare(); session.close()



if __name__ == "__main__":

    main()
