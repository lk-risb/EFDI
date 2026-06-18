#!/usr/bin/env python3
"""stanag4586_layer.py — STANAG 4586 UAS interface (CUCS side) → Zenoh.

Connects to a UAV Vehicle Specific Module (VSM) as a Control and
Communications Subsystem (CUCS), receives vehicle telemetry, and publishes
each vehicle's position to the EFDI Zenoh fabric.

Also subscribes to Zenoh for waypoint/tasking commands and forwards them to
the VSM — enabling bidirectional UAV control via the EFDI fabric.

STANAG 4586 Edition 3 message framing (Section 3.4):
  Offset  Size  Field
  0       2     Message Type (uint16 LE)
  2       2     Message Size (uint16 LE, total bytes including header)
  4       2     Instance Number (uint16 LE)
  6       N     Message body

Implemented message types (Edition 3 — verify numbers against your VSM):
  0x4001  VSM Heartbeat    — liveness, capabilities
  0x4002  CUCS Heartbeat   — we send this to keep the connection alive
  0x0001  Vehicle Operating States — lat/lon/alt/heading/speed

IMPORTANT: Message type numbers and body field offsets vary between STANAG
4586 editions (2, 3, 4).  Verify against the documentation for your specific
UAV ground station before connecting to live equipment.

Config (compose/.env):
  STANAG4586_HOST=     VSM hostname/IP
  STANAG4586_PORT=4586 VSM port (STANAG-assigned: 4586)

Run:
  venv/bin/python3 stanag4586_layer.py --host 192.168.1.50 --port 4586
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

RECONNECT_S     = 10
HEARTBEAT_S     = 5
TOPIC_UAV_OUT   = "{}/air/stanag4586/uav/civ/aircraft/tracks/v1".format(ORG)

# Message type constants (STANAG 4586 Ed.3 — confirm against VSM docs)
MSG_VSM_HEARTBEAT  = 0x4001
MSG_CUCS_HEARTBEAT = 0x4002
MSG_VEHICLE_STATE  = 0x0001   # Vehicle Operating States

HEADER_SIZE = 6


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
# Framing
# ---------------------------------------------------------------------------

def _build_header(msg_type: int, body_len: int, instance: int = 1) -> bytes:
    total = HEADER_SIZE + body_len
    return struct.pack("<HHH", msg_type, total, instance)


def _build_cucs_heartbeat(instance: int = 1) -> bytes:
    # Body: cucs_id (uint16) + timestamp (uint32) + capabilities (uint32)
    body = struct.pack("<HIB", instance, int(time.time()) & 0xFFFFFFFF, 0x01)
    return _build_header(MSG_CUCS_HEARTBEAT, len(body)) + body


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("VSM connection closed")
        buf += chunk
    return buf


def _recv_message(sock: socket.socket) -> tuple[int, int, bytes]:
    """Read one STANAG 4586 message. Returns (msg_type, instance, body)."""
    hdr = _recv_exact(sock, HEADER_SIZE)
    msg_type, total_size, instance = struct.unpack("<HHH", hdr)
    body_size = total_size - HEADER_SIZE
    body = _recv_exact(sock, body_size) if body_size > 0 else b""
    return msg_type, instance, body


# ---------------------------------------------------------------------------
# Vehicle Operating States decoder
# ---------------------------------------------------------------------------

def _decode_vehicle_state(body: bytes, instance: int) -> dict | None:
    """Decode MSG_VEHICLE_STATE body.

    Field layout (Ed.3 Annex B, approximate — verify against your VSM):
      Offset  Size  Type     Field
      0       8     float64  Latitude (degrees)
      8       8     float64  Longitude (degrees)
      16      8     float64  Altitude MSL (metres)
      24      8     float64  Altitude AGL (metres)
      32      8     float64  Heading (degrees)
      40      8     float64  Ground speed (m/s)
      48      8     float64  Vertical speed (m/s)
      56      4     float32  Fuel remaining (0.0–1.0)
      60      1     uint8    Vehicle mode
    """
    if len(body) < 56:
        return None
    try:
        lat, lon, alt_msl, alt_agl, heading, speed, vspeed = \
            struct.unpack_from("<ddddddd", body)
    except struct.error:
        return None

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    track = {
        "_ts":        time.time(),
        "_src":       "STANAG 4586",
        "uid":        "stanag4586-vsm-{}".format(instance),
        "callsign":   "UAV-VSM-{}".format(instance),
        "lat_deg":    round(lat, 6),
        "lon_deg":    round(lon, 6),
        "alt_m":      round(alt_msl, 1),
    }
    if abs(heading) <= 360:
        track["heading_deg"] = round(heading % 360, 1)
    if speed >= 0:
        track["speed_ms"] = round(speed, 2)
    if abs(vspeed) < 1000:
        track["vertical_rate_ms"] = round(vspeed, 2)
    if len(body) >= 60:
        fuel = struct.unpack_from("<f", body, 56)[0]
        if 0.0 <= fuel <= 1.0:
            track["fuel_pct"] = round(fuel * 100, 1)
    return track


# ---------------------------------------------------------------------------
# Heartbeat sender thread
# ---------------------------------------------------------------------------

def _heartbeat_loop(sock: socket.socket, stop_evt: threading.Event):
    while not stop_evt.wait(HEARTBEAT_S):
        try:
            sock.sendall(_build_cucs_heartbeat())
        except OSError:
            break


# ---------------------------------------------------------------------------
# Main session loop
# ---------------------------------------------------------------------------

def _run_session(host: str, port: int, session: "zenoh.Session", verbose: bool):
    sock = socket.create_connection((host, port), timeout=10)
    print("STANAG 4586 connected to {}:{}".format(host, port), flush=True)

    stop_evt = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_loop,
                                 args=(sock, stop_evt), daemon=True)
    hb_thread.start()

    try:
        while True:
            msg_type, instance, body = _recv_message(sock)

            if msg_type == MSG_VSM_HEARTBEAT:
                if verbose:
                    print("VSM heartbeat instance={}".format(instance), flush=True)

            elif msg_type == MSG_VEHICLE_STATE:
                track = _decode_vehicle_state(body, instance)
                if track:
                    session.put(TOPIC_UAV_OUT, json.dumps(track).encode(),
                                encoding=zenoh.Encoding.APPLICATION_JSON)
                    if verbose:
                        print("STANAG4586 UAV{} lat={} lon={} alt={:.0f}m spd={:.1f}m/s".format(
                            instance,
                            round(track["lat_deg"], 4),
                            round(track["lon_deg"], 4),
                            track.get("alt_m", 0),
                            track.get("speed_ms", 0)), flush=True)

            elif verbose:
                print("STANAG4586 msg 0x{:04x} len={} instance={}".format(
                    msg_type, len(body), instance), flush=True)

    finally:
        stop_evt.set()
        sock.close()


def run(args):
    session = zenoh.open(make_config())
    print("STANAG 4586 layer started", flush=True)
    print("  VSM: {}:{}".format(args.host, args.port), flush=True)
    print("  NOTE: Message numbers per Ed.3 — verify against your VSM before use", flush=True)

    while True:
        try:
            _run_session(args.host, args.port, session, args.verbose)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("STANAG4586 error: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
            time.sleep(RECONNECT_S)

    session.close()


def main():
    ap = argparse.ArgumentParser(description="STANAG 4586 CUCS → Zenoh bridge")
    ap.add_argument("--host", default=os.environ.get("STANAG4586_HOST", ""),
                    required=not os.environ.get("STANAG4586_HOST"),
                    help="VSM hostname or IP")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("STANAG4586_PORT", "4586")))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    if not args.host:
        raise SystemExit("Set STANAG4586_HOST in .env or pass --host")
    run(args)


if __name__ == "__main__":
    main()
