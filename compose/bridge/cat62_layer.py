#!/usr/bin/env python3
"""cat62_layer.py — ASTERIX CAT62 radar TCP→Zenoh bridge.

Reads ASTERIX frames from a radar TCP (or UDP) socket, decodes CAT62
System Track records, and publishes them as JSON to the EFDI fabric.

Zenoh topic:  <ORG>/radar/<radar-host>/tracks/v1  (default)
Proto schema: cat62_track.proto  (message Cat62Track, package ltu.cis.tracks.v1)

Install / run:
    . venv/bin/activate
    pip install eclipse-zenoh   # already installed if you ran first-publisher
    python3 cat62_layer.py --radar-host 192.0.2.1 --radar-port 30002
"""

import argparse
import json
import math
import os
import socket
import struct
import time

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG = "1851281db70ccc0409dad4ecfc874cf5"
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

RECONNECT_DELAY_S = 5.0
CAT62 = 0x3E


# ---------------------------------------------------------------------------
# Zenoh helpers
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


# ---------------------------------------------------------------------------
# ASTERIX framing
# ---------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from TCP socket; raise EOFError on close."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def iter_frames_tcp(sock: socket.socket):
    """Yield (cat, data_bytes) for each ASTERIX frame from a TCP stream."""
    while True:
        header = _recv_exact(sock, 3)
        cat = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            continue  # malformed, skip
        data = _recv_exact(sock, length - 3)
        yield cat, data


def iter_frames_udp(sock: socket.socket):
    """Yield (cat, data_bytes) for each ASTERIX frame from UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            data = pkt[offset + 3:offset + length]
            yield cat, data
            offset += length


# ---------------------------------------------------------------------------
# FSPEC parsing
# ---------------------------------------------------------------------------

def parse_fspec(data: bytes, pos: int):
    """Return (fspec_bits_list, new_pos).

    fspec_bits_list[i] is True when data item (i+1) is present.
    Bits are ordered MSB-first within each byte; LSB of each byte is the FX bit.
    """
    bits = []
    while pos < len(data):
        byte = data[pos]
        pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):  # FX bit: 0 = last FSPEC byte
            break
    return bits, pos


# ---------------------------------------------------------------------------
# Field decoders
# ---------------------------------------------------------------------------

def _s16(b: bytes) -> int:
    return struct.unpack(">h", b)[0]


def _u16(b: bytes) -> int:
    return struct.unpack(">H", b)[0]


def _s32(b: bytes) -> int:
    return struct.unpack(">i", b)[0]


def _skip_fx_field(data: bytes, pos: int) -> int:
    """Skip a variable-length FX-extended field (LSB = FX per byte)."""
    while pos < len(data):
        b = data[pos]
        pos += 1
        if not (b & 0x01):
            break
    return pos


def _decode_6bit_str(raw: bytes) -> str:
    """Decode 6-char ICAO 6-bit callsign packed into 6 bytes (36 bits used)."""
    ALPHABET = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
    bits = int.from_bytes(raw, "big")
    chars = []
    for i in range(5, -1, -1):
        idx = (bits >> (i * 6)) & 0x3F
        chars.append(ALPHABET[idx])
    return "".join(chars).rstrip()


# I062 field sizes (bytes) for fixed-length items
_FIELD_FIXED = {
    # frn: (frn_index_0based, size_bytes)
    # mapped by position in FSPEC (0-based)
    0:  ("I062/010", 2),   # Data Source Identifier
    1:  ("I062/015", 1),   # Service Identification
    2:  ("I062/070", 3),   # Time Of Track Information
    3:  ("I062/105", 8),   # Calculated Position in WGS-84 Co-ordinates
    4:  ("I062/100", 4),   # Calculated Track Position (Cartesian)
    5:  ("I062/185", 4),   # Calculated Track Velocity (Cartesian)
    6:  ("I062/210", 4),   # Calculated Acceleration (Cartesian)
    7:  ("I062/060", 2),   # Track Mode 3/A Code
    8:  ("I062/040", 2),   # Track Number
    # 9  = I062/080 — FX variable
    # 10 = I062/290 — compound
    # 11 = I062/200 — 2 bytes (mode of movement)
    # 12 = I062/295 — compound
    # 13 = I062/136 — 2 bytes (measured flight level)
    # 14 = I062/130 — 2 bytes (calculated alt)
    # 15 = I062/380 — compound
    # 16 = I062/390 — compound
    # 17 = I062/270 — FX variable (target size)
    # 18 = I062/300 — 1 byte (vehicle fleet)
    # 19 = I062/110 — compound
    # 20 = I062/120 — compound
    # 21 = I062/510 — FX variable
    # 22 = I062/500 — compound
    # 23 = I062/340 — compound
    # 24 = I062/RE  — reserved expansion
    # 25 = I062/SP  — special purpose
}


def decode_cat62_record(data: bytes, pos: int):
    """Decode one CAT62 record starting at pos. Returns (track_dict, new_pos)."""
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time()}

    for frn, present in enumerate(fspec):
        if not present:
            continue

        # --- Fixed-length items we fully decode ---
        if frn == 0:  # I062/010 Data Source Identifier
            if pos + 2 > len(data):
                return track, len(data)
            track["sac"] = data[pos]
            track["sic"] = data[pos + 1]
            pos += 2

        elif frn == 1:  # I062/015 Service Identification
            if pos + 1 > len(data):
                return track, len(data)
            track["service_id"] = data[pos]
            pos += 1

        elif frn == 2:  # I062/070 Time Of Track Information (1/128 s)
            if pos + 3 > len(data):
                return track, len(data)
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["time_utc_s"] = raw / 128.0
            pos += 3

        elif frn == 3:  # I062/105 WGS-84 position (2 × signed 32-bit, LSB=180/2^25 deg)
            if pos + 8 > len(data):
                return track, len(data)
            lat_raw = _s32(data[pos:pos + 4])
            lon_raw = _s32(data[pos + 4:pos + 8])
            scale = 180.0 / (2 ** 25)
            track["lat_deg"] = lat_raw * scale
            track["lon_deg"] = lon_raw * scale
            pos += 8

        elif frn == 4:  # I062/100 Cartesian position (2 × signed 16-bit, LSB=0.5 m)
            if pos + 4 > len(data):
                return track, len(data)
            track["x_m"] = _s16(data[pos:pos + 2]) * 0.5
            track["y_m"] = _s16(data[pos + 2:pos + 4]) * 0.5
            pos += 4

        elif frn == 5:  # I062/185 Cartesian velocity (2 × signed 16-bit, LSB=0.25 m/s)
            if pos + 4 > len(data):
                return track, len(data)
            vx = _s16(data[pos:pos + 2]) * 0.25
            vy = _s16(data[pos + 2:pos + 4]) * 0.25
            track["vx_ms"] = vx
            track["vy_ms"] = vy
            track["speed_ms"] = math.hypot(vx, vy)
            track["heading_deg"] = math.degrees(math.atan2(vx, vy)) % 360.0
            pos += 4

        elif frn == 6:  # I062/210 Acceleration (2 × signed 16-bit, LSB=0.25 m/s²)
            if pos + 4 > len(data):
                return track, len(data)
            track["ax_ms2"] = _s16(data[pos:pos + 2]) * 0.25
            track["ay_ms2"] = _s16(data[pos + 2:pos + 4]) * 0.25
            pos += 4

        elif frn == 7:  # I062/060 Track Mode 3/A
            if pos + 2 > len(data):
                return track, len(data)
            raw = _u16(data[pos:pos + 2])
            mode3a = raw & 0x0FFF
            track["mode3a"] = "{:04o}".format(mode3a)
            pos += 2

        elif frn == 8:  # I062/040 Track Number (12 bits, upper 4 bits = flags)
            if pos + 2 > len(data):
                return track, len(data)
            raw = _u16(data[pos:pos + 2])
            track["track_num"] = raw & 0x0FFF
            pos += 2

        elif frn == 9:  # I062/080 Track Status (FX variable)
            start = pos
            pos = _skip_fx_field(data, pos)
            track["track_status_hex"] = data[start:pos].hex()
            # Bit 7 of first byte = confirmed track (CNF=0 means confirmed)
            if start < len(data):
                track["track_confirmed"] = not bool(data[start] & 0x80)

        elif frn == 11:  # I062/200 Mode of Movement — 2 bytes
            pos += 2

        elif frn == 13:  # I062/136 Measured Flight Level — 2 bytes
            if pos + 2 > len(data):
                return track, len(data)
            raw = _s16(data[pos:pos + 2])
            track["measured_fl"] = raw * 0.25  # FL in 1/4 FL units
            pos += 2

        elif frn == 14:  # I062/130 Calculated Altitude — 2 bytes
            if pos + 2 > len(data):
                return track, len(data)
            raw = _s16(data[pos:pos + 2])
            track["calc_alt_ft"] = raw * 25  # LSB = 25 ft
            pos += 2

        elif frn == 18:  # I062/300 Vehicle Fleet ID — 1 byte
            pos += 1

        # --- Variable / compound fields: skip gracefully ---
        elif frn in (10, 12, 15, 16, 17, 19, 20, 21, 22, 23, 24, 25):
            # Compound and reserved expansion fields — bail; remaining
            # fields after an unknown-length compound are unrecoverable.
            break

        else:
            # Unknown FRN beyond standard table — stop parsing this record.
            break

    # I062/245 callsign lives inside I062/380 (compound frn 15) — if the
    # caller decoded it separately (e.g. from a pre-parsed compound), it
    # would be in track already. We don't attempt compound parsing here.

    return track, pos


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args):
    topic = "{}/{}/{}".format(ORG, args.topic_prefix, args.topic_suffix)
    print("Zenoh topic:", topic, flush=True)

    session = zenoh.open(make_config())
    pub = session.declare_publisher(topic)

    try:
        while True:
            try:
                if args.udp:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.bind(("", args.radar_port))
                    print("UDP listening on port", args.radar_port, flush=True)
                    frame_iter = iter_frames_udp(sock)
                else:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.connect((args.radar_host, args.radar_port))
                    print("TCP connected to {}:{}".format(args.radar_host, args.radar_port), flush=True)
                    frame_iter = iter_frames_tcp(sock)

                for cat, data in frame_iter:
                    if cat != CAT62:
                        continue
                    pos = 0
                    while pos < len(data):
                        track, pos = decode_cat62_record(data, pos)
                        if len(track) <= 1:  # only _ts
                            continue
                        payload = json.dumps(track)
                        pub.put(payload.encode())
                        print("PUB", payload[:120], flush=True)

            except (EOFError, ConnectionRefusedError, OSError) as exc:
                print("Socket error: {} — reconnecting in {}s".format(exc, RECONNECT_DELAY_S), flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(RECONNECT_DELAY_S)

    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="ASTERIX CAT62 → Zenoh bridge")
    ap.add_argument("--radar-host", default="127.0.0.1", help="Radar TCP host")
    ap.add_argument("--radar-port", type=int, default=30002, help="Radar port")
    ap.add_argument("--topic-prefix", default="radar/{}".format("__host__"),
                    help="Middle segment of topic (default: radar/<radar-host>)")
    ap.add_argument("--topic-suffix", default="tracks/v1", help="Topic suffix")
    ap.add_argument("--udp", action="store_true", help="Use UDP instead of TCP")
    args = ap.parse_args()

    if args.topic_prefix == "radar/__host__":
        label = args.radar_host.replace(".", "-") if not args.udp else "udp-{}".format(args.radar_port)
        args.topic_prefix = "radar/{}".format(label)

    run(args)


if __name__ == "__main__":
    main()
