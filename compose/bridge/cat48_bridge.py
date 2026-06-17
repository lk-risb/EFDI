#!/usr/bin/env python3
"""cat48_bridge.py — ASTERIX CAT-048 / CAT-034 listener → Zenoh bridge.

Listens for ASTERIX data from a surveillance radar (e.g. Saab Giraffe AMB)
and publishes decoded radar tracks to the EFDI Zenoh fabric.

  CAT-034  Monoradar Service Messages — north marker, antenna rotation,
           system status. Decoded and logged; antenna period updates stale time.

  CAT-048  Monoradar Target Reports — the actual radar plots/tracks.
           Decoded and published as JSON to Zenoh so cot_layer.py picks them
           up and forwards to ATAK as unknown-affiliation air tracks (yellow).

The radar pushes TO US — we bind a port, the Giraffe connects / sends here.

  UDP (default): Giraffe sends datagrams to our NetBird IP:PORT.
                 No handshake — fire and forget.
  TCP server:    Giraffe opens a TCP connection; we accept and stream.
                 Use --tcp if the radar is configured for TCP output.

Tell the Giraffe crew:
    Destination IP:   100.64.59.142
    Destination port: <CAT48_PORT from .env, default 30048>
    Protocol:         UDP  (or TCP if configured with --tcp)

Polar → WGS-84 conversion:
    I048/040 gives slant-polar (range, azimuth) relative to the radar antenna.
    Provide --radar-lat / --radar-lon so we can convert to map coordinates.
    If Mode-S ICAO address (I048/220) is present the track already has a
    stable UID that merges with ADS-B data from the other bridges.

Zenoh topic:
    <ORG>/air/cat48/asterix/unknown/aircraft/tracks/v1
    → matched by cot_layer.py "air/**/unknown/**" → CoT a-u-A (yellow)

Run:
    # UDP listener on port 30048, radar site at Vilnius
    venv/bin/python3 cat48_bridge.py --port 30048 \\
        --radar-lat 54.687 --radar-lon 25.279

    # TCP server
    venv/bin/python3 cat48_bridge.py --port 30048 --tcp

Activate in .env:
    CAT48_PORT=30048
    CAT48_RADAR_LAT=54.687
    CAT48_RADAR_LON=25.279
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

TOPIC = "{}/air/cat48/asterix/unknown/aircraft/tracks/v1".format(ORG)

CAT_034 = 0x22   # decimal 34
CAT_048 = 0x30   # decimal 48


def _netbird_ip() -> str:
    """Return the NetBird mesh IP (wt0 interface), or fallback to hostname."""
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


# ---------------------------------------------------------------------------
# ASTERIX framing — shared with cat62_layer.py logic
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
        pkt, addr = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            yield cat, pkt[offset + 3:offset + length]
            offset += length


# ---------------------------------------------------------------------------
# FSPEC parser — identical to cat62_layer
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
# Field helpers
# ---------------------------------------------------------------------------

def _s16(b: bytes) -> int: return struct.unpack(">h", b)[0]
def _u16(b: bytes) -> int: return struct.unpack(">H", b)[0]


_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

def _decode_callsign(raw: bytes) -> str:
    """Decode 6-char ICAO 6-bit aircraft identification packed into 6 bytes."""
    bits = int.from_bytes(raw, "big")
    return "".join(_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(5, -1, -1)).rstrip()


def _polar_to_wgs84(radar_lat: float, radar_lon: float,
                    range_nm: float, azimuth_deg: float):
    """Convert slant-polar radar plot to WGS-84 lat/lon (haversine forward)."""
    d    = range_nm * 1852.0          # NM → metres
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
# CAT-034 decoder  (Monoradar Service Messages)
# FSPEC order per Eurocontrol ASTERIX Edition 1.29
# ---------------------------------------------------------------------------
#
# FRN  Item       Size  Description
#  1   I034/010   2     Data Source Identifier (SAC/SIC)
#  2   I034/000   1     Message Type (1=north_marker 2=sector 3=geo_filter 4=jamming)
#  3   I034/030   3     Time of Day  (1/128 s)
#  4   I034/020   1     Sector Number (360/256 deg per LSB)
#  5   I034/041   2     Antenna Rotation Period (1/128 s)
#  6   I034/050   cmpd  System Configuration and Status
#  7   I034/060   cmpd  System Processing Mode
#  8   I034/070   cmpd  Message Count Values
#  9   I034/100   8     Generic Polar Window
# 10   I034/110   1     Data Filter
# 11   I034/120   cmpd  3D-Range and Azimuth
# 12   I034/090   2     Collimation Error
# 13   I034/RE    var   Reserved Expansion
# 14   I034/SP    var   Special Purpose

_MSG_TYPES_034 = {1: "north_marker", 2: "sector_crossing",
                  3: "geo_filter",   4: "jamming_strobe"}


def decode_cat034(data: bytes) -> dict | None:
    """Decode one CAT-034 message. Returns status dict or None on error."""
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
        elif frn == 2:                  # I034/030  Time of Day
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I034/020  Sector Number
            if pos + 1 > len(data): break
            msg["sector_deg"] = data[pos] * 360.0 / 256.0; pos += 1
        elif frn == 4:                  # I034/041  Antenna Rotation Period
            if pos + 2 > len(data): break
            msg["rotation_s"] = _u16(data[pos:pos+2]) / 128.0; pos += 2
        elif frn in (5, 6, 7, 11):     # compound fields — stop parsing
            break
        elif frn == 8:                  # I034/100  Generic Polar Window  8 bytes
            pos += 8
        elif frn == 9:                  # I034/110  Data Filter  1 byte
            pos += 1
        elif frn == 11:                 # I034/090  Collimation Error  2 bytes
            pos += 2
        else:
            break
    return msg if msg else None


# ---------------------------------------------------------------------------
# CAT-048 decoder  (Monoradar Target Reports)
# FSPEC order per Eurocontrol ASTERIX Edition 1.15
# ---------------------------------------------------------------------------
#
# FRN  Item       Size  Description
#  1   I048/010   2     Data Source Identifier (SAC/SIC)
#  2   I048/140   3     Time of Day  (1/128 s)
#  3   I048/020   FX    Target Report Descriptor
#  4   I048/040   4     Measured Position in Slant Polar Coordinates
#                         Range: 1/256 NM per LSB (u16)
#                         Azimuth: 360/65536 deg per LSB (u16)
#  5   I048/070   2     Mode-3/A Code  (lower 12 bits = octal squawk)
#  6   I048/090   2     Flight Level   (1/4 FL per LSB, signed)
#  7   I048/130   cmpd  Radar Plot Characteristics
#  8   I048/220   3     Aircraft Address  (Mode-S ICAO 24-bit)
#  9   I048/240   6     Aircraft Identification  (6-bit packed callsign)
# 10   I048/250   var   Mode-S MB Data  (BDS registers — skip)
# 11   I048/161   2     Track Number  (lower 12 bits)
# 12   I048/042   4     Calculated Position Cartesian (1/128 NM per LSB, signed)
# 13   I048/200   4     Calculated Track Velocity Polar
#                         Speed:   1/4 kt per LSB (u16)
#                         Heading: 360/65536 deg per LSB (u16)
# 14   I048/170   FX    Track Status
# 15   I048/210   4     Track Quality
# 16   I048/030   FX    Warning/Error Conditions
# 17   I048/080   2     Mode-3/A Confidence Indicator
# 18   I048/100   4     Mode-C Code and Confidence Indicator
# 19   I048/110   2     Height Measured by 3D Radar  (25 ft per LSB, signed)
# 20   I048/120   cmpd  Radial Doppler Speed
# 21   I048/230   2     Communications/ACAS Capability
# 22   I048/260   7     ACAS Resolution Advisory Report
# 23   I048/055   1     Mode-1 Code
# 24   I048/050   2     Mode-2 Code
# 25   I048/RE    var   Reserved Expansion
# 26   I048/SP    var   Special Purpose


def decode_cat048_record(data: bytes, pos: int,
                         radar_lat: float | None, radar_lon: float | None):
    """Decode one CAT-048 target record starting at pos.
    Returns (track_dict, new_pos).
    """
    fspec, pos = parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "cat48"}

    for frn, present in enumerate(fspec):
        if not present:
            continue

        if frn == 0:                    # I048/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos+1]; pos += 2

        elif frn == 1:                  # I048/140  Time of Day
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            track["tod_s"] = raw / 128.0; pos += 3

        elif frn == 2:                  # I048/020  Target Report Descriptor (FX)
            pos = _skip_fx_field(data, pos)

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

        elif frn == 4:                  # I048/070  Mode-3/A
            if pos + 2 > len(data): return track, len(data)
            raw = _u16(data[pos:pos+2])
            track["squawk"] = "{:04o}".format(raw & 0x0FFF); pos += 2

        elif frn == 5:                  # I048/090  Flight Level (1/4 FL, signed)
            if pos + 2 > len(data): return track, len(data)
            raw = _s16(data[pos:pos+2])
            track["alt_baro_ft"] = round(raw * 0.25 * 100); pos += 2

        elif frn == 6:                  # I048/130  Radar Plot Characteristics (compound)
            break                       # compound — stop; remaining fields unrecoverable

        elif frn == 7:                  # I048/220  Aircraft Address (Mode-S ICAO)
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            track["icao24"] = "{:06x}".format(addr); pos += 3

        elif frn == 8:                  # I048/240  Aircraft Identification
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _decode_callsign(data[pos:pos+6]); pos += 6

        elif frn == 9:                  # I048/250  Mode-S MB Data (variable, complex BDS)
            break                       # skip — compound/variable length

        elif frn == 10:                 # I048/161  Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _u16(data[pos:pos+2]) & 0x0FFF; pos += 2

        elif frn == 11:                 # I048/042  Cartesian Position (1/128 NM, signed)
            if pos + 4 > len(data): return track, len(data)
            # Could convert to WGS-84 via radar origin; slant polar is already done
            pos += 4

        elif frn == 12:                 # I048/200  Track Velocity Polar
            if pos + 4 > len(data): return track, len(data)
            spd_raw = _u16(data[pos:pos+2])
            hdg_raw = _u16(data[pos+2:pos+4])
            spd_kt  = spd_raw / 4.0
            track["speed_ms"]   = round(spd_kt * 0.514444, 2)
            track["heading_deg"] = round(hdg_raw * 360.0 / 65536.0, 2)
            pos += 4

        elif frn == 13:                 # I048/170  Track Status (FX variable)
            pos = _skip_fx_field(data, pos)

        elif frn == 14:                 # I048/210  Track Quality  4 bytes
            pos += 4

        elif frn == 15:                 # I048/030  Warning/Error Conditions (FX)
            pos = _skip_fx_field(data, pos)

        elif frn == 16:                 # I048/080  Mode-3/A Confidence  2 bytes
            pos += 2

        elif frn == 17:                 # I048/100  Mode-C + Confidence  4 bytes
            pos += 4

        elif frn == 18:                 # I048/110  3D Height  (25 ft per LSB, signed)
            if pos + 2 > len(data): return track, len(data)
            track["alt_3d_ft"] = _s16(data[pos:pos+2]) * 25; pos += 2

        elif frn == 19:                 # I048/120  Radial Doppler (compound)
            break

        elif frn == 20:                 # I048/230  Comms/ACAS  2 bytes
            pos += 2

        elif frn == 21:                 # I048/260  ACAS RA Report  7 bytes
            pos += 7

        elif frn == 22:                 # I048/055  Mode-1  1 byte
            pos += 1

        elif frn == 23:                 # I048/050  Mode-2  2 bytes
            pos += 2

        else:
            break

    # Build stable radar_id for PSR-only tracks (no Mode-S ICAO)
    if "icao24" not in track:
        sac = track.get("sac", 0); sic = track.get("sic", 0)
        tnum = track.get("track_num", 0)
        track["radar_id"] = "CAT48-{:03d}-{:03d}-{:04d}".format(sac, sic, tnum)

    return track, pos


# ---------------------------------------------------------------------------
# Publish helpers
# ---------------------------------------------------------------------------

def _publish(pub, track: dict, verbose: bool):
    if "lat_deg" not in track and "lon_deg" not in track:
        return   # no position — nothing to show on the map
    pub.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        ident = (track.get("icao24") or track.get("callsign") or
                 track.get("radar_id") or "?")
        print("PUB cat48 {} lat={} lon={} sq={} fl={}".format(
            ident,
            round(track.get("lat_deg", 0), 4),
            round(track.get("lon_deg", 0), 4),
            track.get("squawk", "----"),
            round(track.get("alt_baro_ft", 0) / 100) if track.get("alt_baro_ft") else "---",
        ), flush=True)


def _handle_stream(frame_iter, pub, radar_lat, radar_lon, verbose):
    for cat, data in frame_iter:
        if cat == CAT_034:
            msg = decode_cat034(data)
            if msg and verbose:
                mtype = msg.get("msg_type", "?")
                tod   = msg.get("tod_s")
                rot   = msg.get("rotation_s")
                print("CAT-034 {} tod={} rot={}s".format(
                    mtype,
                    "{:.2f}".format(tod) if tod else "-",
                    "{:.2f}".format(rot) if rot else "-",
                ), flush=True)

        elif cat == CAT_048:
            pos = 0
            while pos < len(data):
                track, pos = decode_cat048_record(data, pos, radar_lat, radar_lon)
                if len(track) > 2:   # more than just _ts + _src
                    if verbose and "lat_deg" not in track:
                        ident = track.get("icao24") or track.get("radar_id") or "PSR"
                        print("CAT-048 {} no-position (set radar-lat/lon)".format(ident), flush=True)
                    _publish(pub, track, verbose)

        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


# ---------------------------------------------------------------------------
# Transport modes
# ---------------------------------------------------------------------------

def run_udp(port: int, pub, radar_lat, radar_lon, verbose):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    our_ip = _netbird_ip()
    print("CAT-48/34 UDP listening on 0.0.0.0:{}".format(port), flush=True)
    print("Tell Giraffe crew: send ASTERIX to {}:{} (UDP)".format(our_ip, port), flush=True)
    _handle_stream(iter_frames_udp(sock), pub, radar_lat, radar_lon, verbose)


def _tcp_client(conn, addr, pub, radar_lat, radar_lon, verbose):
    print("CAT-48/34 TCP connected: {}".format(addr), flush=True)
    try:
        _handle_stream(iter_frames_tcp(conn), pub, radar_lat, radar_lon, verbose)
    except EOFError:
        pass
    finally:
        conn.close()
        print("CAT-48/34 TCP disconnected: {}".format(addr), flush=True)


def run_tcp(port: int, pub, radar_lat, radar_lon, verbose):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    our_ip = _netbird_ip()
    print("CAT-48/34 TCP server on 0.0.0.0:{}".format(port), flush=True)
    print("Tell Giraffe crew: connect to {}:{} (TCP)".format(our_ip, port), flush=True)
    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=_tcp_client,
                             args=(conn, addr, pub, radar_lat, radar_lon, verbose),
                             daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="ASTERIX CAT-048/034 → Zenoh bridge")
    ap.add_argument("--port", type=int, default=30048,
                    help="Port to listen on (default: 30048)")
    ap.add_argument("--tcp", action="store_true",
                    help="Use TCP server mode instead of UDP (default: UDP)")
    def _env_float(key):
        v = os.environ.get(key, "").strip()
        return float(v) if v else 0.0

    ap.add_argument("--radar-lat", type=float, default=_env_float("CAT48_RADAR_LAT"),
                    help="Radar antenna latitude for polar→WGS84 conversion")
    ap.add_argument("--radar-lon", type=float, default=_env_float("CAT48_RADAR_LON"),
                    help="Radar antenna longitude for polar→WGS84 conversion")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    radar_lat = args.radar_lat if args.radar_lat != 0.0 else None
    radar_lon = args.radar_lon if args.radar_lon != 0.0 else None

    if radar_lat is None:
        print("WARNING: --radar-lat/--radar-lon not set. "
              "Polar plots will have no WGS-84 position. "
              "Only Mode-S tracks with ICAO position will be visible.", flush=True)

    session = zenoh.open(make_config())
    pub = session.declare_publisher(TOPIC)
    print("Zenoh topic:", TOPIC, flush=True)

    try:
        if args.tcp:
            run_tcp(args.port, pub, radar_lat, radar_lon, args.verbose)
        else:
            run_udp(args.port, pub, radar_lat, radar_lon, args.verbose)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
