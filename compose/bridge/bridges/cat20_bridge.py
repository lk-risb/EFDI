#!/usr/bin/env python3
"""cat20_bridge.py — ASTERIX CAT-020 Multilateration Target Reports → Zenoh bridge.

CAT-020 (Edition 1.9) carries MLAT (Multilateration) data — positions computed
by time-difference-of-arrival from a network of ground receivers interrogating
Mode-S transponders.  Like CAT-021, positions are in WGS-84 directly.

Typical use: airport surface movement radar, wide-area MLAT networks, airspace
surveillance without primary radar.  Requires cooperative (transponder-equipped)
targets but gives better coverage in low-angle areas where primary radar is blind.

Key fields decoded:
    I020/010  Data Source Identifier (SAC/SIC)
    I020/140  Time of Day
    I020/041  Position in WGS-84    (4+4 bytes, 180/2^25 deg/LSB)
    I020/042  Cartesian Position     (x 3B + y 3B, 0.5 m/LSB — stored, not mapped)
    I020/070  Mode-3/A squawk
    I020/090  Barometric Flight Level (1/4 FL/LSB → alt_baro_ft)
    I020/220  Aircraft Address       ICAO 24-bit
    I020/245  Target Identification  (1B flags + 6B callsign 6-bit packed)
    I020/105  Geometric Altitude     (6.25 ft/LSB signed)
    I020/110  Height by 3D Radar     (25 ft/LSB signed → alt_3d_ft)

Zenoh topic:
    <ORG>/air/asterix/cat20/civ/aircraft/tracks/v1
    → cot_layer.py "air/**/civ/aircraft/**" → civilian air (green / red if hostile)

Run:
    venv/bin/python3 cat20_bridge.py --port 30020
    venv/bin/python3 cat20_bridge.py --port 30020 --tcp

Activate in .env:
    CAT20_PORT=30020
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

TOPIC   = "{}/air/asterix/cat20/civ/aircraft/tracks/v1".format(ORG)
CAT_020 = 0x14   # decimal 20


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
# ASTERIX framing
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
# CAT-020 decoder  (Edition 1.9)
# ---------------------------------------------------------------------------
#
# FRN  Item       Size   Description
#  0   I020/010   2      Data Source Identifier (SAC/SIC)
#  1   I020/020   FX     Target Report Descriptor
#  2   I020/140   3      Time of Day (1/128 s)
#  3   I020/041   8      Position WGS-84  (lat 4B + lon 4B, 180/2^25)
#  4   I020/042   6      Cartesian Position (x 3B + y 3B, 0.5 m/LSB)
#  5   I020/070   2      Mode-3/A squawk (lower 12 bits)
#  6   I020/090   2      Barometric Flight Level (1/4 FL, signed)
#  7   I020/100   cmpd   Mode-C Code and Confidence  ← compound, stop
#  8   I020/220   3      Aircraft Address (ICAO 24-bit)
#  9   I020/245   7      Target Identification (1B flags + 6B callsign)
# 10   I020/110   2      Height by 3D Radar (25 ft/LSB, signed → alt_3d_ft)
# 11   I020/105   2      Geometric Altitude (6.25 ft/LSB, signed)
# 12   I020/210   cmpd   Track Quality  ← compound, stop
# 13   I020/300   1      Vehicle Fleet ID
# 14   I020/310   FX     Pre-programmed Message
# 15   I020/500   cmpd   Position Accuracy  ← compound, stop
# 16   I020/400   var    Contributing Receivers (variable)
# 17   I020/250   cmpd   Mode-S MB Data  ← compound, stop

def decode_cat020_record(data: bytes, pos: int):
    """Decode one CAT-020 record. Returns (track_dict, new_pos)."""
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
            track["squawk"] = "{:04o}".format(_u16(data[pos:pos + 2]) & 0x0FFF)
            pos += 2

        elif frn == 6:                  # I020/090  Barometric FL (1/4 FL × 100 ft)
            if pos + 2 > len(data): return track, len(data)
            track["alt_baro_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2

        elif frn == 7:                  # I020/100  Mode-C + Confidence (compound)
            break

        elif frn == 8:                  # I020/220  ICAO 24-bit
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["icao24"] = "{:06x}".format(addr); pos += 3

        elif frn == 9:                  # I020/245  Target ID (1B flags + 6B callsign)
            if pos + 7 > len(data): return track, len(data)
            pos += 1                    # skip STI/spare byte
            track["callsign"] = _decode_callsign(data[pos:pos + 6]); pos += 6

        elif frn == 10:                 # I020/110  3D Radar Height (25 ft/LSB)
            if pos + 2 > len(data): return track, len(data)
            track["alt_3d_ft"] = _s16(data[pos:pos + 2]) * 25; pos += 2

        elif frn == 11:                 # I020/105  Geometric Altitude (6.25 ft/LSB)
            if pos + 2 > len(data): return track, len(data)
            track["alt_geom_ft"] = round(_s16(data[pos:pos + 2]) * 6.25); pos += 2

        elif frn == 12:                 # I020/210  Track Quality (compound)
            break

        elif frn == 13:                 # I020/300  Vehicle Fleet ID  1 byte
            pos += 1

        elif frn == 14:                 # I020/310  Pre-programmed Message (FX)
            pos = _skip_fx_field(data, pos)

        elif frn == 15:                 # I020/500  Position Accuracy (compound)
            break

        elif frn == 16:                 # I020/400  Contributing Receivers (variable)
            # 1-byte count + count × 1 byte receiver IDs
            if pos + 1 > len(data): return track, len(data)
            count = data[pos]; pos += 1
            pos += count

        elif frn == 17:                 # I020/250  Mode-S MB Data (compound)
            break

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
        print("PUB cat20 {} lat={} lon={} sq={} alt={}ft".format(
            ident,
            round(track.get("lat_deg", 0), 4),
            round(track.get("lon_deg", 0), 4),
            track.get("squawk", "----"),
            int(track.get("alt_baro_ft") or track.get("alt_geom_ft") or 0),
        ), flush=True)


def _handle(frames, pub, verbose):
    for cat, data in frames:
        if cat != CAT_020:
            if verbose:
                print("SKIP cat=0x{:02x}".format(cat), flush=True)
            continue
        pos = 0
        while pos < len(data):
            track, pos = decode_cat020_record(data, pos)
            if len(track) > 2:
                _publish(pub, track, verbose)


def run_udp(port, pub, verbose):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    ip = _netbird_ip()
    print("CAT-020 UDP listening on 0.0.0.0:{}".format(port), flush=True)
    print("Tell sender: UDP to {}:{}".format(ip, port), flush=True)
    _handle(iter_frames_udp(sock), pub, verbose)


def _tcp_client(conn, addr, pub, verbose):
    print("CAT-020 TCP connected: {}".format(addr), flush=True)
    try:
        _handle(iter_frames_tcp(conn), pub, verbose)
    except EOFError:
        pass
    finally:
        conn.close()
        print("CAT-020 TCP disconnected: {}".format(addr), flush=True)


def run_tcp(port, pub, verbose):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    ip = _netbird_ip()
    print("CAT-020 TCP server on 0.0.0.0:{}".format(port), flush=True)
    print("Tell sender: TCP to {}:{}".format(ip, port), flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_tcp_client, args=(conn, addr, pub, verbose),
                         daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="ASTERIX CAT-020 MLAT → Zenoh bridge")
    ap.add_argument("--port", type=int, default=30020, help="Listen port (default: 30020)")
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
