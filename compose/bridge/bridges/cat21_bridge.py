#!/usr/bin/env python3
"""cat21_bridge.py — ASTERIX CAT-021 ADS-B Target Reports → Zenoh bridge.

CAT-021 (Edition 2.4) carries ADS-B (Automatic Dependent Surveillance–Broadcast)
data received by a ground station from Mode-S transponders.  Unlike CAT-048 (which
gives radar-relative polar plots), CAT-021 contains WGS-84 positions directly —
no antenna position needed.

Key fields decoded:
    I021/010  Data Source Identifier (SAC/SIC)
    I021/030  Time of Day
    I021/130  WGS-84 Position   (3+3 bytes, 180/2^23 deg/LSB)
    I021/080  Target Address    ICAO 24-bit
    I021/140  Geometric Altitude (6.25 ft/LSB signed)
    I021/145  Barometric FL      (1/4 FL/LSB signed → alt_baro_ft)
    I021/160  Airborne Ground Vector (GS + track angle)
    I021/170  Target Identification (callsign, 6-bit packed)
    I021/070  Mode-3/A squawk
    I021/131  High-Res WGS-84   (4+4 bytes, 180/2^31, overrides /130)

Zenoh topic:
    <ORG>/air/asterix/cat21/civ/aircraft/tracks/v1
    → cot_layer.py "air/**/civ/aircraft/**" → civilian air (green / red if hostile)

Run:
    venv/bin/python3 cat21_bridge.py --port 30021
    venv/bin/python3 cat21_bridge.py --port 30021 --tcp

Activate in .env:
    CAT21_PORT=30021
"""

import argparse
import json
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

TOPIC   = "{}/air/asterix/cat21/civ/aircraft/tracks/v1".format(ORG)
CAT_021 = 0x15   # decimal 21


# ---------------------------------------------------------------------------
# Zenoh
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ASTERIX framing (identical to asterix_bridge)
# ---------------------------------------------------------------------------

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def iter_frames_udp(sock):
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            yield cat, pkt[offset + 3:offset + length]
            offset += length


def iter_frames_tcp(sock):
    while True:
        header = _recv_exact(sock, 3)
        cat = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            continue
        yield cat, _recv_exact(sock, length - 3)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _u16(b): return struct.unpack(">H", b)[0]
def _s16(b): return struct.unpack(">h", b)[0]
def _s32(b): return struct.unpack(">i", b)[0]


def _s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw


def _skip_fx_field(data, pos):
    while pos < len(data):
        b = data[pos]; pos += 1
        if not (b & 0x01):
            break
    return pos


def parse_fspec(data, pos):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _decode_callsign(raw: bytes) -> str:
    ALPHA = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
    bits = int.from_bytes(raw, "big")
    chars = []
    for i in range(7, -1, -1):
        idx = (bits >> (i * 6)) & 0x3F
        chars.append(ALPHA[idx])
    return "".join(chars).strip()


# ---------------------------------------------------------------------------
# CAT-021 decoder  (Edition 2.4)
# ---------------------------------------------------------------------------
#
# FRN  Item       Size   Description
#  0   I021/010   2      Data Source Identifier (SAC/SIC)
#  1   I021/040   FX     Target Report Descriptor
#  2   I021/030   3      Time of Day (1/128 s)
#  3   I021/130   6      Position WGS-84  (lat 3B + lon 3B, 180/2^23)
#  4   I021/080   3      Target Address (ICAO 24-bit)
#  5   I021/140   2      Geometric Altitude (6.25 ft/LSB, signed)
#  6   I021/090   2      Figure of Merit / Accuracy
#  7   I021/210   FX     Link Technology Indicator
# --- FSPEC byte 2 ---
#  8   I021/230   2      Roll Angle
#  9   I021/145   2      Barometric Flight Level (1/4 FL, signed → *25 ft)
# 10   I021/150   2      Air Speed (IAS/Mach)
# 11   I021/151   2      True Airspeed
# 12   I021/152   2      Magnetic Heading
# 13   I021/155   2      Barometric Vertical Rate
# 14   I021/157   2      Geometric Vertical Rate
# --- FSPEC byte 3 ---
# 15   I021/160   4      Airborne Ground Vector (GS 2B + TrackAngle 2B)
#                          GS: 15-bit unsigned, LSB = 2^-14 NM/s
#                          TA: 16-bit unsigned, LSB = 360/65536 deg
# 16   I021/165   2      Track Angle Rate
# 17   I021/170   6      Target Identification (callsign, 6-bit packed, 8 chars)
# 18   I021/095   1      Velocity Accuracy
# 19   I021/032   1      Time Accuracy
# 20   I021/200   FX     Target Status
# --- FSPEC byte 4 ---
# 21   I021/020   1      Emitter Category
# 22   I021/220   cmpd   Meteorological Information  ← compound, stop here
# 23   I021/146   2      Selected Altitude
# 24   I021/148   2      Final State Selected Altitude
# 25   I021/110   cmpd   Trajectory Intent            ← compound, stop here
# 26   I021/070   2      Mode-3/A squawk (lower 12 bits)
# 27   I021/131   8      High-Res WGS-84 (lat 4B + lon 4B, 180/2^31)

def decode_cat021_record(data: bytes, pos: int):
    """Decode one CAT-021 record. Returns (track_dict, new_pos)."""
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-21"}

    for frn, present in enumerate(fspec):
        if not present:
            continue

        if frn == 0:                    # I021/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos + 1]; pos += 2

        elif frn == 1:                  # I021/040  Target Report Descriptor (FX)
            pos = _skip_fx_field(data, pos)

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

        elif frn == 6:                  # I021/090  Figure of Merit  2 bytes
            pos += 2

        elif frn == 7:                  # I021/210  Link Technology (FX)
            pos = _skip_fx_field(data, pos)

        elif frn == 8:                  # I021/230  Roll Angle  2 bytes
            pos += 2

        elif frn == 9:                  # I021/145  Barometric FL (1/4 FL × 100 ft)
            if pos + 2 > len(data): return track, len(data)
            track["alt_baro_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2

        elif frn == 10:                 # I021/150  Air Speed  2 bytes
            pos += 2

        elif frn == 11:                 # I021/151  True Airspeed  2 bytes
            pos += 2

        elif frn == 12:                 # I021/152  Magnetic Heading  2 bytes
            pos += 2

        elif frn == 13:                 # I021/155  Barometric Vertical Rate  2 bytes
            pos += 2

        elif frn == 14:                 # I021/157  Geometric Vertical Rate  2 bytes
            pos += 2

        elif frn == 15:                 # I021/160  Ground Vector (GS + TA)
            if pos + 4 > len(data): return track, len(data)
            gs_raw = _u16(data[pos:pos + 2]) & 0x7FFF   # strip RE flag
            ta_raw = _u16(data[pos + 2:pos + 4])
            gs_kt  = gs_raw * (3600.0 / 16384.0)        # 2^-14 NM/s → kt
            track["speed_ms"]    = round(gs_kt * 0.514444, 2)
            track["heading_deg"] = round(ta_raw * 360.0 / 65536.0, 2)
            pos += 4

        elif frn == 16:                 # I021/165  Track Angle Rate  2 bytes
            pos += 2

        elif frn == 17:                 # I021/170  Target Identification (callsign)
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _decode_callsign(data[pos:pos + 6]); pos += 6

        elif frn == 18:                 # I021/095  Velocity Accuracy  1 byte
            pos += 1

        elif frn == 19:                 # I021/032  Time Accuracy  1 byte
            pos += 1

        elif frn == 20:                 # I021/200  Target Status (FX)
            pos = _skip_fx_field(data, pos)

        elif frn == 21:                 # I021/020  Emitter Category  1 byte
            pos += 1

        elif frn == 22:                 # I021/220  Met Info (compound) — stop
            break

        elif frn == 23:                 # I021/146  Selected Altitude  2 bytes
            pos += 2

        elif frn == 24:                 # I021/148  Final State Selected Alt  2 bytes
            pos += 2

        elif frn == 25:                 # I021/110  Trajectory Intent (compound) — stop
            break

        elif frn == 26:                 # I021/070  Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_u16(data[pos:pos + 2]) & 0x0FFF)
            pos += 2

        elif frn == 27:                 # I021/131  High-Res WGS-84 (4+4, 180/2^31)
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / (2 ** 31)
            track["lat_deg"] = round(_s32(data[pos:pos + 4]) * scale, 7)
            track["lon_deg"] = round(_s32(data[pos + 4:pos + 8]) * scale, 7)
            pos += 8

        else:
            break

    return track, pos


# ---------------------------------------------------------------------------
# Stream handler & transport
# ---------------------------------------------------------------------------

def _publish(pub, track: dict, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    pub.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        ident = track.get("icao24") or track.get("callsign") or "?"
        print("PUB cat21 {} lat={} lon={} sq={} alt={}ft".format(
            ident,
            round(track.get("lat_deg", 0), 4),
            round(track.get("lon_deg", 0), 4),
            track.get("squawk", "----"),
            int(track.get("alt_baro_ft") or track.get("alt_geom_ft") or 0),
        ), flush=True)


def _handle(frames, pub, verbose):
    for cat, data in frames:
        if cat != CAT_021:
            if verbose:
                print("SKIP cat=0x{:02x}".format(cat), flush=True)
            continue
        pos = 0
        while pos < len(data):
            track, pos = decode_cat021_record(data, pos)
            if len(track) > 2:
                _publish(pub, track, verbose)


def run_udp(port, pub, verbose):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    ip = _netbird_ip()
    print("CAT-021 UDP listening on 0.0.0.0:{}".format(port), flush=True)
    print("Tell sender: UDP to {}:{}".format(ip, port), flush=True)
    _handle(iter_frames_udp(sock), pub, verbose)


def _tcp_client(conn, addr, pub, verbose):
    print("CAT-021 TCP connected: {}".format(addr), flush=True)
    try:
        _handle(iter_frames_tcp(conn), pub, verbose)
    except EOFError:
        pass
    finally:
        conn.close()
        print("CAT-021 TCP disconnected: {}".format(addr), flush=True)


def run_tcp(port, pub, verbose):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    ip = _netbird_ip()
    print("CAT-021 TCP server on 0.0.0.0:{}".format(port), flush=True)
    print("Tell sender: TCP to {}:{}".format(ip, port), flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_tcp_client, args=(conn, addr, pub, verbose),
                         daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="ASTERIX CAT-021 ADS-B → Zenoh bridge")
    ap.add_argument("--port", type=int, default=30021, help="Listen port (default: 30021)")
    ap.add_argument("--tcp", action="store_true", help="TCP server mode (default: UDP)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    session = zenoh.open(make_config())
    pub = session.declare_publisher(TOPIC)
    print("Zenoh topic:", TOPIC, flush=True)
    try:
        if args.tcp:
            run_tcp(args.port, pub, args.verbose)
        else:
            run_udp(args.port, pub, args.verbose)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
