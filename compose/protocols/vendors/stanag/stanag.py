#!/usr/bin/env python3
"""STANAG protocol translators — Zenoh <-> NATO STANAG interfaces.

One file per the repo's protocol convention: every STANAG variant EFDI speaks
lives here. Each variant is a fully self-contained, prefix-namespaced section
(own make_config/helpers, no shared state between sections) — the same
isolation-over-DRY convention as protocols/vendors/asterix/cat.py. Select
which one to run with --proto {4586,4607,4609,5516}.

  --proto 4586  STANAG 4586 UAS VSM/CUCS telemetry     (TCP/UDP -> Zenoh)
  --proto 4607  STANAG 4607 / NATO GMTI ground radar    (Zenoh raw -> tracks)
  --proto 4609  STANAG 4609 / MISB KLV video metadata   (Zenoh raw -> tracks)
  --proto 5516  STANAG 5516 / Link 16 JREAP-C           (UDP -> Zenoh)

Run:
  venv/bin/python3 protocols/vendors/stanag/stanag.py --proto 4586 --host 192.168.1.50 --port 4586
  venv/bin/python3 protocols/vendors/stanag/stanag.py --proto 4607 --zenoh-raw
  venv/bin/python3 protocols/vendors/stanag/stanag.py --proto 4609 --zenoh-raw
  venv/bin/python3 protocols/vendors/stanag/stanag.py --proto 5516 --port 3010 --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
from namespace_prefix import topic_root
from protocols.proto.stanag_pb2 import (
    Stanag4586Track, Stanag4607Track, Stanag4609Track, Stanag5516Track)

from protocols.gateway import ZError, open_session, publish_dual, publish_native, subscribe
from protocols.track_views import native_topic, semantic_topic

# ============================================================================
# STANAG 4586 — UAS VSM/CUCS telemetry
#
# Connects to a UAV Vehicle Specific Module (VSM) as a Control and
# Communications Subsystem (CUCS), receives vehicle telemetry, and publishes
# each vehicle's position to the EFDI Zenoh fabric.
#
# The public NATO material does not define the edition- and VSM-specific
# binary wire profile. The historical EFDI layout below is therefore disabled
# unless STANAG4586_PROFILE=legacy_ed3_approx is explicitly selected after
# checking it against the target VSM ICD. It must not be treated as generic
# STANAG 4586.
#
# Legacy deployment framing:
#   Offset  Size  Field
#   0       2     Message Type (uint16 LE)
#   2       2     Message Size (uint16 LE, total bytes including header)
#   4       2     Instance Number (uint16 LE)
#   6       N     Message body
#
# Legacy deployment message types (verify against your VSM):
#   0x4001  VSM Heartbeat    — liveness, capabilities
#   0x4002  CUCS Heartbeat   — we send this to keep the connection alive
#   0x0001  Vehicle Operating States — lat/lon/alt/heading/speed
#
# IMPORTANT: Message type numbers and body field offsets vary between STANAG
# 4586 editions (2, 3, 4). Verify against the documentation for your specific
# UAV ground station before connecting to live equipment.
#
# Config (compose/.env):
#   STANAG4586_HOST=     VSM hostname/IP
#   STANAG4586_PORT=4586 VSM port (STANAG-assigned: 4586)
# ============================================================================

_4586_TOPIC_ROOT  = topic_root()

_4586_RECONNECT_S   = 10
_4586_HEARTBEAT_S   = 5
_4586_TOPIC_UAV_OUT = "{}/air/stanag_4586/telemetry/civ/aircraft".format(_4586_TOPIC_ROOT)
_4586_ZENOH_RETRY_S = 5

# Historical deployment constants. They are not asserted to be universal.
_4586_MSG_VSM_HEARTBEAT  = 0x4001
_4586_MSG_CUCS_HEARTBEAT = 0x4002
_4586_MSG_VEHICLE_STATE  = 0x0001   # Vehicle Operating States

_4586_HEADER_SIZE = 6
_4586_MAX_FRAME_BYTES = 1_048_576
_4586_SUPPORTED_PROFILE = "legacy_ed3_approx"


def _4586_require_profile() -> None:
    profile = os.environ.get("STANAG4586_PROFILE", "").strip()
    if profile != _4586_SUPPORTED_PROFILE:
        raise SystemExit(
            "STANAG 4586 wire layouts are edition/VSM-profile specific. "
            "Set STANAG4586_PROFILE={} only after validating this legacy layout "
            "against the deployed VSM ICD.".format(_4586_SUPPORTED_PROFILE)
        )


def _4586_build_header(msg_type: int, body_len: int, instance: int = 1) -> bytes:
    total = _4586_HEADER_SIZE + body_len
    return struct.pack("<HHH", msg_type, total, instance)


def _4586_build_cucs_heartbeat(instance: int = 1) -> bytes:
    # Historical body: CUCS ID (uint16), timestamp (uint32), capabilities (uint8).
    body = struct.pack("<HIB", instance, int(time.time()) & 0xFFFFFFFF, 0x01)
    return _4586_build_header(_4586_MSG_CUCS_HEARTBEAT, len(body)) + body


def _4586_recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("VSM connection closed")
        buf += chunk
    return buf


def _4586_recv_message(sock: socket.socket) -> tuple[int, int, bytes]:
    """Read one STANAG 4586 message. Returns (msg_type, instance, body)."""
    hdr = _4586_recv_exact(sock, _4586_HEADER_SIZE)
    msg_type, total_size, instance = struct.unpack("<HHH", hdr)
    if total_size < _4586_HEADER_SIZE:
        raise ValueError(f"invalid STANAG 4586 message size: {total_size}")
    if total_size > _4586_MAX_FRAME_BYTES:
        raise ValueError(f"STANAG 4586 message exceeds {_4586_MAX_FRAME_BYTES} bytes")
    body_size = total_size - _4586_HEADER_SIZE
    body = _4586_recv_exact(sock, body_size) if body_size > 0 else b""
    return msg_type, instance, body


def _4586_decode_vehicle_state(body: bytes, instance: int) -> dict | None:
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
      56      4     float32  Fuel remaining (0.0-1.0)
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
        "uid":        "stanag_4586-vsm-{}".format(instance),
        "vsm_instance": instance,
        "callsign":   "UAV-VSM-{}".format(instance),
        "lat_deg":    round(lat, 6),
        "lon_deg":    round(lon, 6),
        "alt_m":      round(alt_msl, 1),
    }
    if -1000 <= alt_agl <= 100_000:
        track["alt_agl_m"] = round(alt_agl, 1)
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


def _4586_heartbeat_loop(sock: socket.socket, stop_evt: threading.Event):
    while not stop_evt.wait(_4586_HEARTBEAT_S):
        try:
            sock.sendall(_4586_build_cucs_heartbeat())
        except OSError:
            break


def _4586_run_session(host: str, port: int, session: "zenoh.Session", verbose: bool):
    sock = socket.create_connection((host, port), timeout=10)
    print("STANAG 4586 connected to {}:{}".format(host, port), flush=True)

    stop_evt = threading.Event()
    hb_thread = threading.Thread(target=_4586_heartbeat_loop,
                                 args=(sock, stop_evt), daemon=True)
    hb_thread.start()

    try:
        while True:
            msg_type, instance, body = _4586_recv_message(sock)

            if msg_type == _4586_MSG_VSM_HEARTBEAT:
                if verbose:
                    print("VSM heartbeat instance={}".format(instance), flush=True)

            elif msg_type == _4586_MSG_VEHICLE_STATE:
                track = _4586_decode_vehicle_state(body, instance)
                if track:
                    publish_dual(session, _4586_TOPIC_UAV_OUT, track, Stanag4586Track)
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


def _4586_recv_message_from_bytes(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < _4586_HEADER_SIZE:
        raise ValueError("short STANAG 4586 frame")
    msg_type, total_size, instance = struct.unpack_from("<HHH", frame)
    if total_size != len(frame):
        raise ValueError("STANAG 4586 frame length mismatch")
    return msg_type, instance, frame[_4586_HEADER_SIZE:]


def _4586_run(args):
    _4586_require_profile()
    if args.zenoh_raw:
        return _4586_run_zenoh_raw(args)
    while True:
        try:
            session = open_session()
            break
        except ZError as exc:
            print("STANAG4586 Zenoh connect failed: {} — retry in {}s".format(exc, _4586_ZENOH_RETRY_S), flush=True)
            time.sleep(_4586_ZENOH_RETRY_S)
    print("STANAG 4586 layer started", flush=True)
    print("  VSM: {}:{}".format(args.host, args.port), flush=True)
    print("  Profile: {} (deployment-validated opt-in required)".format(_4586_SUPPORTED_PROFILE), flush=True)

    while True:
        try:
            _4586_run_session(args.host, args.port, session, args.verbose)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("STANAG4586 error: {} — retry in {}s".format(exc, _4586_RECONNECT_S), flush=True)
            time.sleep(_4586_RECONNECT_S)

    session.close()


def _4586_run_zenoh_raw(args):
    """Decode complete or concatenated Ed.3 frames from a Zenoh raw topic."""
    while True:
        try:
            session = open_session()
            break
        except ZError as exc:
            print("STANAG4586 raw Zenoh connect failed: {} — retry in {}s".format(exc, _4586_ZENOH_RETRY_S), flush=True)
            time.sleep(_4586_ZENOH_RETRY_S)
    topic = args.raw_topic or _4586_TOPIC_ROOT + "/raw/stanag_4586/**"
    buffer = bytearray()

    def on_sample(sample):
        try:
            data = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
            buffer.extend(data)
            while len(buffer) >= _4586_HEADER_SIZE:
                total_size = struct.unpack_from("<H", buffer, 2)[0]
                if total_size < _4586_HEADER_SIZE or total_size > _4586_MAX_FRAME_BYTES:
                    del buffer[0]
                    continue
                if len(buffer) < total_size:
                    break
                msg_type, _instance, body = _4586_recv_message_from_bytes(bytes(buffer[:total_size]))
                del buffer[:total_size]
                if msg_type == _4586_MSG_VEHICLE_STATE:
                    track = _4586_decode_vehicle_state(body, _instance)
                    if track:
                        publish_dual(session, _4586_TOPIC_UAV_OUT, track, Stanag4586Track)
        except Exception as exc:
            print("STANAG4586 raw decode error:", exc, flush=True)

    subscriber = subscribe(session, topic, on_sample)
    print("STANAG4586 Zenoh raw translator subscribed to {}".format(topic), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def _4586_main():
    ap = argparse.ArgumentParser(description="STANAG 4586 CUCS -> Zenoh bridge")
    ap.add_argument("--host", default=os.environ.get("STANAG4586_HOST", ""),
                    help="VSM hostname or IP")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("STANAG4586_PORT", "4586")))
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--zenoh-raw", action="store_true",
                    help="decode bytes from .../raw/stanag_4586/** instead of opening VSM TCP")
    ap.add_argument("--raw-topic", default=os.environ.get("STANAG4586_RAW_TOPIC", ""))
    args = ap.parse_args()
    if not args.host and not args.zenoh_raw:
        raise SystemExit("Set STANAG4586_HOST in .env or pass --host")
    _4586_run(args)


# ============================================================================
# STANAG 4607 — NATO GMTI (Ground Moving Target Indicator) Format
#
# Binary, packet/segment-oriented format for ground-radar moving-target
# reports (the "ASTERIX equivalent" for GMTI radar — AGS/JSTARS-class
# platforms). The primary STANAG text is NATO-restricted, same situation as
# 4586's VSM ICD; unlike 4586 there is no vendor-specific ambiguity here,
# because Wireshark ships a real, actively-maintained open-source dissector
# (GPL-2.0-or-later) for this exact wire format. That dissector — not this
# project's memory of the format — is the source for every byte offset and
# scale factor below; see docs/references/stanag/STANAG.md for the exact
# fetched URL and what was cross-checked.
#
# The STANAG defines the message, not the bearer: real deployments carry it
# over TCP, UDP, or a tactical data link depending on installation, so — like
# 4609 — this decodes whatever complete packets a bridge has already placed
# on .../raw/stanag_4607/**; it never owns a socket itself.
#
# Packet = 32-byte header + one or more segments (1-byte type + 4-byte size
# + payload). Segments implemented: Mission(1), Dwell(2) — including every
# Target Report inside it, one NormalizedTrack each — Job Definition(5), and
# Platform Location(13). Unknown segment types are skipped by their declared
# size rather than guessed.
#
# Target Report's Delta Latitude/Longitude (2-byte, compact alternative to
# the 4-byte absolute lat/lon) has no resolved scale even in the reference
# dissector itself — it displays the raw signed integer with no unit
# conversion. Kept exactly that way here: raw ints, not a guessed formula.
# A track's lat_deg/lon_deg is only ever set from the absolute Hi-Res
# Latitude/Longitude fields, which do have a confirmed formula.
# ============================================================================

_4607_TOPIC_ROOT = topic_root()
_4607_TRACK_TOPIC = "{}/land/stanag_4607/gmti/neutral/vehicle".format(_4607_TOPIC_ROOT)
_4607_RECONNECT_S = float(os.environ.get("STANAG4607_RECONNECT_S", "10"))

_4607_MISSION_SEGMENT = 1
_4607_DWELL_SEGMENT = 2
_4607_JOB_DEFINITION_SEGMENT = 5
_4607_PLATFORM_LOCATION_SEGMENT = 13

# D-numbers are STANAG 4607's own field numbers (Table 2-4 Dwell Segment /
# Table 2-6 Target Report), not bit positions — the existence-mask bit
# location for each is m*8+n per the format's own mapping figure, exactly as
# the reference dissector defines it (packet-stanag4607.c's D-prefixed
# macros).
_4607_D = {  # dwell-segment existence mask: field name -> bit offset in the 64-bit mask
    "scale_lat": 6 * 8 + 7, "scale_lon": 6 * 8 + 6, "unc_along": 6 * 8 + 5,
    "unc_cross": 6 * 8 + 4, "unc_alt": 6 * 8 + 3, "track": 6 * 8 + 2,
    "speed": 6 * 8 + 1, "vert_velocity": 6 * 8 + 0, "track_unc": 5 * 8 + 7,
    "speed_unc": 5 * 8 + 6, "vv_unc": 5 * 8 + 5, "plat_heading": 5 * 8 + 4,
    "plat_pitch": 5 * 8 + 3, "plat_roll": 5 * 8 + 2, "sensor_heading": 4 * 8 + 5,
    "sensor_pitch": 4 * 8 + 4, "sensor_roll": 4 * 8 + 3, "mdv": 4 * 8 + 2,
}
_4607_D32 = {  # target-report existence mask: field name -> bit offset
    "index": 4 * 8 + 1, "lat": 4 * 8 + 0, "lon": 3 * 8 + 7,
    "delta_lat": 3 * 8 + 6, "delta_lon": 3 * 8 + 5, "height": 3 * 8 + 4,
    "radial": 3 * 8 + 3, "wrap": 3 * 8 + 2, "snr": 3 * 8 + 1,
    "classification": 3 * 8 + 0, "prob": 2 * 8 + 7, "unc_slant": 2 * 8 + 6,
    "unc_cross": 2 * 8 + 5, "unc_height": 2 * 8 + 4, "unc_radial": 2 * 8 + 3,
    "tag_app": 2 * 8 + 2, "tag_entity": 2 * 8 + 1, "section": 2 * 8 + 0,
}

_4607_SENSOR_TYPES = {
    0: "unidentified", 1: "other", 2: "hisar", 3: "astor",
    4: "rotary_wing_radar", 5: "global_hawk_sensor", 6: "horizon", 7: "apy-3",
    8: "apy-6", 9: "apy-8_lynx_i", 10: "radarsat2", 11: "asars-2a",
    12: "tesar", 13: "mp-rtip", 14: "apg-77", 15: "apg-79", 16: "apg-81",
    17: "apy-6v1", 18: "spy-i_lynx_ii", 19: "sidm", 20: "limit",
    21: "tcar_ags_a321", 22: "lsrs_sensor", 23: "ugs_single_sensor",
    24: "ugs_cluster_sensor", 25: "imaster_gmti", 26: "an/zpy-1_starlite",
    27: "vader", 255: "no_statement",
}
_4607_RADAR_MODES = {
    0: "unspecified", 1: "mti", 2: "hrr", 3: "uhrr", 4: "hur", 5: "fti",
}
_4607_TARGET_CLASSES = {
    0: "no_info_live", 1: "tracked_vehicle_live", 2: "wheeled_vehicle_live",
    3: "rotary_wing_aircraft_live", 4: "fixed_wing_aircraft_live",
    5: "stationary_rotator_live", 6: "maritime_live", 7: "beacon_live",
    8: "amphibious_live", 9: "person_live", 10: "vehicle_live",
    11: "animal_live", 12: "large_multi_return_land_live",
    13: "large_multi_return_maritime_live", 126: "other_live",
    127: "unknown_live", 128: "no_info_simulated",
    129: "tracked_vehicle_simulated", 130: "wheeled_vehicle_simulated",
    131: "rotary_wing_aircraft_simulated", 132: "fixed_wing_aircraft_simulated",
    133: "stationary_rotator_simulated", 134: "maritime_simulated",
    135: "beacon_simulated", 136: "amphibious_simulated",
    137: "person_simulated", 138: "vehicle_simulated", 139: "animal_simulated",
    140: "large_multi_return_land_simulated",
    141: "large_multi_return_maritime_simulated", 143: "tagging_device",
    254: "other_simulated", 255: "unknown_simulated",
}


def _4607_sa32(raw: int) -> float:
    """Signed Angle, 32-bit: latitude, degrees/2^30, range ~-90..+90."""
    return (raw / (1 << 30)) * 45.0


def _4607_ba32(raw: int) -> float:
    """Binary Angle, 32-bit unsigned: longitude, degrees/2^30 * 90, 0..360;
    normalized to -180..180 to match every other category's lon_deg."""
    deg = (raw / (1 << 30)) * 90.0
    return deg - 360.0 if deg > 180.0 else deg


def _4607_sa16(raw: int) -> float:
    return (raw / (1 << 14)) * 90.0


def _4607_ba16(raw: int) -> float:
    return (raw / (1 << 14)) * 90.0


def _4607_centimeters(raw: int) -> float:
    return raw / 100.0


def _4607_decimeters(raw: int) -> float:
    return raw / 10.0


def _4607_kilometers(raw: int) -> float:
    return raw / 128.0


def _4607_speed_mmps(raw: int) -> float:
    return raw / 1000.0


def _4607_speed_centi(raw: int) -> float:
    return raw / 100.0


def _4607_speed_deci(raw: int) -> float:
    return raw / 10.0


def _4607_millisec(raw: int) -> float:
    return raw / 1000.0


def _4607_s16(b: bytes) -> int: return struct.unpack(">h", b)[0]
def _4607_u16(b: bytes) -> int: return struct.unpack(">H", b)[0]
def _4607_s32(b: bytes) -> int: return struct.unpack(">i", b)[0]
def _4607_u32(b: bytes) -> int: return struct.unpack(">I", b)[0]


def _4607_decode_header(data: bytes) -> dict | None:
    """32-byte packet header. Returns None if the buffer is too short."""
    if len(data) < 32:
        return None
    return {
        "edition": chr(data[0]), "version": chr(data[1]),
        "packet_size": _4607_u32(data[2:6]),
        "nationality": data[6:8].decode("ascii", "replace"),
        "security_classification": data[8],
        "security_system": data[9:11].decode("ascii", "replace"),
        "security_code": _4607_u16(data[11:13]),
        "exercise_indicator": data[13],
        "platform_id": data[14:24].decode("ascii", "replace").strip("\x00").strip(),
        "mission_id": _4607_u32(data[24:28]),
        "job_id": _4607_u32(data[28:32]),
    }


def _4607_decode_mission(data: bytes, pos: int) -> tuple[dict, int]:
    out = {
        "mission_plan": data[pos:pos + 12].decode("ascii", "replace").strip("\x00").strip(),
        "flight_plan": data[pos + 12:pos + 24].decode("ascii", "replace").strip("\x00").strip(),
        "platform_type": data[pos + 24],
        "platform_config": data[pos + 25:pos + 35].decode("ascii", "replace").strip("\x00").strip(),
        "mission_year": _4607_u16(data[pos + 35:pos + 37]),
        "mission_month": data[pos + 37],
        "mission_day": data[pos + 38],
    }
    return out, pos + 39


def _4607_decode_jobdef(data: bytes, pos: int) -> tuple[dict, int]:
    start = pos
    sensor_type = data[pos + 4]
    out = {
        "job_id": _4607_u32(data[pos:pos + 4]),
        "sensor_type": _4607_SENSOR_TYPES.get(sensor_type, sensor_type),
        "sensor_model": data[pos + 5:pos + 11].decode("ascii", "replace").strip("\x00").strip(),
        "target_filtering_flag": data[pos + 11],
        "radar_priority": data[pos + 12],
        "bounding_area_a_lat_deg": round(_4607_sa32(_4607_s32(data[pos + 13:pos + 17])), 7),
        "bounding_area_a_lon_deg": round(_4607_ba32(_4607_u32(data[pos + 17:pos + 21])), 7),
        "bounding_area_b_lat_deg": round(_4607_sa32(_4607_s32(data[pos + 21:pos + 25])), 7),
        "bounding_area_b_lon_deg": round(_4607_ba32(_4607_u32(data[pos + 25:pos + 29])), 7),
        "bounding_area_c_lat_deg": round(_4607_sa32(_4607_s32(data[pos + 29:pos + 33])), 7),
        "bounding_area_c_lon_deg": round(_4607_ba32(_4607_u32(data[pos + 33:pos + 37])), 7),
        "bounding_area_d_lat_deg": round(_4607_sa32(_4607_s32(data[pos + 37:pos + 41])), 7),
        "bounding_area_d_lon_deg": round(_4607_ba32(_4607_u32(data[pos + 41:pos + 45])), 7),
        "radar_mode": _4607_RADAR_MODES.get(data[pos + 45], data[pos + 45]),
        "revisit_interval_s": _4607_u16(data[pos + 46:pos + 48]),
    }
    pos = start + 48
    # Nominal sensor uncertainty / sensing performance fields, all present.
    out["nominal_pos_unc_along_m"] = _4607_u16(data[pos:pos + 2]); pos += 2
    out["nominal_pos_unc_cross_m"] = _4607_u16(data[pos:pos + 2]); pos += 2
    out["nominal_pos_unc_alt_m"] = _4607_u16(data[pos:pos + 2]); pos += 2
    out["nominal_heading_unc_deg"] = data[pos]; pos += 1
    out["nominal_speed_unc_ms"] = _4607_u16(data[pos:pos + 2]); pos += 2
    out["nominal_sensing_slant_range_unc_m"] = _4607_u16(data[pos:pos + 2]); pos += 2
    out["nominal_sensing_cross_range_unc_m"] = _4607_u16(data[pos:pos + 2]); pos += 2
    out["nominal_sensing_vlos_unc_ms"] = _4607_u16(data[pos:pos + 2]); pos += 2
    out["nominal_sensing_mdv_ms"] = data[pos]; pos += 1
    out["nominal_detection_prob_pct"] = data[pos]; pos += 1
    out["nominal_false_alarm_density"] = data[pos]; pos += 1
    out["terrain_elevation_model"] = data[pos]; pos += 1
    out["geoid_model"] = data[pos]; pos += 1
    return out, pos


def _4607_decode_platform_location(data: bytes, pos: int) -> tuple[dict, int]:
    out = {
        "location_time_s": round(_4607_millisec(_4607_u32(data[pos:pos + 4])), 3),
        "platform_lat_deg": round(_4607_sa32(_4607_s32(data[pos + 4:pos + 8])), 7),
        "platform_lon_deg": round(_4607_ba32(_4607_u32(data[pos + 8:pos + 12])), 7),
        "platform_alt_m": round(_4607_centimeters(_4607_s32(data[pos + 12:pos + 16])), 2),
        "platform_track_deg": round(_4607_ba16(_4607_u16(data[pos + 16:pos + 18])), 2),
        "platform_speed_ms": round(_4607_speed_mmps(_4607_u32(data[pos + 18:pos + 22])), 3),
        "platform_vertical_velocity_ms": round(
            _4607_speed_deci(struct.unpack(">b", data[pos + 22:pos + 23])[0]), 1),
    }
    return out, pos + 23


def _4607_decode_target_report(data: bytes, pos: int, mask: int) -> tuple[dict, int]:
    out: dict = {}

    def bit(name: str) -> bool:
        return bool((mask >> _4607_D32[name]) & 1)

    if bit("index"):
        out["target_report_index"] = _4607_u16(data[pos:pos + 2]); pos += 2
    if bit("lat"):
        out["lat_deg"] = round(_4607_sa32(_4607_s32(data[pos:pos + 4])), 7); pos += 4
    if bit("lon"):
        out["lon_deg"] = round(_4607_ba32(_4607_u32(data[pos:pos + 4])), 7); pos += 4
    if bit("delta_lat"):
        # No resolved scale even in the reference dissector — raw signed int.
        out["delta_lat_raw"] = _4607_s16(data[pos:pos + 2]); pos += 2
    if bit("delta_lon"):
        out["delta_lon_raw"] = _4607_s16(data[pos:pos + 2]); pos += 2
    if bit("height"):
        out["height_m"] = _4607_s16(data[pos:pos + 2]); pos += 2
    if bit("radial"):
        out["radial_velocity_ms"] = round(_4607_speed_centi(_4607_s16(data[pos:pos + 2])), 2); pos += 2
    if bit("wrap"):
        out["wrap_velocity_ms"] = round(_4607_speed_centi(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("snr"):
        out["snr_db"] = struct.unpack(">b", data[pos:pos + 1])[0]; pos += 1
    if bit("classification"):
        cls = data[pos]; pos += 1
        out["target_classification"] = _4607_TARGET_CLASSES.get(cls, cls)
        out["target_classification_code"] = cls
    if bit("prob"):
        out["target_class_probability_pct"] = data[pos]; pos += 1
    if bit("unc_slant"):
        out["unc_slant_range_m"] = round(_4607_centimeters(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("unc_cross"):
        out["unc_cross_range_m"] = round(_4607_decimeters(_4607_u16(data[pos:pos + 2])), 1); pos += 2
    if bit("unc_height"):
        out["unc_height_m"] = data[pos]; pos += 1
    if bit("unc_radial"):
        out["unc_radial_velocity_ms"] = round(_4607_speed_centi(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("tag_app"):
        out["truth_tag_application"] = data[pos]; pos += 1
    if bit("tag_entity"):
        out["truth_tag_entity"] = _4607_u32(data[pos:pos + 4]); pos += 4
    if bit("section"):
        out["radar_cross_section_dbsm"] = struct.unpack(">b", data[pos:pos + 1])[0]; pos += 1
    return out, pos


def _4607_decode_dwell(data: bytes, pos: int) -> tuple[dict, list[dict], int]:
    """Returns (dwell context dict, list of target-report dicts, next offset)."""
    mask = _4607_u32(data[pos:pos + 4]) << 32 | _4607_u32(data[pos + 4:pos + 8])
    pos += 8

    def bit(name: str) -> bool:
        return bool((mask >> _4607_D[name]) & 1)

    dwell: dict = {
        "revisit_index": _4607_s16(data[pos:pos + 2]), }
    pos += 2
    dwell["dwell_index"] = _4607_u16(data[pos:pos + 2]); pos += 2
    dwell["last_dwell_of_revisit"] = bool(data[pos]); pos += 1
    target_count = _4607_u16(data[pos:pos + 2]); pos += 2
    dwell["dwell_time_s"] = round(_4607_millisec(_4607_u32(data[pos:pos + 4])), 3); pos += 4
    dwell["sensor_lat_deg"] = round(_4607_sa32(_4607_s32(data[pos:pos + 4])), 7); pos += 4
    dwell["sensor_lon_deg"] = round(_4607_ba32(_4607_u32(data[pos:pos + 4])), 7); pos += 4
    dwell["sensor_alt_m"] = round(_4607_centimeters(_4607_s32(data[pos:pos + 4])), 2); pos += 4

    if bit("scale_lat"):
        dwell["scale_lat_raw"] = _4607_s32(data[pos:pos + 4]); pos += 4
    if bit("scale_lon"):
        dwell["scale_lon_raw"] = _4607_u32(data[pos:pos + 4]); pos += 4
    if bit("unc_along"):
        dwell["sensor_unc_along_m"] = round(_4607_centimeters(_4607_u32(data[pos:pos + 4])), 2); pos += 4
    if bit("unc_cross"):
        dwell["sensor_unc_cross_m"] = round(_4607_centimeters(_4607_u32(data[pos:pos + 4])), 2); pos += 4
    if bit("unc_alt"):
        dwell["sensor_unc_alt_m"] = round(_4607_centimeters(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("track"):
        dwell["sensor_track_deg"] = round(_4607_ba16(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("speed"):
        dwell["sensor_speed_ms"] = round(_4607_speed_mmps(_4607_u32(data[pos:pos + 4])), 3); pos += 4
    if bit("vert_velocity"):
        dwell["sensor_vertical_velocity_ms"] = round(_4607_speed_deci(struct.unpack(">b", data[pos:pos + 1])[0]), 1); pos += 1
    if bit("track_unc"):
        dwell["sensor_track_unc_deg"] = data[pos]; pos += 1
    if bit("speed_unc"):
        dwell["sensor_speed_unc_ms"] = round(_4607_speed_mmps(_4607_u16(data[pos:pos + 2])), 3); pos += 2
    if bit("vv_unc"):
        dwell["sensor_vv_unc_ms"] = round(_4607_speed_centi(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("plat_heading"):
        dwell["platform_heading_deg"] = round(_4607_ba16(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("plat_pitch"):
        dwell["platform_pitch_deg"] = round(_4607_sa16(_4607_s16(data[pos:pos + 2])), 3); pos += 2
    if bit("plat_roll"):
        dwell["platform_roll_deg"] = round(_4607_sa16(_4607_s16(data[pos:pos + 2])), 3); pos += 2

    dwell["dwell_area_center_lat_deg"] = round(_4607_sa32(_4607_s32(data[pos:pos + 4])), 7); pos += 4
    dwell["dwell_area_center_lon_deg"] = round(_4607_ba32(_4607_u32(data[pos:pos + 4])), 7); pos += 4
    dwell["dwell_area_range_half_extent_km"] = round(_4607_kilometers(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    dwell["dwell_area_angle_half_extent_deg"] = round(_4607_ba16(_4607_u16(data[pos:pos + 2])), 2); pos += 2

    if bit("sensor_heading"):
        dwell["sensor_heading_deg"] = round(_4607_ba16(_4607_u16(data[pos:pos + 2])), 2); pos += 2
    if bit("sensor_pitch"):
        dwell["sensor_pitch_deg"] = round(_4607_sa16(_4607_s16(data[pos:pos + 2])), 3); pos += 2
    if bit("sensor_roll"):
        dwell["sensor_roll_deg"] = round(_4607_sa16(_4607_s16(data[pos:pos + 2])), 3); pos += 2
    if bit("mdv"):
        dwell["mdv_ms"] = round(_4607_speed_deci(data[pos]), 1); pos += 1

    targets = []
    for _ in range(target_count):
        report, pos = _4607_decode_target_report(data, pos, mask)
        targets.append(report)
    return dwell, targets, pos


def _4607_decode_packet(data: bytes) -> tuple[dict, list[dict]] | None:
    """Returns (packet header dict, list of NormalizedTrack-shaped dicts) or
    None if the buffer is too short to even hold a header."""
    header = _4607_decode_header(data)
    if header is None:
        return None
    tracks: list[dict] = []
    pos = 32
    size = min(header["packet_size"], len(data))
    while pos + 5 <= size:
        seg_type = data[pos]
        seg_size = _4607_u32(data[pos + 1:pos + 5])
        if seg_size < 5:
            break
        seg_end = pos + seg_size
        body = pos + 5
        if seg_type == _4607_MISSION_SEGMENT:
            _mission, _ = _4607_decode_mission(data, body)
        elif seg_type == _4607_JOB_DEFINITION_SEGMENT:
            _jobdef, _ = _4607_decode_jobdef(data, body)
        elif seg_type == _4607_PLATFORM_LOCATION_SEGMENT:
            _ploc, _ = _4607_decode_platform_location(data, body)
        elif seg_type == _4607_DWELL_SEGMENT:
            dwell, targets, _ = _4607_decode_dwell(data, body)
            for report in targets:
                track = {
                    "_ts": time.time(),
                    "_src": "STANAG 4607",
                    "uid": "stanag4607-{}-{}".format(
                        header["job_id"], report.get("target_report_index", len(tracks))),
                    "job_id": header["job_id"],
                    "dwell_time_s": dwell["dwell_time_s"],
                    "sensor_lat_deg": dwell["sensor_lat_deg"],
                    "sensor_lon_deg": dwell["sensor_lon_deg"],
                    "sensor_alt_m": dwell["sensor_alt_m"],
                }
                track.update(report)
                tracks.append(track)
        pos = seg_end
    return header, tracks


def _4607_run(args):
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("STANAG4607 Zenoh connect failed: {} — retry in {}s".format(exc, _4607_RECONNECT_S), flush=True)
            time.sleep(_4607_RECONNECT_S)

    topic = args.raw_topic or "{}/raw/stanag_4607/**".format(_4607_TOPIC_ROOT)

    def on_sample(sample):
        data = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
        try:
            result = _4607_decode_packet(data)
        except (struct.error, IndexError, ValueError) as exc:
            print("STANAG4607 decode error: {}".format(exc), flush=True)
            return
        if result is None:
            return
        _header, tracks = result
        for track in tracks:
            if "lat_deg" in track and "lon_deg" in track:
                publish_dual(session, _4607_TRACK_TOPIC, track, Stanag4607Track, wrapper_field="track")
            if args.verbose:
                print("STANAG4607 target job={} idx={} lat={} lon={}".format(
                    track.get("job_id"), track.get("target_report_index"),
                    track.get("lat_deg"), track.get("lon_deg")), flush=True)

    subscriber = subscribe(session, topic, on_sample)
    print("STANAG 4607 GMTI decoder started, subscribed to {}".format(topic), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def _4607_main():
    parser = argparse.ArgumentParser(description="STANAG 4607 GMTI decoder — Zenoh raw -> tracks")
    parser.add_argument("--zenoh-raw", action="store_true",
                        help="decode packets from .../raw/stanag_4607/** (default and only mode)")
    parser.add_argument("--raw-topic", default=os.environ.get("STANAG4607_RAW_TOPIC", ""),
                        help="override the raw packet subscription key expression")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    _4607_run(args)


# ============================================================================
# STANAG 4609 — MISB KLV video metadata
#
# Subscribes to the raw MISB KLV packets that bridges/4609_bridge.py ingests
# from the SRT/MPEG-TS transport, decodes a small, safe subset of common MISB
# ST 0601 fields, and publishes positioned frames as canonical tracks
# (SAPIENT / JSON / protobuf views). SRT/ffmpeg ingest is the bridge's job;
# this protocol never touches the transport and never transcodes the video
# essence itself.
#
# The decoder is intentionally conservative:
# - a positioned frame (sensor or frame-centre lat/lon present) becomes a
#   track; its exact KLV bytes ride the /raw sibling of the object key
# - non-positioned KLV carries no canonical track: its bytes already live on
#   the fabric via the ingress bridge, so the decoder stays silent
#
# Config (compose/.env):
#   STANAG4609_SOURCE=optional-stream-name  # ingress source tag (contact identity)
# ============================================================================

import base64
import queue  # noqa: F401 — kept for parity with historical imports; unused directly
from datetime import datetime, timezone

_4609_TOPIC_ROOT = topic_root()
_4609_SOURCE     = os.environ.get("STANAG4609_SOURCE", "stanag_4609").strip() or "stanag_4609"
_4609_RECONNECT_S = float(os.environ.get("STANAG4609_RECONNECT_S", "10"))
_4609_READ_CHUNK  = int(os.environ.get("STANAG4609_READ_CHUNK", "65536"))
_4609_MAX_KLV_BYTES = int(os.environ.get("STANAG4609_MAX_KLV_BYTES", "1048576"))

_4609_KLV_PREFIX = b"\x06\x0E\x2B\x34"
_4609_ST0601_LOCAL_SET_KEY = bytes.fromhex("060e2b34020b01010e01030101000000")
_4609_TRACK_TOPIC = "{}/air/stanag_4609/camera/unknown/uav".format(_4609_TOPIC_ROOT)


def _4609_decode_ber(data: bytes) -> tuple[int, int]:
    if not data:
        raise ValueError("missing BER length")
    first = data[0]
    if first < 0x80:
        return first, 1
    count = first & 0x7F
    if count == 0:
        raise ValueError("indefinite BER lengths are not supported")
    if count > 8:
        raise ValueError("BER length exceeds 64 bits")
    if len(data) < 1 + count:
        raise ValueError("short BER length")
    return int.from_bytes(data[1:1 + count], "big"), 1 + count


def _4609_decode_unsigned(raw: bytes, minimum: float, maximum: float) -> float:
    value = int.from_bytes(raw, "big", signed=False)
    scale = (1 << (8 * len(raw))) - 1
    return minimum + (value / scale) * (maximum - minimum) if scale else minimum


def _4609_decode_signed(raw: bytes, minimum: float, maximum: float) -> float:
    value = int.from_bytes(raw, "big", signed=True)
    min_raw = -(1 << (8 * len(raw) - 1))
    max_raw = (1 << (8 * len(raw) - 1)) - 1
    if value == min_raw:
        raise ValueError("MISB reserved signed integer error value")
    return minimum + ((value + max_raw) / (2 * max_raw)) * (maximum - minimum)


def _4609_looks_like_klv_key(key: bytes) -> bool:
    return len(key) == 16 and key.startswith(_4609_KLV_PREFIX)


def _4609_encode_ber(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative BER length")
    if value < 0x80:
        return bytes((value,))
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(raw),)) + raw


def _4609_decode_ber_oid(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(10):
        if pos >= len(data):
            raise ValueError("truncated BER-OID tag")
        byte = data[pos]; pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise ValueError("BER-OID tag exceeds 64 bits")


def _4609_parse_local_set(value: bytes) -> dict[int, bytes]:
    tags: dict[int, bytes] = {}
    pos = 0
    while pos < len(value):
        tag, pos = _4609_decode_ber_oid(value, pos)
        tag_len, len_size = _4609_decode_ber(value[pos:])
        pos += len_size
        if pos + tag_len > len(value):
            raise ValueError("truncated MISB local-set value")
        tags[tag] = value[pos:pos + tag_len]
        pos += tag_len
    return tags


def _4609_decode_st0601(tags: dict[int, bytes]) -> dict[str, object]:
    out: dict[str, object] = {}

    def mapped_signed(raw: bytes | None, size: int, minimum: float, maximum: float):
        if raw is None or len(raw) != size:
            return None
        try:
            return _4609_decode_signed(raw, minimum, maximum)
        except ValueError:
            # The most-negative code word is MISB's reserved error indicator,
            # not the minimum coordinate/angle. Omit that field but retain the
            # rest of the Local Set and its exact raw KLV bytes.
            return None

    timestamp = tags.get(2)
    if timestamp and len(timestamp) == 8:
        timestamp_us = int.from_bytes(timestamp, "big", signed=False)
        out["timestamp_us"] = timestamp_us
        try:
            out["timestamp_iso"] = datetime.fromtimestamp(timestamp_us / 1_000_000, tz=timezone.utc).isoformat()
        except (OverflowError, ValueError):
            pass

    heading = tags.get(5)
    if heading and len(heading) == 2:
        out["platform_heading_deg"] = round(_4609_decode_unsigned(heading, 0.0, 360.0), 3)

    altitude = tags.get(15)
    if altitude and len(altitude) == 2:
        out["sensor_alt_m"] = round(_4609_decode_unsigned(altitude, -900.0, 19_000.0), 2)

    lat = tags.get(13)
    lat_value = mapped_signed(lat, 4, -90.0, 90.0)
    if lat_value is not None:
        out["sensor_lat_deg"] = round(lat_value, 6)

    lon = tags.get(14)
    lon_value = mapped_signed(lon, 4, -180.0, 180.0)
    if lon_value is not None:
        out["sensor_lon_deg"] = round(lon_value, 6)

    rel_az = tags.get(18)
    if rel_az and len(rel_az) == 4:
        out["sensor_relative_azimuth_deg"] = round(_4609_decode_unsigned(rel_az, 0.0, 360.0), 3)

    rel_el = tags.get(19)
    rel_el_value = mapped_signed(rel_el, 4, -180.0, 180.0)
    if rel_el_value is not None:
        out["sensor_relative_elevation_deg"] = round(rel_el_value, 3)

    frame_lat = tags.get(23)
    frame_lat_value = mapped_signed(frame_lat, 4, -90.0, 90.0)
    if frame_lat_value is not None:
        out["frame_center_lat_deg"] = round(frame_lat_value, 6)

    frame_lon = tags.get(24)
    frame_lon_value = mapped_signed(frame_lon, 4, -180.0, 180.0)
    if frame_lon_value is not None:
        out["frame_center_lon_deg"] = round(frame_lon_value, 6)

    frame_altitude = tags.get(25)
    if frame_altitude and len(frame_altitude) == 2:
        out["frame_center_alt_m"] = round(_4609_decode_unsigned(frame_altitude, -900.0, 19_000.0), 2)

    raw_tags: dict[str, str] = {}
    for tag, raw in tags.items():
        if tag in {2, 5, 13, 14, 15, 18, 19, 23, 24, 25}:
            continue
        raw_tags[str(tag)] = raw.hex()
    if raw_tags:
        out["raw_tags_hex"] = raw_tags

    return out


def _4609_parse_klv_packets(stream):
    buf = bytearray()
    while True:
        chunk = stream.read(_4609_READ_CHUNK)
        if not chunk:
            return
        buf.extend(chunk)
        while True:
            if len(buf) < 17:
                break
            if not _4609_looks_like_klv_key(bytes(buf[:16])):
                idx = bytes(buf).find(_4609_KLV_PREFIX, 1)
                if idx < 0:
                    # Preserve a possible prefix split across read() chunks.
                    keep = min(len(buf), len(_4609_KLV_PREFIX) - 1)
                    if len(buf) > keep:
                        del buf[:-keep]
                    break
                del buf[:idx]
                continue
            first_length = buf[16]
            if first_length & 0x80:
                length_octets = first_length & 0x7F
                if length_octets == 0 or length_octets > 8:
                    del buf[0]
                    continue
                if len(buf) < 17 + length_octets:
                    break
            try:
                value_len, len_size = _4609_decode_ber(bytes(buf[16:]))
            except ValueError:
                if buf:
                    del buf[0]
                break
            if value_len > _4609_MAX_KLV_BYTES:
                del buf[0]
                continue
            total_len = 16 + len_size + value_len
            if len(buf) < total_len:
                break
            key = bytes(buf[:16])
            value = bytes(buf[16 + len_size:total_len])
            del buf[:total_len]
            yield key, value


def _4609_split_klv_packet(packet: bytes) -> tuple[bytes, bytes] | None:
    if len(packet) < 17:
        return None
    key = packet[:16]
    if not _4609_looks_like_klv_key(key):
        return None
    value_len, len_size = _4609_decode_ber(packet[16:])
    if value_len > _4609_MAX_KLV_BYTES:
        return None
    total_len = 16 + len_size + value_len
    if len(packet) != total_len:
        return None
    return key, packet[16 + len_size:total_len]


def _4609_publish_packet(session, packet_index, key, value, verbose, stream_id, source):
    """Decode one raw KLV packet; publish a track only if it carries a position.

    Non-positioned KLV produces no canonical output — its exact bytes already
    live on the fabric (the ingress bridge put them there), so re-publishing
    here would only echo into this decoder's own raw subscription.
    """
    raw_packet = key + _4609_encode_ber(len(value)) + value
    payload = {
        "_ts": time.time(),
        "_src": source,
        "stream_id": stream_id,
        "source_tag": source,
        "packet_index": packet_index,
        "klv_key": key.hex(),
        "klv_len": len(value),
        "klv_raw_b64": base64.b64encode(raw_packet).decode("ascii"),
    }

    if _4609_looks_like_klv_key(key) and key == _4609_ST0601_LOCAL_SET_KEY:
        tags = _4609_parse_local_set(value)
        decoded = _4609_decode_st0601(tags)
        payload.update(decoded)

        lat = decoded.get("sensor_lat_deg")
        lon = decoded.get("sensor_lon_deg")
        if lat is None: lat = decoded.get("frame_center_lat_deg")
        if lon is None: lon = decoded.get("frame_center_lon_deg")
        if lat is not None and lon is not None:
            payload["lat_deg"] = lat
            payload["lon_deg"] = lon
        if "platform_heading_deg" in decoded:
            payload["heading_deg"] = decoded["platform_heading_deg"]
        altitude = decoded.get("sensor_alt_m")
        if altitude is None: altitude = decoded.get("frame_center_alt_m")
        if altitude is not None: payload["geo_alt_m"] = altitude

    if "lat_deg" not in payload or "lon_deg" not in payload:
        return

    # A positioned frame is a track, so it leaves on the object key in every
    # view — SAPIENT, JSON and the per-protocol protobuf — exactly like the
    # other decoders. The stream is the object: its identity is the ingress
    # source tag, so successive frames update one contact instead of spawning
    # a new key each frame.
    payload["uid"] = stream_id
    obj_key = semantic_topic(_4609_TRACK_TOPIC, payload)
    publish_dual(session, _4609_TRACK_TOPIC, payload, Stanag4609Track,
                 wrapper_field="track")
    # The exact KLV packet rides the /raw sibling of that same object key —
    # it is not embedded in the protobuf, the RawEnvelope is its home.
    publish_native(session, native_topic(obj_key), raw_packet, "stanag_4609",
                   profile="misb-st0601")
    if verbose:
        print("STANAG4609 TRACK {} uid={} key={} len={}".format(
            _4609_TRACK_TOPIC, stream_id, payload["klv_key"][:12], payload["klv_len"]), flush=True)


def _4609_run(args):
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("STANAG4609 Zenoh connect failed: {} — retry in {}s".format(exc, _4609_RECONNECT_S), flush=True)
            time.sleep(_4609_RECONNECT_S)

    topic = args.raw_topic or "{}/raw/stanag_4609/**".format(_4609_TOPIC_ROOT)
    counter = {"i": 0}

    def on_sample(sample):
        data = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
        parsed = _4609_split_klv_packet(data)
        if parsed is None:
            return
        key, value = parsed
        # Contact identity is the ingress source segment of the raw topic
        # (…/raw/stanag_4609/<source>); fall back to the configured SOURCE.
        source = str(sample.key_expr).rstrip("/").rsplit("/", 1)[-1] or _4609_SOURCE
        _4609_publish_packet(session, counter["i"], key, value, args.verbose, source, source)
        counter["i"] += 1

    subscriber = subscribe(session, topic, on_sample)
    print("STANAG 4609 KLV decoder started", flush=True)
    print("  Raw   : {}".format(topic), flush=True)
    print("  Track : {}".format(_4609_TRACK_TOPIC), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def _4609_main() -> None:
    parser = argparse.ArgumentParser(description="STANAG 4609 KLV decoder — Zenoh raw -> tracks")
    parser.add_argument("--zenoh-raw", action="store_true",
                        help="decode KLV from .../raw/stanag_4609/** (default and only mode)")
    parser.add_argument("--raw-topic", default=os.environ.get("STANAG4609_RAW_TOPIC", ""),
                        help="override the raw KLV subscription key expression")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    _4609_run(args)


# Backward-compatible bare aliases (test_protocol_conformance.py conformance surface).
ST0601_LOCAL_SET_KEY = _4609_ST0601_LOCAL_SET_KEY
parse_local_set = _4609_parse_local_set
decode_st0601 = _4609_decode_st0601
decode_signed = _4609_decode_signed
split_klv_packet = _4609_split_klv_packet
parse_klv_packets = _4609_parse_klv_packets
_encode_ber = _4609_encode_ber


# ============================================================================
# STANAG 5516 — Link 16 / JREAP-C
#
# Receives Link 16 J-series messages encapsulated in JREAP-C UDP packets
# (MIL-STD-3011 / STANAG 5602) and publishes decoded tactical tracks to the
# EFDI Zenoh fabric so tak_layer.py can forward them to ATAK.
#
# Protocol stack:
#     UDP (port 3010)
#       |-- JREAP-C header (4 bytes)
#             |-- J-series words (75 bits each, LSB-padded to 10 bytes)
#                   |-- Message label -> J3.2 / J2.2 / J3.5 / J3.7 ...
#
# Message types decoded:
#     J2.2  PPLI Air        — friendly air unit (own-force position report)
#     J2.5  PPLI Surface    — friendly surface unit
#     J3.2  Air Track       — surveillance air track (cooperative / PSR)
#     J3.5  Surface Track   — surface track
#     J3.7  Land Track      — land track
#
# Position encoding (Binary Angular Measurement — BAM):
#     All positions in Link 16 use signed integer BAM fractions of the full
#     circle. The conversion is: degrees = raw x (360 / 2^bits)
#     (Some fields use 25-bit lat / 26-bit lon within the 75-bit word.)
#
# NOTE: Bit field positions in this file are based on MIL-STD-6016F /
# STANAG 5516 (Edition 5) unclassified summary tables. If your terminal uses
# an earlier edition (MIL-STD-6016C/D/E) verify the field offsets before
# operational use — minor revisions moved some sub-fields.
#
# Zenoh topics published:
#     air/stanag_5516/c2/friendly/aircraft/json/tracks   — J2.2 / J3.2 friend
#     air/stanag_5516/c2/hostile/aircraft/json/tracks    — J3.2 hostile
#     air/stanag_5516/c2/unknown/json/tracks             — J3.2 unknown
#     sea/stanag_5516/c2/friendly/vessel/json/tracks     — J2.5 / J3.5 friend
#     sea/stanag_5516/c2/hostile/vessel/json/tracks      — J3.5 hostile
#     land/stanag_5516/c2/friendly/unit/json/tracks      — J3.7 friend
#     land/stanag_5516/c2/hostile/unit/json/tracks       — J3.7 hostile
#
# Configuration (compose/.env):
#     STANAG5516_PORT=3010           # JREAP-C UDP listen port (default: 3010)
#     STANAG5516_TCP=0               # reserved; TCP requires a gateway framing ICD
# ============================================================================

_5516_TOPIC_ROOT = topic_root()

_5516_JREAP_PORT = int(os.environ.get("STANAG5516_PORT", "3010"))
_5516_WORD_BITS  = 75
_5516_WORD_BYTES = 10     # 75 bits padded to 80 bits (5 unused LSBs per word)

# J-series message labels (J-number x 8 + sub-label encoding)
# Label field: bits 2-7 = J-number (0-63), bits 8-11 = sub-label (0-15)
_5516_LABEL = {
    (2, 2): "J2.2",   # PPLI Air
    (2, 5): "J2.5",   # PPLI Surface
    (3, 2): "J3.2",   # Air Track
    (3, 5): "J3.5",   # Surface Track
    (3, 7): "J3.7",   # Land Track
}

# Force/identity codes -> affiliation slug
_5516_ID_AFF = {
    0b000: "friendly",   # Friend
    0b001: "neutral",    # Neutral
    0b010: "hostile",    # Hostile/Suspect
    0b011: "unknown",    # Unknown
    0b100: "hostile",    # Suspect (treat as hostile)
    0b101: "friendly",   # Assumed Friend
}

# Topic templates per (domain, affiliation)
_5516_TOPIC_MAP = {
    ("air",  "friendly"): "{}/air/stanag_5516/c2/friendly/aircraft".format(_5516_TOPIC_ROOT),
    ("air",  "hostile"):  "{}/air/stanag_5516/c2/hostile/aircraft".format(_5516_TOPIC_ROOT),
    ("air",  "neutral"):  "{}/air/stanag_5516/c2/neutral/aircraft".format(_5516_TOPIC_ROOT),
    ("air",  "unknown"):  "{}/air/stanag_5516/c2/unknown".format(_5516_TOPIC_ROOT),
    ("sea",  "friendly"): "{}/sea/stanag_5516/c2/friendly/vessel".format(_5516_TOPIC_ROOT),
    ("sea",  "hostile"):  "{}/sea/stanag_5516/c2/hostile/vessel".format(_5516_TOPIC_ROOT),
    ("sea",  "neutral"):  "{}/sea/stanag_5516/c2/neutral/vessel".format(_5516_TOPIC_ROOT),
    ("sea",  "unknown"):  "{}/sea/stanag_5516/c2/unknown/vessel".format(_5516_TOPIC_ROOT),
    ("land", "friendly"): "{}/land/stanag_5516/c2/friendly/unit".format(_5516_TOPIC_ROOT),
    ("land", "hostile"):  "{}/land/stanag_5516/c2/hostile/unit".format(_5516_TOPIC_ROOT),
    ("land", "neutral"):  "{}/land/stanag_5516/c2/neutral/unit".format(_5516_TOPIC_ROOT),
    ("land", "unknown"):  "{}/land/stanag_5516/c2/unknown/unit".format(_5516_TOPIC_ROOT),
}


def _5516_netbird_ip() -> str:
    for iface in ("wt0", "netbird0"):
        try:
            import subprocess
            out = subprocess.check_output(["ip", "-4", "addr", "show", iface],
                                          stderr=subprocess.DEVNULL, text=True)
            for tok in out.split():
                if "/" in tok and tok[0].isdigit():
                    return tok.split("/")[0]
        except Exception:
            pass
    return socket.gethostname()


class _5516_BitReader:
    """Extract arbitrary-width fields from a 75-bit Link 16 word.

    Words are transmitted MSB-first, padded to 80 bits (10 bytes) with 5
    unused LSBs. Field offsets are 0-based from the MSB of the 75-bit word.
    """

    def __init__(self, data: bytes):
        # Use only the first 75 bits (top bits of the 10-byte block)
        self._val = int.from_bytes(data[:10], "big") >> 5   # shift off 5 padding bits
        self._bits = 75

    def u(self, offset: int, width: int) -> int:
        """Unsigned field: offset bits from MSB, width bits wide."""
        shift = self._bits - offset - width
        if shift < 0:
            return 0
        return (self._val >> shift) & ((1 << width) - 1)

    def s(self, offset: int, width: int) -> int:
        """Signed (two's complement) field."""
        v = self.u(offset, width)
        if v >= (1 << (width - 1)):
            v -= (1 << width)
        return v

    def bam(self, offset: int, width: int) -> float:
        """Signed BAM field -> decimal degrees. Full circle = 2^width LSBs."""
        return self.s(offset, width) * (360.0 / (1 << width))


def _5516_parse_jreap_header(data: bytes) -> tuple[int, int, bytes]:
    """Parse 4-byte JREAP-C PDU header. Returns (pdu_type, seq_num, payload)."""
    if len(data) < 4:
        raise ValueError("Packet too short for JREAP-C header")
    version  = data[0]
    if version != 0x01:
        raise ValueError("Unsupported JREAP-C version: {}".format(version))
    pdu_type = data[1]   # 0x00=init 0x01=J-series 0x02=keepalive 0x03=term
    seq_num  = struct.unpack(">H", data[2:4])[0]
    return pdu_type, seq_num, data[4:]


def _5516_extract_words(payload: bytes) -> list[bytes]:
    """Split JREAP-C payload into individual 75-bit words (each padded to 10 bytes)."""
    words = []
    off = 0
    while off + _5516_WORD_BYTES <= len(payload):
        words.append(payload[off:off + _5516_WORD_BYTES])
        off += _5516_WORD_BYTES
    return words


def _5516_word_label(w: "_5516_BitReader") -> tuple[int, int]:
    """Extract (J-number, sub-label) from the label field of a word header.

    Per MIL-STD-6016F, the label occupies bits 2-7 (J-number, 6 bits) and
    bits 8-11 (sub-label, 4 bits) of the initial word.
    """
    jnum  = w.u(2, 6)    # bits 2-7
    sub   = w.u(8, 4)    # bits 8-11
    return jnum, sub


# Bit positions below follow MIL-STD-6016F Table A-E-III (Air Track J3.2)
# and Table A-E-I (PPLI J2.2). Unclassified field positions only.
#
# Initial word (word 0): bits 0-74
# Continuation word 1:   bits 75-149
# Continuation word 2:   bits 150-224
#
# For 3-word messages, concatenate the three 75-bit words into a 225-bit
# stream, then read fields by absolute bit offset.

class _5516_MultiWordReader:
    """Read bit fields across multiple 75-bit words (concatenated)."""

    def __init__(self, words: list[bytes]):
        bits = b""
        for w in words:
            # Each 10-byte block: top 75 bits are data, bottom 5 are padding
            val = int.from_bytes(w[:10], "big") >> 5
            bits += val.to_bytes(10, "big")
        # total available bits = len(words) x 75 (stored in top bits of each 10B)
        self._total_bytes = len(words) * 10
        self._val  = int.from_bytes(bits, "big")
        self._bits = len(words) * 80  # stored width (includes per-word padding bytes)
        self._word_count = len(words)

    def _effective_offset(self, bit: int) -> int:
        """Map logical bit offset (across 75-bit words) to storage bit offset."""
        word_idx  = bit // 75
        bit_in_w  = bit % 75
        # After >>5 each block has 5 leading zeros then 75 data bits — skip them
        return word_idx * 80 + bit_in_w + 5

    def u(self, bit: int, width: int) -> int:
        """Unsigned field at logical bit offset across concatenated 75-bit words."""
        eff = self._effective_offset(bit)
        shift = self._bits - eff - width
        if shift < 0 or width <= 0:
            return 0
        return (self._val >> shift) & ((1 << width) - 1)

    def s(self, bit: int, width: int) -> int:
        v = self.u(bit, width)
        if v >= (1 << (width - 1)):
            v -= (1 << width)
        return v

    def bam(self, bit: int, width: int) -> float:
        return self.s(bit, width) * (360.0 / (1 << width))


# J3.2  Air Track  (3 words, 225 bits)
# Ref: MIL-STD-6016F, Table A-E-III / STANAG 5516 Ed.5, Annex E
#
# Word 0 (bits 0-74):
#   0- 1  Word type = 00 (initial)
#   2- 7  Label = 000011 (J-number 3)
#   8-11  Sub-label = 0010 (J3.2)
#  12-13  Link name (network)
#  14-16  Track quality (1-7)
#  17-19  Identity/force: 000=friend 001=neutral 010=hostile 011=unknown
#  20-21  Exercise (00=live 01=exercise 10=sim)
#  22-23  Track number MSBs (2 bits)
#  24-34  Track number (10 bits)  [total 12-bit track number]
#  35-59  Latitude (25-bit signed BAM -> x180/2^24 deg)
#  60-74  Longitude MSBs (15 bits)
#
# Word 1 (bits 75-149):
#  75-85  Longitude LSBs (11 bits, combined with above = 26-bit signed BAM)
#  86-96  Altitude (11-bit, 100 ft per LSB, offset -1000 ft -> alt = raw*100 - 100000)
#  97-108 Speed (12-bit unsigned, 1 kt per LSB)
# 109-119 Heading (11-bit unsigned BAM -> x360/2048)
# 120-149 (additional fields, environment type, etc.)

def _5516_decode_j32(words: list[bytes]) -> dict | None:
    """Decode J3.2 Air Track from a list of 3 word buffers."""
    if len(words) < 3:
        return None
    r = _5516_MultiWordReader(words)

    identity_raw = r.u(17, 3)
    aff          = _5516_ID_AFF.get(identity_raw, "unknown")

    track_num    = (r.u(22, 2) << 10) | r.u(24, 10)   # 12-bit track number

    lat_raw      = r.s(35, 25)
    lat_deg      = lat_raw * (180.0 / (1 << 24))

    lon_msb      = r.u(60, 15)
    lon_lsb      = r.u(75, 11)
    lon_raw_u    = (lon_msb << 11) | lon_lsb           # 26-bit unsigned
    lon_raw_s    = lon_raw_u - (1 << 26) if lon_raw_u >= (1 << 25) else lon_raw_u
    lon_deg      = lon_raw_s * (360.0 / (1 << 26))

    alt_raw      = r.u(86, 11)
    alt_ft       = alt_raw * 100 - 100_000             # offset encoding

    spd_raw      = r.u(97, 12)
    spd_ms       = spd_raw * 0.514444                  # kt -> m/s

    hdg_raw      = r.u(109, 11)
    hdg_deg      = hdg_raw * (360.0 / 2048.0)

    if abs(lat_deg) > 90 or abs(lon_deg) > 180:
        return None

    return {
        "_ts":         time.time(),
        "_src":        "Link 16 J3.2",
        "track_num":   track_num,
        "affiliation": aff,
        "lat_deg":     round(lat_deg, 6),
        "lon_deg":     round(lon_deg, 6),
        "alt_baro_ft": round(alt_ft),
        "speed_ms":    round(spd_ms, 1),
        "heading_deg": round(hdg_deg, 1),
    }


# J2.2  PPLI Air  (3 words, 225 bits)
# Ref: MIL-STD-6016F, Table A-E-I
# PPLI = Precise Participant Location and Identification (self-report)
# Always friendly — emitting own-force units identify themselves.
#
# Word 0:
#   0- 1  Word type = 00
#   2- 7  Label = 000010 (J-number 2)
#   8-11  Sub-label = 0010 (J2.2)
#  12-23  Source Track Number (STN, 12-bit own-force ID)
#  24-48  Latitude (25-bit signed BAM)
#  49-74  Longitude (26-bit signed BAM)
#
# Word 1:
#  75-85  First continuation-word field
#  86-95  Altitude (10-bit, 100 ft/LSB, offset)
#  96-107 Speed (12-bit unsigned, 1 kt/LSB)
# 108-118 Heading (11-bit unsigned BAM)
# 119-131 STN callsign extension / activity fields

def _5516_decode_j22(words: list[bytes]) -> dict | None:
    """Decode J2.2 PPLI Air from a list of 3 word buffers."""
    if len(words) < 3:
        return None
    r = _5516_MultiWordReader(words)

    stn          = r.u(12, 12)                         # source track number (unit ID)

    lat_raw      = r.s(24, 25)
    lat_deg      = lat_raw * (180.0 / (1 << 24))

    lon_msb      = r.u(49, 26)
    lon_raw_u    = lon_msb                             # complete 26-bit BAM field
    lon_raw_s    = lon_raw_u - (1 << 25) if lon_raw_u >= (1 << 25) else lon_raw_u
    lon_deg      = lon_raw_s * (360.0 / (1 << 26))

    alt_raw      = r.u(86, 10)
    alt_ft       = alt_raw * 100 - 100_000

    spd_raw      = r.u(96, 12)
    spd_ms       = spd_raw * 0.514444

    hdg_raw      = r.u(108, 11)
    hdg_deg      = hdg_raw * (360.0 / 2048.0)

    if abs(lat_deg) > 90 or abs(lon_deg) > 180:
        return None

    return {
        "_ts":         time.time(),
        "_src":        "Link 16 J2.2",
        "track_num":   stn,
        "affiliation": "friendly",   # PPLI = always own-force friendly
        "lat_deg":     round(lat_deg, 6),
        "lon_deg":     round(lon_deg, 6),
        "alt_baro_ft": round(alt_ft),
        "speed_ms":    round(spd_ms, 1),
        "heading_deg": round(hdg_deg, 1),
    }


# J3.5  Surface Track  (same layout as J3.2 except altitude = MSL, no speed)
# J3.7  Land Track     (same layout, altitude = terrain clearance)
# Both are 3-word messages with identical position encoding as J3.2.

def _5516_decode_j35(words: list[bytes]) -> dict | None:
    """Decode J3.5 Surface Track (same layout as J3.2, sea domain)."""
    track = _5516_decode_j32(words)
    if track:
        track["_src"] = "Link 16 J3.5"
        track["domain"] = "sea"
    return track


def _5516_decode_j25(words: list[bytes]) -> dict | None:
    """Decode J2.5 PPLI Surface (same layout as J2.2, sea domain)."""
    track = _5516_decode_j22(words)
    if track:
        track["_src"] = "Link 16 J2.5"
        track["domain"] = "sea"
    return track


def _5516_decode_j37(words: list[bytes]) -> dict | None:
    """Decode J3.7 Land Track."""
    track = _5516_decode_j32(words)
    if track:
        track["_src"] = "Link 16 J3.7"
        track["domain"] = "land"
    return track


# How many continuation words each message type needs (after the initial word)
_5516_MSG_WORD_COUNT = {
    "J2.2": 3,
    "J2.5": 3,
    "J3.2": 3,
    "J3.5": 3,
    "J3.7": 3,
}

_5516_MSG_DECODER = {
    "J2.2": _5516_decode_j22,
    "J2.5": _5516_decode_j25,
    "J3.2": _5516_decode_j32,
    "J3.5": _5516_decode_j35,
    "J3.7": _5516_decode_j37,
}

_5516_MSG_DOMAIN = {
    "J2.2": "air",
    "J2.5": "sea",
    "J3.2": "air",
    "J3.5": "sea",
    "J3.7": "land",
}


def _5516_topic_for(track: dict, msg_type: str) -> str:
    domain = track.get("domain") or _5516_MSG_DOMAIN.get(msg_type, "land")
    aff    = track.get("affiliation", "unknown")
    return _5516_TOPIC_MAP.get((domain, aff),
                           "{}/land/stanag_5516/c2/unknown/unit".format(_5516_TOPIC_ROOT))


def _5516_process_packet(data: bytes, pub: "zenoh.Session", verbose: bool):
    """Parse one JREAP-C UDP packet and publish any decoded tracks."""
    try:
        pdu_type, seq, payload = _5516_parse_jreap_header(data)
    except ValueError:
        return

    if pdu_type != 0x01:  # not J-series data
        return

    words = _5516_extract_words(payload)
    if not words:
        return

    i = 0
    while i < len(words):
        r = _5516_BitReader(words[i])
        jnum, sub = _5516_word_label(r)
        msg_type  = _5516_LABEL.get((jnum, sub))

        if msg_type is None:
            i += 1
            continue

        needed = _5516_MSG_WORD_COUNT.get(msg_type, 1)
        if i + needed > len(words):
            break   # not enough words remaining

        decoder = _5516_MSG_DECODER.get(msg_type)
        if decoder:
            track = decoder(words[i:i + needed])
            if track:
                topic = _5516_topic_for(track, msg_type)
                publish_dual(pub, topic, track, Stanag5516Track)
                if verbose:
                    print("PUB stanag_5516 {} aff={} lat={} lon={} alt={}ft".format(
                        msg_type,
                        track.get("affiliation", "?"),
                        round(track.get("lat_deg", 0), 4),
                        round(track.get("lon_deg", 0), 4),
                        track.get("alt_baro_ft", "---"),
                    ), flush=True)

        i += needed


def _5516_run_udp(port: int, session: "zenoh.Session", verbose: bool):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    ip = _5516_netbird_ip()
    print("Link 16 JREAP-C UDP listening on 0.0.0.0:{}".format(port), flush=True)
    print("Tell JREAP gateway: send to {}:{}".format(ip, port), flush=True)
    while True:
        data, _ = sock.recvfrom(65535)
        _5516_process_packet(data, session, verbose)


def _5516_run_zenoh_raw(raw_topic: str, verbose: bool):
    session = open_session()

    def on_sample(sample):
        try:
            data = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
            _5516_process_packet(data, session, verbose)
        except Exception as exc:
            print("STANAG5516 raw decode error:", exc, flush=True)

    subscriber = subscribe(session, raw_topic, on_sample)
    print("STANAG5516 Zenoh raw translator subscribed to {}".format(raw_topic), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def _5516_main():
    ap = argparse.ArgumentParser(description="Link 16 JREAP-C -> Zenoh bridge")
    ap.add_argument("--port", type=int, default=_5516_JREAP_PORT,
                    help="JREAP-C listen port (default: {})".format(_5516_JREAP_PORT))
    ap.add_argument("--tcp", action="store_true",
                    help="TCP server mode instead of UDP")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--zenoh-raw", action="store_true",
                    help="decode bytes from .../raw/stanag_5516/** instead of opening UDP")
    ap.add_argument("--raw-topic", default=os.environ.get("STANAG5516_RAW_TOPIC", ""))
    args = ap.parse_args()

    if args.tcp:
        ap.error(
            "--tcp is disabled: this bridge has no verified JREAP-C stream "
            "framing/length ICD. Use UDP or implement the gateway's documented framing."
        )

    if args.zenoh_raw:
        _5516_run_zenoh_raw(args.raw_topic or _5516_TOPIC_ROOT + "/raw/stanag_5516/**", args.verbose)
        return

    session = open_session()
    print("Link 16 bridge started", flush=True)
    print("  Topics:", flush=True)
    for (dom, aff), topic in _5516_TOPIC_MAP.items():
        print("    {} {} -> {}".format(dom, aff, topic.split(_5516_TOPIC_ROOT + "/")[1]), flush=True)

    try:
        _5516_run_udp(args.port, session, args.verbose)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


# ============================================================================
# Dispatch
# ============================================================================

def _pop_proto_argument() -> int | None:
    for index, argument in enumerate(sys.argv[1:], 1):
        if argument == "--proto":
            if index + 1 >= len(sys.argv):
                raise SystemExit("--proto requires one of: 4586, 4607, 4609, 5516")
            value = sys.argv[index + 1]
            del sys.argv[index:index + 2]
            break
        if argument.startswith("--proto="):
            value = argument.split("=", 1)[1]
            del sys.argv[index]
            break
    else:
        return None
    try:
        proto = int(value)
    except ValueError as exc:
        raise SystemExit("invalid STANAG protocol: {}".format(value)) from exc
    if proto not in _PROTO_MAINS:
        raise SystemExit("unsupported STANAG protocol: {}".format(proto))
    return proto


_PROTO_MAINS = {
    4586: _4586_main,
    4607: _4607_main,
    4609: _4609_main,
    5516: _5516_main,
}


def main() -> None:
    proto = _pop_proto_argument()
    if proto is None:
        raise SystemExit("Set --proto {4586,4607,4609,5516}")
    _PROTO_MAINS[proto]()


if __name__ == "__main__":
    main()
