#!/usr/bin/env python3
"""sapient_layer.py — SAPIENT v7/v8 TCP → Zenoh bridge.

Connects to a SAPIENT Node Manager as a sensor client, receives
DetectionReport messages (length-prefixed protobuf), decodes position and
classification, and publishes each object as a track JSON to the EFDI fabric.

SAPIENT framing: 4-byte big-endian length prefix + raw protobuf bytes.
No external protobuf library required — wire-format fields decoded in-house.

Zenoh topic:  <ORG>/sapient/<node-id>/tracks/v1

Run:
    venv/bin/python3 sapient_layer.py --host 192.0.2.10 --port 7001
    venv/bin/python3 sapient_layer.py --host 192.0.2.10 --port 7001 --node-id sensor-01
"""

import argparse
import json
import os
import socket
import struct
import time

import zenoh
from namespace_prefix import prefix

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG   # org prefix (configurable) precedes the pod namespace
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

RECONNECT_S = 5

# SAPIENT message type field numbers inside SapientMessage
_FIELD_STATUS_REPORT    = 3
_FIELD_DETECTION_REPORT = 4


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
# Minimal protobuf wire-format decoder (stdlib only)
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int):
    """Decode a base-128 varint at pos. Return (value, new_pos)."""
    result = 0
    shift  = 0
    while pos < len(buf):
        b = buf[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 8
    raise ValueError("truncated varint")


def _iter_fields(buf: bytes):
    """Iterate over (field_number, wire_type, raw_value) in a protobuf message."""
    pos = 0
    while pos < len(buf):
        tag, pos   = _read_varint(buf, pos)
        field_num  = tag >> 3
        wire_type  = tag & 0x07
        if wire_type == 0:          # varint
            val, pos = _read_varint(buf, pos)
            yield field_num, 0, val
        elif wire_type == 1:        # 64-bit
            val = struct.unpack_from("<Q", buf, pos)[0]; pos += 8
            yield field_num, 1, val
        elif wire_type == 2:        # length-delimited
            length, pos = _read_varint(buf, pos)
            val = buf[pos:pos + length]; pos += length
            yield field_num, 2, val
        elif wire_type == 5:        # 32-bit
            val = struct.unpack_from("<I", buf, pos)[0]; pos += 4
            yield field_num, 5, val
        else:
            break  # unknown wire type — stop


def _f64(raw) -> float:
    """Interpret raw (int from wire type 1) as IEEE-754 double."""
    return struct.unpack("<d", struct.pack("<Q", raw))[0]


def _f32(raw) -> float:
    """Interpret raw (int from wire type 5) as IEEE-754 float."""
    return struct.unpack("<f", struct.pack("<I", raw))[0]


def _bytes_to_hex(b: bytes) -> str:
    return b.hex() if isinstance(b, (bytes, bytearray)) else str(b)


# ---------------------------------------------------------------------------
# SAPIENT DetectionReport decoder
# The field numbers follow SAPIENT v7 (BSI SAPIENT ICD v7.0).
# LatLng inside ObjectState.Location: field 1 = lat (double), field 2 = lng (double)
# Velocity: field 1 = eN (m/s), field 2 = eE (m/s), field 3 = eD (m/s)
# ---------------------------------------------------------------------------

def _decode_lat_lng(buf: bytes):
    lat = lon = None
    for fn, wt, val in _iter_fields(buf):
        if fn == 1 and wt == 1:
            lat = _f64(val)
        elif fn == 2 and wt == 1:
            lon = _f64(val)
    return lat, lon


def _decode_location(buf: bytes):
    """Location message: field 1 = LatLng (msg), field 3 = z/altitude (float)."""
    lat = lon = alt = None
    for fn, wt, val in _iter_fields(buf):
        if fn == 1 and wt == 2:
            lat, lon = _decode_lat_lng(val)
        elif fn == 3 and wt == 5:
            alt = _f32(val)
    return lat, lon, alt


def _decode_velocity(buf: bytes):
    """Velocity NED: eN=field1, eE=field2, eD=field3 (all float/double)."""
    vn = ve = 0.0
    for fn, wt, val in _iter_fields(buf):
        if fn == 1:
            vn = _f32(val) if wt == 5 else _f64(val)
        elif fn == 2:
            ve = _f32(val) if wt == 5 else _f64(val)
    import math
    speed   = math.hypot(vn, ve)
    heading = math.degrees(math.atan2(ve, vn)) % 360.0
    return round(speed, 2), round(heading, 1)


def _decode_object_state(buf: bytes):
    lat = lon = alt = None
    speed = heading = 0.0
    for fn, wt, val in _iter_fields(buf):
        if fn == 1 and wt == 2:
            lat, lon, alt = _decode_location(val)
        elif fn == 2 and wt == 2:
            speed, heading = _decode_velocity(val)
    return lat, lon, alt, speed, heading


# Classification type field 1 = ClassifierType enum, field 2 = confidence
_CLASS_TYPE = {
    0: "unknown", 1: "person", 2: "vehicle", 3: "aircraft",
    4: "uav", 5: "emitter", 6: "animal", 7: "watercraft",
}

def _decode_classification(buf: bytes) -> str:
    for fn, wt, val in _iter_fields(buf):
        if fn == 1 and wt == 0:
            return _CLASS_TYPE.get(val, "unknown")
    return "unknown"


def decode_detection_report(buf: bytes) -> dict | None:
    """
    DetectionReport field map (SAPIENT v7):
      1 = reportId (bytes)
      2 = sourceNodeId (bytes)
      3 = objectId (bytes)
      4 = timestamp (google.protobuf.Timestamp msg: 1=seconds, 2=nanos)
      5 = objectState (ObjectState msg)
      6 = detectionConfidence (float)
      7 = classification (Classification msg)
    """
    obj_id = src_id = None
    lat = lon = alt = None
    speed = heading = 0.0
    obj_class = "unknown"
    ts = time.time()

    for fn, wt, val in _iter_fields(buf):
        if fn == 3 and wt == 2:
            obj_id = _bytes_to_hex(val)
        elif fn == 2 and wt == 2:
            src_id = _bytes_to_hex(val)
        elif fn == 4 and wt == 2:
            # Timestamp: field 1 = seconds (varint)
            for tfn, twt, tv in _iter_fields(val):
                if tfn == 1 and twt == 0:
                    ts = float(tv)
        elif fn == 5 and wt == 2:
            lat, lon, alt, speed, heading = _decode_object_state(val)
        elif fn == 7 and wt == 2:
            obj_class = _decode_classification(val)

    if lat is None or lon is None:
        return None

    return {
        "_ts":          ts,
        "_src":         "sapient",
        "sensor_id":    obj_id or "unknown",
        "src_node":     src_id,
        "lat_deg":      round(lat, 6),
        "lon_deg":      round(lon, 6),
        "geo_alt_m":    round(alt, 1) if alt else None,
        "speed_ms":     speed,
        "heading_deg":  heading,
        "sapient_class": obj_class,
    }


def decode_sapient_message(buf: bytes) -> tuple[int, bytes] | None:
    """Return (content_field_number, content_bytes) or None."""
    for fn, wt, val in _iter_fields(buf):
        if fn in (_FIELD_STATUS_REPORT, _FIELD_DETECTION_REPORT) and wt == 2:
            return fn, val
    return None


# ---------------------------------------------------------------------------
# TCP framing — 4-byte big-endian length prefix
# ---------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def iter_sapient_frames(sock: socket.socket):
    """Yield raw protobuf bytes for each SAPIENT message."""
    while True:
        length = struct.unpack(">I", _recv_exact(sock, 4))[0]
        if length == 0 or length > 1_000_000:
            raise ValueError(f"invalid SAPIENT frame length: {length}")
        yield _recv_exact(sock, length)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    topic = "{}/air/sapient/sapient/unknown/aircraft/tracks/v1".format(TOPIC_ROOT)
    print("SAPIENT → Zenoh topic:", topic, flush=True)

    session = zenoh.open(make_config())
    pub = session.declare_publisher(topic)

    try:
        while True:
            try:
                print("Connecting to SAPIENT node {}:{}…".format(args.host, args.port), flush=True)
                sock = socket.create_connection((args.host, args.port), timeout=10)
                sock.settimeout(30)
                print("Connected.", flush=True)

                for frame in iter_sapient_frames(sock):
                    result = decode_sapient_message(frame)
                    if result is None:
                        continue
                    msg_type, content = result
                    if msg_type != _FIELD_DETECTION_REPORT:
                        continue
                    track = decode_detection_report(content)
                    if track is None:
                        continue
                    pub.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                    if args.verbose:
                        print("SAPIENT {} {} lat={} lon={}".format(
                            track["sapient_class"], track["sensor_id"],
                            track["lat_deg"], track["lon_deg"]), flush=True)

            except (EOFError, OSError, TimeoutError, ValueError) as exc:
                print("SAPIENT connection error: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(RECONNECT_S)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="SAPIENT v7/v8 → Zenoh bridge")
    ap.add_argument("--host", default=os.environ.get("SAPIENT_HOST", "127.0.0.1"),
                    help="SAPIENT node manager host")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SAPIENT_PORT", "7001")),
                    help="SAPIENT node manager port (default: 7001)")
    ap.add_argument("--node-id", default=os.environ.get("SAPIENT_NODE_ID", "sapient-01"),
                    help="Node ID label for Zenoh topic")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
