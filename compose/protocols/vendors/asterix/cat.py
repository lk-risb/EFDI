#!/usr/bin/env python3
"""EUROCONTROL ASTERIX protocol implementation: CAT-001/002/004/007/008/009/
010/011/015/016/017/018/019/020/021/023/025/032/034/048/062/063/065/150/205/
240/247.

Sections below are ordered by ascending CAT number for easy lookup — search
for "# CAT-NNN" to jump to a category. The module keeps each edition-scoped
decoder isolated while exposing one source entrypoint. Run without
``--category`` to launch the configured CAT processes, or pass
``--category NN`` for one translator process.

Every category calls into protocols.gateway for session/publish/subscribe —
this file has no direct zenoh import at all; gateway.py is the only module
that does.
"""

from __future__ import annotations

import os
import sys

from namespace_prefix import topic_root
from protocols.gateway import (
    open_session,
    publish_dual,
    publish_collection,
    publish_native,
    run_zenoh_raw,
    run_inbound,
    subscribe,
    ZError,
)
from protocols.track_views import native_topic, semantic_topic, asterix_data_block
from protocols.data_stats import record_in
from protocols.process_bundle import run_bundle
from protocols.proto.cat_pb2 import (
    AsterixCat1Track,
    AsterixCat2Status,
    AsterixCat4Alert,
    AsterixCat7Track,
    AsterixCat8Weather,
    AsterixCat9Weather,
    AsterixCat10SensorStatus,
    AsterixCat10Track,
    AsterixCat11Track,
    AsterixCat15Track,
    AsterixCat16Status,
    AsterixCat17Track,
    AsterixCat18Track,
    AsterixCat19Status,
    AsterixCat20Track,
    AsterixCat21Track,
    AsterixCat23Status,
    AsterixCat25Status,
    AsterixCat32Status,
    AsterixCat34Status,
    AsterixCat48Track,
    Cat62Track,
    AsterixCat63Status,
    AsterixCat65Status,
    AsterixCat150Status,
    AsterixCat205Track,
    AsterixCat240Status,
    AsterixCat247Status,
)


def _asterix_source(track: dict) -> str:
    """The reporting sensor's identity, for the `source` topic segment.

    Every ASTERIX category carries SAC/SIC (System Area Code / System
    Identification Code) in I0xx/010, so the sensor names itself on the wire.
    Two radars feeding one router therefore stay separable by topic; before
    this they both published under the literal `asterix` and collided.

    Topic constants below are templates holding `{source}` — the segment can
    only be filled once a record has been decoded.
    """
    return "{:03d}-{:03d}".format(track.get("sac", 0) or 0, track.get("sic", 0) or 0)


# ==========================================================================
# CAT-001 — Transmission of Monoradar Data Target Reports, Ed.1.4
#
# Legacy monoradar plot/track category, superseded for most modern uses by
# CAT-048 (SSR/PSR/Mode S) but still emitted by some older sensors. Unlike
# every other category here, one CAT-001 record can mean two different
# things: I001/020's TYP bit says whether this is a PLOT or a TRACK report,
# and the two variants use DIFFERENT FRN-to-item mappings from FRN3 onward
# (FRN1=010 and FRN2=020 are shared). _cat1__PLOT_ITEMS / _cat1__TRACK_ITEMS
# below are the two UAP tables; which one applies is only known after
# decoding I001/020, so FRN0/FRN1 are handled explicitly before the loop
# switches to the selected table.
# ==========================================================================

import argparse

import json

import math

import os

import socket

import struct

import threading

import time

from namespace_prefix import topic_root
from protocols.track_views import (publish_dual, publish_native, native_topic,
                                      semantic_topic, asterix_data_block)
from protocols.data_stats import record_in

_cat1_TOPIC_ROOT = topic_root()

_cat1_TOPIC_TRACK = _cat1_TOPIC_ROOT + "/air/{source}/radar/unknown/aircraft"
_cat1_RAW_INPUT_TOPIC = "{}/raw/asterix/cat1".format(_cat1_TOPIC_ROOT)
_cat1_CAT_001 = 1

_cat1__SSR_PSR = {0: "no_detection", 1: "sole_primary", 2: "sole_secondary", 3: "combined"}
_cat1__EMERGENCY = {0: "none", 1: "unlawful_interference", 2: "radio_failure", 3: "emergency"}
_cat1__WARNING_CODES = {
    0: "none", 1: "garbled_reply", 2: "reflection", 3: "sidelobe_reply",
    4: "split_plot", 5: "second_time_around_reply", 6: "angels",
    7: "terrestrial_vehicles", 64: "possible_wrong_mode3a",
    65: "possible_wrong_altitude", 66: "possible_phantom_mssr_plot",
    80: "fixed_psr_plot", 81: "slow_psr_plot", 82: "low_quality_psr_plot",
}
# Pulse order for I001/060 and I001/080's 12-bit confidence groups.
_cat1__CONF_PULSES_ABCD = ("qa4", "qa2", "qa1", "qb4", "qb2", "qb1",
                            "qc4", "qc2", "qc1", "qd4", "qd2", "qd1")
# I001/100's byte-3 confidence bits use a different pulse order.
_cat1__CONF_PULSES_MODEC = ("qc1", "qa1", "qc2", "qa2", "qc4", "qa4",
                             "qb1", "qd1", "qb2", "qd2", "qb4", "qd4")


def _cat1__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat1__netbird_ip() -> str:
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


def _cat1__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat1_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat1__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat1__recv_exact(sock, length - 3)
        record_in("cat1", len(data))
        yield cat, data


def _cat1_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat1", len(record))
            yield cat, record
            offset += length


def _cat1_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat1__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat1__decode_020(data: bytes, pos: int, msg: dict) -> int:
    """I001/020 Target Report Descriptor — 1-3 byte FX chain."""
    if pos + 1 > len(data):
        return pos
    b1 = data[pos]; pos += 1
    msg["is_track"]  = bool(b1 & 0x80)
    msg["simulated"] = bool(b1 & 0x40)
    msg["ssr_psr"]   = _cat1__SSR_PSR.get((b1 >> 4) & 0x03, "undefined")
    msg["antenna"]   = 2 if (b1 & 0x08) else 1
    msg["spi"]       = bool(b1 & 0x04)
    msg["fixed_transponder"] = bool(b1 & 0x02)
    if not (b1 & 0x01):
        return pos

    if pos + 1 > len(data):
        return pos
    b2 = data[pos]; pos += 1
    msg["test_target"] = bool(b2 & 0x80)
    msg["emergency"]    = _cat1__EMERGENCY.get((b2 >> 5) & 0x03, "undefined")
    msg["military_emergency"] = bool(b2 & 0x08)
    msg["military_id"]        = bool(b2 & 0x04)
    if not (b2 & 0x01):
        return pos

    if pos + 1 > len(data):
        return pos
    pos += 1   # byte 3 is all spare
    return pos


def _cat1__decode_polar(data: bytes, pos: int) -> tuple[float, float, int] | None:
    if pos + 4 > len(data):
        return None
    rho   = _cat1__u16(data[pos:pos + 2]) / 128.0
    theta = _cat1__u16(data[pos + 2:pos + 4]) * (360.0 / 65536.0)
    return round(rho, 4), round(theta, 4), pos + 4


def _cat1__u16(raw: bytes) -> int:
    return struct.unpack(">H", raw)[0]


def _cat1__s16(raw: bytes) -> int:
    return struct.unpack(">h", raw)[0]


def _cat1__decode_mode_code(data: bytes, pos: int) -> tuple[int, bool, bool, bool, int] | None:
    """Shared V(1)/G(1)/L(1)/spare(1) + 12-bit octal-code layout (I001/050,
    I001/070): byte1's low nibble is the code's top 4 bits, byte2 is the
    remaining 8 bits (4 + 8 = 12)."""
    if pos + 2 > len(data):
        return None
    b1, b2 = data[pos], data[pos + 1]
    v = bool(b1 & 0x80); g = bool(b1 & 0x40); l = bool(b1 & 0x20)
    code12 = ((b1 & 0x0F) << 8) | b2
    return v, g, l, code12, pos + 2


def _cat1__decode_confidence12(data: bytes, pos: int, pulse_order: tuple) -> tuple[dict, int] | None:
    if pos + 2 > len(data):
        return None
    raw = _cat1__u16(data[pos:pos + 2]) & 0x0FFF
    quality = {}
    for index, name in enumerate(pulse_order):
        shift = len(pulse_order) - 1 - index
        quality[name] = bool(raw & (1 << shift))
    return quality, pos + 2


def _cat1__decode_repetitive(data: bytes, pos: int) -> tuple[list, int]:
    """Repetitive item with FX-as-last-bit-of-each-byte (I001/030, I001/130, I001/210)."""
    items = []
    while pos < len(data):
        b = data[pos]; pos += 1
        items.append(b >> 1)
        if not (b & 0x01):
            break
    return items, pos


# FRN-index (0-based, counting from FRN3 i.e. index 0 = FRN3) -> item tag.
# "raw030" / "raw130" / "raw210" are repetitive; everything else is a fixed
# or FX-chained field decoded inline below.
_cat1__PLOT_ITEMS = ("040", "070", "090", "raw130", "141", "050", "120",
                      "131", "080", "100", "060", "raw030", "150")
_cat1__TRACK_ITEMS = ("161", "040", "042", "200", "070", "090", "141",
                       "raw130", "131", "120", "170", "raw210", "050",
                       "080", "100", "060", "raw030")
# SP and the undocumented "rfs" trailer are length-prefixed and always last
# regardless of variant — handled by the generic RE/SP/spare fallthrough.


def _cat1__polar_to_wgs84(radar_lat: float, radar_lon: float,
                            range_nm: float, azimuth_deg: float):
    """Haversine forward: slant-polar radar plot -> WGS-84 lat/lon."""
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


def _cat1__cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_m: float, y_m: float):
    """Local X=east/Y=north metres to WGS-84 for short-range sites."""
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon


def _cat1_decode_cat001(data: bytes, site_lat=None, site_lon=None) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat1_parse_fspec(data, 0)
    msg = {}

    if len(fspec) < 1 or not fspec[0]:
        return None
    if pos + 2 > len(data):
        return None
    msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2

    if len(fspec) < 2 or not fspec[1]:
        return msg if msg else None
    pos = _cat1__decode_020(data, pos, msg)

    items = _cat1__TRACK_ITEMS if msg.get("is_track") else _cat1__PLOT_ITEMS

    for offset, present in enumerate(fspec[2:]):
        if not present:
            continue
        if offset >= len(items):
            # SP / rfs / spare tail — length-prefixed, safe to skip generically.
            pos = _cat1__skip_len_field(data, pos)
            continue
        tag = items[offset]
        if tag == "161":                # Track/Plot Number
            if pos + 2 > len(data): break
            msg["track_num"] = _cat1__u16(data[pos:pos + 2]); pos += 2
        elif tag == "040":               # Polar position
            result = _cat1__decode_polar(data, pos)
            if result is None: break
            msg["rho_nm"], msg["theta_deg"], pos = result
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat1__polar_to_wgs84(site_lat, site_lon, msg["rho_nm"], msg["theta_deg"])
                msg.setdefault("lat_deg", round(lat, 7)); msg.setdefault("lon_deg", round(lon, 7))
        elif tag == "042":               # Cartesian position
            if pos + 4 > len(data): break
            x_nm = round(_cat1__s16(data[pos:pos + 2]) / 64.0, 4)
            y_nm = round(_cat1__s16(data[pos + 2:pos + 4]) / 64.0, 4)
            msg["x_nm"] = x_nm; msg["y_nm"] = y_nm
            pos += 4
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat1__cartesian_to_wgs84(site_lat, site_lon, x_nm * 1852.0, y_nm * 1852.0)
                msg.setdefault("lat_deg", round(lat, 7)); msg.setdefault("lon_deg", round(lon, 7))
        elif tag == "200":               # Calculated track velocity (polar)
            if pos + 4 > len(data): break
            gsp_raw = _cat1__u16(data[pos:pos + 2])
            msg["speed_ms"] = round(gsp_raw / 16384.0 * 1852.0, 3)
            msg["heading_deg"] = round(_cat1__u16(data[pos + 2:pos + 4]) * (360.0 / 65536.0), 4)
            pos += 4
        elif tag in ("070", "050"):      # Mode-3/A or Mode-2 code (octal)
            result = _cat1__decode_mode_code(data, pos)
            if result is None: break
            v, g, l, code12, pos = result
            prefix = "mode3a" if tag == "070" else "mode2"
            msg["{}_squawk".format(prefix)] = "{:04o}".format(code12)
            msg["{}_validated".format(prefix)] = not v
            msg["{}_garbled".format(prefix)] = g
            msg["{}_smoothed".format(prefix)] = l
        elif tag == "090":                # Mode-C binary height
            if pos + 2 > len(data): break
            b1, b2 = data[pos], data[pos + 1]
            v = bool(b1 & 0x80); g = bool(b1 & 0x40)
            raw14 = ((b1 & 0x3F) << 8) | b2
            if raw14 & 0x2000:            # sign-extend 14-bit two's complement
                raw14 -= 1 << 14
            msg["alt_baro_ft"] = round(raw14 * 0.25 * 100.0, 1)
            msg["altitude_validated"] = not v
            msg["altitude_garbled"] = g
            pos += 2
        elif tag == "141":                # Truncated time of day
            if pos + 2 > len(data): break
            msg["tod_trunc_s"] = round(_cat1__u16(data[pos:pos + 2]) / 128.0, 4)
            pos += 2
        elif tag == "120":                # Doppler speed
            if pos + 1 > len(data): break
            raw = struct.unpack("b", bytes((data[pos],)))[0]
            msg["doppler_speed_ms"] = round(raw / 256.0 * 1852.0, 3)
            pos += 1
        elif tag == "131":                # Received power
            if pos + 1 > len(data): break
            msg["received_power_dbm"] = struct.unpack("b", bytes((data[pos],)))[0]
            pos += 1
        elif tag == "080":                # Mode-3/A confidence
            result = _cat1__decode_confidence12(data, pos, _cat1__CONF_PULSES_ABCD)
            if result is None: break
            msg["mode3a_confidence"], pos = result
        elif tag == "060":                # Mode-2 confidence
            result = _cat1__decode_confidence12(data, pos, _cat1__CONF_PULSES_ABCD)
            if result is None: break
            msg["mode2_confidence"], pos = result
        elif tag == "100":                # Mode-C code + confidence
            if pos + 3 > len(data): break
            b1, b2 = data[pos], data[pos + 1]
            v = bool(b1 & 0x80); g = bool(b1 & 0x40)
            modec_gray = ((b1 & 0x0F) << 8) | b2
            msg["mode_c_gray_code"] = modec_gray
            msg["mode_c_validated"] = not v
            msg["mode_c_garbled"] = g
            pos += 2
            result = _cat1__decode_confidence12(data, pos, _cat1__CONF_PULSES_MODEC)
            if result is None: break
            msg["mode_c_confidence"], pos = result
        elif tag == "170":                 # Track status
            if pos + 1 > len(data): break
            b1 = data[pos]; pos += 1
            msg["confirmed"]  = not bool(b1 & 0x80)
            msg["radar_type"] = "ssr_combined" if (b1 & 0x40) else "primary"
            msg["maneuvering"] = bool(b1 & 0x20)
            msg["doubtful_association"] = bool(b1 & 0x10)
            msg["rdp_chain"] = 2 if (b1 & 0x08) else 1
            if b1 & 0x01:
                if pos + 1 > len(data): break
                b2 = data[pos]; pos += 1
                msg["ghost_track"] = bool(b2 & 0x80)
                if b2 & 0x01:
                    if pos + 1 > len(data): break
                    pos += 1   # byte 3 all spare
        elif tag == "150":                 # X-pulse presence
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["x_pulse_mode3a"] = bool(b & 0x80)
            msg["x_pulse_modec"]  = bool(b & 0x20)
            msg["x_pulse_mode2"]  = bool(b & 0x04)
        elif tag == "raw030":              # Warning/error conditions (repetitive)
            codes, pos = _cat1__decode_repetitive(data, pos)
            msg["warnings"] = [_cat1__WARNING_CODES.get(c, "code_{}".format(c)) for c in codes]
        elif tag == "raw130":              # Radar plot characteristics (repetitive, app-defined)
            values, pos = _cat1__decode_repetitive(data, pos)
            if values: msg["plot_characteristics_raw"] = values
        elif tag == "raw210":              # Track quality (repetitive, app-defined)
            values, pos = _cat1__decode_repetitive(data, pos)
            if values: msg["track_quality_raw"] = values
        else:
            break
    return msg if msg else None


def _cat1__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat1_CAT_001:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat1__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat1__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat1__raw_frame_payload(bytes(sample.payload), _cat1_CAT_001)
        except ValueError as exc:
            print("CAT-1 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-1 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat1__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat1__process_stream(_cat1_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-1 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-1 TCP disconnected: {}".format(addr), flush=True)


def _cat1__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat1__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-1 Ed.1.4 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-1 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat1__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-1 Ed.1.4 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat1__process_stream(_cat1_iter_frames_udp(sock), handler, verbose)


def _cat1__make_handler(session, verbose_default: bool, site_lat=None, site_lon=None):
    def _h(data: bytes, verbose: bool):
        msg = _cat1_decode_cat001(data, site_lat=site_lat, site_lon=site_lon)
        if msg is None:
            return
        if "lat_deg" not in msg or "lon_deg" not in msg:
            if verbose:
                print("CAT-1 dropped (no position): CAT-001 carries only radar-local "
                      "polar (I001/040) or cartesian (I001/042) coordinates — set "
                      "CAT1_RADAR_LAT/LON to georeference them", flush=True)
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-1 Ed.1.4"
        topic = _cat1_TOPIC_TRACK.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat1Track)
        if verbose:
            print("PUB CAT-1 {} track={} type={}".format(
                topic, msg.get("track_num"), "track" if msg.get("is_track") else "plot"), flush=True)
    return _h


def _cat1_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-001 Ed.1.4 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT1_PORT", "50001") or 50001))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT1_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT1_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT1_INPUT_TOPIC", _cat1_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat1__env_float("CAT1_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat1__env_float("CAT1_RADAR_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT1_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat1__make_handler(session, args.verbose,
                                   site_lat=args.site_lat or None, site_lon=args.site_lon or None)
    try:
        if args.zenoh_raw: _cat1__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat1__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-002 — Transmission of Monoradar Service Messages, Ed.1.2
#
# North-marker / sector-crossing / status messages for the same monoradar
# that CAT-001 reports plots/tracks from — the "system status" relationship
# CAT-034 has to CAT-048. Not a track: no lat/lon, published as a sensor
# status record like CAT-019/023/063.
# ==========================================================================

_cat2_TOPIC_ROOT = topic_root()

_cat2_TOPIC_SENSOR    = _cat2_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat2_RAW_INPUT_TOPIC = "{}/raw/asterix/cat2".format(_cat2_TOPIC_ROOT)
_cat2_CAT_002 = 2

_cat2__MSG_TYPES = {
    1: "north_marker", 2: "sector_crossing", 3: "south_marker",
    8: "blind_zone_filtering_start", 9: "blind_zone_filtering_stop",
}
_cat2__PLOT_COUNT_IDENT = {1: "sole_primary", 2: "sole_ssr", 3: "combined"}


def _cat2__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat2__netbird_ip() -> str:
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


def _cat2__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat2_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat2__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat2__recv_exact(sock, length - 3)
        record_in("cat2", len(data))
        yield cat, data


def _cat2_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat2", len(record))
            yield cat, record
            offset += length


def _cat2_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat2__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat2__decode_fx_repetitive(data: bytes, pos: int) -> tuple[list, int]:
    """FX-chained repetitive: 7 data bits + 1 FX bit per byte (I002/050, 060, 080)."""
    items = []
    while pos < len(data):
        b = data[pos]; pos += 1
        items.append(b >> 1)
        if not (b & 0x01):
            break
    return items, pos


def _cat2_decode_cat002(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat2_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I002/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I002/000 Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _cat2__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I002/020 Sector Number
            if pos + 1 > len(data): break
            msg["sector_deg"] = round(data[pos] * (360.0 / 256.0), 4); pos += 1
        elif frn == 3:                  # I002/030 Time of Day
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 4:                  # I002/041 Antenna Rotation Speed
            if pos + 2 > len(data): break
            msg["antenna_rotation_s"] = round(struct.unpack(">H", data[pos:pos + 2])[0] / 128.0, 4)
            pos += 2
        elif frn == 5:                  # I002/050 Station Configuration Status (FX-repetitive)
            values, pos = _cat2__decode_fx_repetitive(data, pos)
            if values: msg["station_configuration_raw"] = values
        elif frn == 6:                  # I002/060 Station Processing Mode (FX-repetitive)
            values, pos = _cat2__decode_fx_repetitive(data, pos)
            if values: msg["station_processing_mode_raw"] = values
        elif frn == 7:                  # I002/070 Plot Count Values (REP-count + 2B entries)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            counts = []
            for _ in range(rep):
                if pos + 2 > len(data): break
                w = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
                counts.append({
                    "antenna": 2 if (w & 0x8000) else 1,
                    "ident": _cat2__PLOT_COUNT_IDENT.get((w >> 10) & 0x1F, "undefined"),
                    "count": w & 0x03FF,
                })
            if counts: msg["plot_counts"] = counts
        elif frn == 8:                  # I002/100 Dynamic Window Type 1
            if pos + 8 > len(data): break
            msg["window_rho_start_nm"] = round(struct.unpack(">H", data[pos:pos + 2])[0] / 128.0, 4)
            msg["window_rho_end_nm"]   = round(struct.unpack(">H", data[pos + 2:pos + 4])[0] / 128.0, 4)
            msg["window_theta_start_deg"] = round(struct.unpack(">H", data[pos + 4:pos + 6])[0] * (360.0 / 65536.0), 4)
            msg["window_theta_end_deg"]   = round(struct.unpack(">H", data[pos + 6:pos + 8])[0] * (360.0 / 65536.0), 4)
            pos += 8
        elif frn == 9:                  # I002/090 Collimation Error
            if pos + 2 > len(data): break
            msg["range_error_nm"] = round(struct.unpack("b", bytes((data[pos],)))[0] / 128.0, 4)
            msg["azimuth_error_deg"] = round(struct.unpack("b", bytes((data[pos + 1],)))[0] * (360.0 / 16384.0), 4)
            pos += 2
        elif frn == 10:                 # I002/080 Warning/Error Conditions (FX-repetitive)
            values, pos = _cat2__decode_fx_repetitive(data, pos)
            if values: msg["warnings_raw"] = values
        elif frn in (11, 12, 13):       # spare / SP / rfs
            pos = _cat2__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat2__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat2_CAT_002:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat2__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat2__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat2__raw_frame_payload(bytes(sample.payload), _cat2_CAT_002)
        except ValueError as exc:
            print("CAT-2 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-2 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat2__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat2__process_stream(_cat2_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-2 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-2 TCP disconnected: {}".format(addr), flush=True)


def _cat2__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat2__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-2 Ed.1.2 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-2 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat2__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-2 Ed.1.2 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat2__process_stream(_cat2_iter_frames_udp(sock), handler, verbose)


def _cat2__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat2_decode_cat002(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-2 Ed.1.2"
        topic = _cat2_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat2Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-2 {} msg_type={}".format(topic, msg.get("msg_type")), flush=True)
    return _h


def _cat2_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-002 Ed.1.2 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT2_PORT", "50002") or 50002))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT2_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT2_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT2_INPUT_TOPIC", _cat2_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT2_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat2__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat2__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat2__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-004 — Safety Net Messages, Ed.1.13
#
# STCA/MSAW/APW/RIMCA/... conflict-alert messages. The most structurally
# complex category implemented here: five compound items, one of which
# (I004/120's CN subfield) is itself FX-chained, and one (CC's CPC field)
# whose 3-bit value's MEANING depends on both TID and the outer message
# type. Every bit is decoded and preserved (nothing silently discarded);
# where the public spec's per-message-type interpretation tables were not
# fully enumerable with confidence (CN's octets beyond the first, CPC's
# value), the raw bits are kept as-is rather than guessing a label that
# could be wrong for a conflict-alert message.
# ==========================================================================

_cat4_TOPIC_ROOT = topic_root()

_cat4_TOPIC_ALERT     = _cat4_TOPIC_ROOT + "/air/{source}/radar/unknown/alert"
_cat4_RAW_INPUT_TOPIC = "{}/raw/asterix/cat4".format(_cat4_TOPIC_ROOT)
_cat4_CAT_004 = 4

_cat4__MSG_TYPES = {1: "alive_message", 17: "end_of_conflict"}
_cat4__AREA_STATUS = {0: "inactive", 1: "active", 2: "pre_active"}
_cat4__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
_cat4__GATOAT = {0: "unknown", 1: "gat", 2: "oat", 3: "not_applicable"}
_cat4__FR1FR2 = {0: "ifr", 1: "vfr", 2: "not_applicable", 3: "cvfr"}
_cat4__RVSM = {0: "unknown", 1: "approved", 2: "exempt", 3: "not_approved"}
_cat4__CDM = {0: "maintaining", 1: "climbing", 2: "descending", 3: "invalid"}


def _cat4__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat4__netbird_ip() -> str:
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


def _cat4__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat4_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat4__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat4__recv_exact(sock, length - 3)
        record_in("cat4", len(data))
        yield cat, data


def _cat4_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat4", len(record))
            yield cat, record
            offset += length


def _cat4_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat4__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat4__s16(raw: bytes) -> int:
    return struct.unpack(">h", raw)[0]


def _cat4__u16(raw: bytes) -> int:
    return struct.unpack(">H", raw)[0]


def _cat4__s24(raw: bytes) -> int:
    v = int.from_bytes(raw, "big")
    return v - (1 << 24) if v & (1 << 23) else v


def _cat4__s32(raw: bytes) -> int:
    return struct.unpack(">i", raw)[0]


def _cat4__decode_callsign(raw: bytes) -> str:
    """8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat4__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()


def _cat4__decode_sdps_list(data: bytes, pos: int) -> tuple[list, int]:
    """REP(1 byte) + N x SAC/SIC (I004/015, I004/110 share this shape)."""
    if pos >= len(data):
        return [], pos
    rep = data[pos]; pos += 1
    items = []
    for _ in range(rep):
        if pos + 2 > len(data):
            break
        items.append({"sac": data[pos], "sic": data[pos + 1]})
        pos += 2
    return items, pos


def _cat4__decode_060(data: bytes, pos: int) -> tuple[dict, int]:
    """I004/060 Safety Net Function and System Status — FX-chained.

    Octets 1-3 have confidently-named bits per the public spec; further
    octets exist (sub-function detail bits) but the spec text was not
    precise enough about exact bit-to-name mapping to trust a label —
    those bytes are kept raw rather than guessed.
    """
    status = {}
    if pos + 1 > len(data):
        return status, pos
    b1 = data[pos]; pos += 1
    status["mrva"]  = bool(b1 & 0x80)
    status["ramld"] = bool(b1 & 0x40)
    status["ramhd"] = bool(b1 & 0x20)
    status["msaw"]  = bool(b1 & 0x10)
    status["apw"]   = bool(b1 & 0x08)
    status["clam"]  = bool(b1 & 0x04)
    status["stca"]  = bool(b1 & 0x02)
    if not (b1 & 0x01):
        return status, pos

    if pos + 1 > len(data):
        return status, pos
    b2 = data[pos]; pos += 1
    status["apm"]      = bool(b2 & 0x80)
    status["rimca"]    = bool(b2 & 0x40)
    status["acasra"]   = bool(b2 & 0x20)
    status["ntca"]     = bool(b2 & 0x10)
    status["degraded"] = bool(b2 & 0x08)
    status["overflow"] = bool(b2 & 0x04)
    status["overload"] = bool(b2 & 0x02)
    if not (b2 & 0x01):
        return status, pos

    if pos + 1 > len(data):
        return status, pos
    b3 = data[pos]; pos += 1
    status["aiw"]  = bool(b3 & 0x80)
    status["paiw"] = bool(b3 & 0x40)
    status["ocat"] = bool(b3 & 0x20)
    status["sam"]  = bool(b3 & 0x10)
    status["vcd"]  = bool(b3 & 0x08)
    status["cham"] = bool(b3 & 0x04)
    status["dsam"] = bool(b3 & 0x02)
    ext_raw = []
    b = b3
    while b & 0x01:
        if pos >= len(data):
            break
        b = data[pos]; pos += 1
        ext_raw.append(b)
    if ext_raw:
        status["ext_raw"] = ext_raw
    return status, pos


def _cat4__decode_120(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I004/120 Conflict Characteristics — CN (FX-chained) + CC (1B) + CP (1B) + CD (3B).
    No top-level presence bitmask: all four are present whenever I004/120 itself is."""
    out = {}

    # CN — octet 1 confidently named; further FX-chained octets kept raw
    # (the spec's sub-function bit names beyond octet 1 were inconsistent
    # across passes of the public source — raw preserves the data without
    # risking a wrong label on a conflict-nature flag).
    cn_raw = []
    if pos + 1 > len(data):
        return None
    b = data[pos]; pos += 1
    out["conflict_military_airspace"] = bool(b & 0x80)
    out["conflict_civil_airspace"]    = bool(b & 0x40)
    out["conflict_fast_lateral_divergence"]  = bool(b & 0x20)
    out["conflict_fast_vertical_divergence"] = bool(b & 0x10)
    out["conflict_major_separation"] = bool(b & 0x08)
    out["conflict_crossed"]   = bool(b & 0x04)
    out["conflict_diverging"] = bool(b & 0x02)
    while b & 0x01:
        if pos + 1 > len(data):
            break
        b = data[pos]; pos += 1
        cn_raw.append(b)
    if cn_raw:
        out["conflict_nature_ext_raw"] = cn_raw

    # CC — 1 byte: TID(4) + CPC(3) + CS(1). CPC's meaning depends on TID and
    # the outer I004/000 message type — kept as a raw 3-bit value rather
    # than an interpreted label (see module docstring).
    if pos + 1 > len(data):
        return out
    b = data[pos]; pos += 1
    out["conflict_table_id"] = (b >> 4) & 0x0F
    out["conflict_properties_raw"] = (b >> 1) & 0x07
    out["conflict_severity_high"] = bool(b & 0x01)

    # CP — 1 byte unsigned, scale 0.5%
    if pos + 1 > len(data):
        return out
    out["conflict_probability_pct"] = round(data[pos] * 0.5, 2); pos += 1

    # CD — 3 bytes unsigned, scale 1/128 s
    if pos + 3 > len(data):
        return out
    raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
    out["conflict_duration_s"] = round(raw / 128.0, 4); pos += 3

    return out, pos


def _cat4__decode_070(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I004/070 Conflict Timing and Separation — compound, 1 presence byte."""
    if pos + 1 > len(data):
        return None
    fx = data[pos]; pos += 1
    out = {}
    if fx & 0x80:
        if pos + 3 > len(data): return out, pos
        raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
        out["time_to_conflict_s"] = round(raw / 128.0, 4); pos += 3
    if fx & 0x40:
        if pos + 3 > len(data): return out, pos
        raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
        out["time_to_closest_approach_s"] = round(raw / 128.0, 4); pos += 3
    if fx & 0x20:
        if pos + 3 > len(data): return out, pos
        raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
        out["current_horizontal_separation_m"] = round(raw * 0.5, 2); pos += 3
    if fx & 0x10:
        if pos + 2 > len(data): return out, pos
        out["min_horizontal_separation_m"] = round(_cat4__u16(data[pos:pos + 2]) * 0.5, 2); pos += 2
    if fx & 0x08:
        if pos + 2 > len(data): return out, pos
        out["current_vertical_separation_ft"] = round(_cat4__u16(data[pos:pos + 2]) * 25.0, 1); pos += 2
    if fx & 0x04:
        if pos + 2 > len(data): return out, pos
        out["min_vertical_separation_ft"] = round(_cat4__u16(data[pos:pos + 2]) * 25.0, 1); pos += 2
    return out, pos


def _cat4__decode_100(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I004/100 Area Definition — compound, 1 presence byte."""
    if pos + 1 > len(data):
        return None
    fx = data[pos]; pos += 1
    out = {}
    if fx & 0x80:                      # AN — 6 bytes, 6-bit ICAO chars
        if pos + 6 > len(data): return out, pos
        out["area_name"] = _cat4__decode_callsign(data[pos:pos + 6]); pos += 6
    if fx & 0x40:                      # CAN — 7 bytes ASCII
        if pos + 7 > len(data): return out, pos
        out["crossing_area_name"] = data[pos:pos + 7].decode("ascii", "replace").strip(); pos += 7
    if fx & 0x20:                      # RT1 — 7 bytes ASCII
        if pos + 7 > len(data): return out, pos
        out["runway_taxiway_1"] = data[pos:pos + 7].decode("ascii", "replace").strip(); pos += 7
    if fx & 0x10:                      # RT2 — 7 bytes ASCII
        if pos + 7 > len(data): return out, pos
        out["runway_taxiway_2"] = data[pos:pos + 7].decode("ascii", "replace").strip(); pos += 7
    if fx & 0x08:                      # SB — 7 bytes ASCII
        if pos + 7 > len(data): return out, pos
        out["stop_bar"] = data[pos:pos + 7].decode("ascii", "replace").strip(); pos += 7
    if fx & 0x04:                      # G — 7 bytes ASCII
        if pos + 7 > len(data): return out, pos
        out["gate"] = data[pos:pos + 7].decode("ascii", "replace").strip(); pos += 7
    return out, pos


def _cat4__decode_ac(raw: bytes) -> dict:
    """AC1/AC2 Aircraft Characteristics — 2 bytes of bit-packed enums."""
    w = _cat4__u16(raw)
    return {
        "gat_oat": _cat4__GATOAT.get((w >> 14) & 0x03, "undefined"),
        "flight_rules": _cat4__FR1FR2.get((w >> 12) & 0x03, "undefined"),
        "rvsm": _cat4__RVSM.get((w >> 10) & 0x03, "undefined"),
        "high_priority": bool(w & 0x0200),
        "climb_descend": _cat4__CDM.get((w >> 7) & 0x03, "undefined"),
        "primary": bool(w & 0x0040),
        "ground_vehicle": bool(w & 0x0020),
    }


def _cat4__decode_aircraft_block(data: bytes, pos: int, prefix: str) -> tuple[dict, int] | None:
    """I004/170 or I004/171 — fixed 46-byte sequential block, no presence byte."""
    if pos + 46 > len(data):
        return None
    out = {}
    out["{}_callsign".format(prefix)] = data[pos:pos + 7].decode("ascii", "replace").strip(); pos += 7
    m31 = _cat4__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
    out["{}_mode3a_squawk".format(prefix)] = "{:04o}".format(m31)
    out["{}_wgs84_lat_deg".format(prefix)] = round(_cat4__s32(data[pos:pos + 4]) * 180.0 / 2**25, 7); pos += 4
    out["{}_wgs84_lon_deg".format(prefix)] = round(_cat4__s32(data[pos:pos + 4]) * 180.0 / 2**25, 7); pos += 4
    out["{}_wgs84_alt_ft".format(prefix)] = round(_cat4__s16(data[pos:pos + 2]) * 25.0, 1); pos += 2
    out["{}_cart_x_m".format(prefix)] = round(_cat4__s24(data[pos:pos + 3]) * 0.5, 2); pos += 3
    out["{}_cart_y_m".format(prefix)] = round(_cat4__s24(data[pos:pos + 3]) * 0.5, 2); pos += 3
    out["{}_cart_z_ft".format(prefix)] = round(_cat4__s16(data[pos:pos + 2]) * 25.0, 1); pos += 2
    raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
    out["{}_time_to_threshold_s".format(prefix)] = round(raw / 128.0, 4); pos += 3
    out["{}_distance_to_threshold_m".format(prefix)] = round(_cat4__u16(data[pos:pos + 2]) * 0.5, 2); pos += 2
    out["{}_characteristics".format(prefix)] = _cat4__decode_ac(data[pos:pos + 2]); pos += 2
    out["{}_mode_s_id".format(prefix)] = _cat4__decode_callsign(data[pos:pos + 6]); pos += 6
    fp = int.from_bytes(data[pos:pos + 4], "big") & 0x07FFFFFF
    out["{}_flight_plan_number".format(prefix)] = fp; pos += 4
    out["{}_cleared_fl".format(prefix)] = round(_cat4__u16(data[pos:pos + 2]) * 0.25, 2); pos += 2
    return out, pos


def _cat4_decode_cat004(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat4_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I004/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I004/000 Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _cat4__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I004/015 SDPS Identifier (REP list)
            items, pos = _cat4__decode_sdps_list(data, pos)
            if items: msg["sdps_ids"] = items
        elif frn == 3:                  # I004/020 Time of Message
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 4:                  # I004/040 Alert Identifier
            if pos + 2 > len(data): break
            msg["alert_id"] = _cat4__u16(data[pos:pos + 2]); pos += 2
        elif frn == 5:                  # I004/045 Area and Alert Status
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["area_status_populated"] = bool(b & 0x80)
            msg["area_status"] = _cat4__AREA_STATUS.get((b >> 4) & 0x07, "undefined")
            msg["alert_status_raw"] = (b >> 1) & 0x07
        elif frn == 6:                  # I004/060 Safety Net Function/System Status
            status, pos = _cat4__decode_060(data, pos)
            if status: msg["safety_net_status"] = status
        elif frn == 7:                  # I004/030 Track Number 1
            if pos + 2 > len(data): break
            msg["track_num_1"] = _cat4__u16(data[pos:pos + 2]); pos += 2
        elif frn == 8:                  # I004/170 Aircraft 1
            result = _cat4__decode_aircraft_block(data, pos, "aircraft1")
            if result is None: break
            fields, pos = result
            msg.update(fields)
        elif frn == 9:                  # I004/120 Conflict Characteristics
            result = _cat4__decode_120(data, pos)
            if result is None: break
            fields, pos = result
            msg.update(fields)
        elif frn == 10:                 # I004/070 Conflict Timing and Separation
            result = _cat4__decode_070(data, pos)
            if result is None: break
            fields, pos = result
            if fields: msg["conflict_timing"] = fields
        elif frn == 11:                 # I004/076 Vertical Deviation
            if pos + 2 > len(data): break
            msg["vertical_deviation_ft"] = round(_cat4__s16(data[pos:pos + 2]) * 25.0, 1); pos += 2
        elif frn == 12:                 # I004/074 Longitudinal Deviation
            if pos + 2 > len(data): break
            msg["longitudinal_deviation_m"] = round(_cat4__s16(data[pos:pos + 2]) * 32.0, 1); pos += 2
        elif frn == 13:                 # I004/075 Transversal Distance Deviation
            if pos + 3 > len(data): break
            msg["transversal_deviation_m"] = round(_cat4__s24(data[pos:pos + 3]) * 0.5, 2); pos += 3
        elif frn == 14:                 # I004/100 Area Definition
            result = _cat4__decode_100(data, pos)
            if result is None: break
            fields, pos = result
            if fields: msg["area"] = fields
        elif frn == 15:                 # I004/035 Track Number 2
            if pos + 2 > len(data): break
            msg["track_num_2"] = _cat4__u16(data[pos:pos + 2]); pos += 2
        elif frn == 16:                 # I004/171 Aircraft 2
            result = _cat4__decode_aircraft_block(data, pos, "aircraft2")
            if result is None: break
            fields, pos = result
            msg.update(fields)
        elif frn == 17:                 # I004/110 FDPS Sector Control (REP list)
            items, pos = _cat4__decode_sdps_list(data, pos)
            if items: msg["fdps_sectors"] = items
        elif frn in (18, 19):           # RE / SP
            pos = _cat4__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat4__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat4_CAT_004:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat4__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat4__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat4__raw_frame_payload(bytes(sample.payload), _cat4_CAT_004)
        except ValueError as exc:
            print("CAT-4 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-4 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat4__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat4__process_stream(_cat4_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-4 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-4 TCP disconnected: {}".format(addr), flush=True)


def _cat4__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat4__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-4 Ed.1.13 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-4 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat4__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-4 Ed.1.13 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat4__process_stream(_cat4_iter_frames_udp(sock), handler, verbose)


def _cat4__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat4_decode_cat004(data)
        if msg is None:
            return
        # The alert's own position is whichever aircraft's WGS-84 fix is
        # present — aircraft1 (the primary/following aircraft in the
        # conflict) takes priority, aircraft2 as fallback.
        if "aircraft1_wgs84_lat_deg" in msg and "aircraft1_wgs84_lon_deg" in msg:
            msg["lat_deg"] = msg["aircraft1_wgs84_lat_deg"]
            msg["lon_deg"] = msg["aircraft1_wgs84_lon_deg"]
        elif "aircraft2_wgs84_lat_deg" in msg and "aircraft2_wgs84_lon_deg" in msg:
            msg["lat_deg"] = msg["aircraft2_wgs84_lat_deg"]
            msg["lon_deg"] = msg["aircraft2_wgs84_lon_deg"]
        if "lat_deg" not in msg or "lon_deg" not in msg:
            if verbose:
                print("CAT-4 dropped (no position): neither aircraft carried "
                      "I004/170.CPW nor I004/171.CPW WGS-84 coordinates", flush=True)
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-4 Ed.1.13"
        topic = _cat4_TOPIC_ALERT.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat4Alert, wrapper_field="alert")
        if verbose:
            print("PUB CAT-4 {} msg_type={} alert_id={}".format(
                topic, msg.get("msg_type"), msg.get("alert_id")), flush=True)
    return _h


def _cat4_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-004 Ed.1.13 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT4_PORT", "50004") or 50004))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT4_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT4_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT4_INPUT_TOPIC", _cat4_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT4_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat4__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat4__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat4__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-007 — Transmission of Directed Interrogation Messages, Ed.1.12
#
# Directed Mode 5/Mode S interrogation control between a ground interrogator
# and its network — a niche military IFF capability, not general
# surveillance. Two different UAPs share only their first three items
# (010, 025, 410); which one applies is only known after decoding I007/410
# (message type): 0-4 = downlink (target report), 5-8 = uplink (request).
#
# I007/415's RIM subfield lists ~30 raw Mode 4/5 control bits whose exact
# bit-to-name mapping the public spec text could not resolve without
# contradiction across passes — kept as a raw 4-byte block rather than a
# guessed name-per-bit split (see module comment style established for
# CAT-004). Every other item here decodes fully by name.
# ==========================================================================

_cat7_TOPIC_ROOT = topic_root()

_cat7_TOPIC_TRACK     = _cat7_TOPIC_ROOT + "/air/{source}/radar/unknown/aircraft"
_cat7_RAW_INPUT_TOPIC = "{}/raw/asterix/cat7".format(_cat7_TOPIC_ROOT)
_cat7_CAT_007 = 7

_cat7__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat7__TYP = {0: "no_detection", 1: "single_psr", 2: "single_ssr", 3: "ssr_psr",
              4: "mode_s_all_call", 5: "mode_s_roll_call",
              6: "mode_s_all_call_psr", 7: "mode_s_roll_call_psr"}
_cat7__FOEFRI = {0: "no_mode4", 1: "friendly", 2: "unknown", 3: "no_reply"}
_cat7__RAD = {0: "combined", 1: "psr", 2: "ssr_mode_s", 3: "invalid"}
_cat7__CDM = {0: "maintaining", 1: "climbing", 2: "descending", 3: "unknown"}
_cat7__COM = {0: "surveillance_only", 1: "comm_ab", 2: "comm_ab_ul_elm",
              3: "comm_ab_ul_dl_elm", 4: "level_5"}
_cat7__STAT = {0: "no_alert_no_spi_airborne", 1: "no_alert_no_spi_ground",
               2: "alert_no_spi_airborne", 3: "alert_no_spi_ground",
               4: "alert_spi", 5: "no_alert_spi", 7: "unknown"}
_cat7__MSG_TYPE_410 = {
    0: "acknowledge", 1: "reject", 2: "interrogation_finished",
    3: "interrogation_completed", 4: "target_report", 5: "request_type_a",
    6: "request_type_b", 7: "request_type_c", 8: "selective_bds_register",
}
_cat7__WARNING_CODES = {
    0: "not_defined", 1: "multipath_reply", 2: "sidelobe_interrogation",
    3: "split_plot", 4: "second_time_around", 5: "angel",
    6: "slow_moving_terrestrial_vehicle", 7: "fixed_psr", 8: "slow_psr",
    9: "low_quality_psr", 10: "phantom_ssr", 11: "non_matching_mode3a",
    12: "mode_c_abnormal", 13: "target_in_clutter",
    14: "max_doppler_in_zero_filter", 15: "transponder_anomaly",
    16: "duplicated_illegal_mode_s_address", 17: "mode_s_error_correction",
    18: "undecodable_mode_c", 19: "birds", 20: "flock_of_birds",
    21: "mode1_present", 22: "mode2_present", 23: "wind_turbine",
    24: "helicopter", 25: "max_reinterrogations_surveillance",
    26: "max_reinterrogations_bds", 27: "bds_overlay_incoherence",
    28: "bds_swap", 29: "track_update_in_zenithal_gap",
    30: "mode_s_reacquired", 31: "duplicated_mode5_pair",
    32: "wrong_df_format", 33: "transponder_anomaly_xpd",
    34: "transponder_anomaly_si", 35: "potential_ic_conflict",
    36: "ic_conflict_detection_possible",
    64: "ambiguous_ack_overlapping_windows",
    65: "ambiguous_ack_duplicate_request", 66: "ambiguous_ack_duplicate_track",
    67: "reject_unable_to_process", 68: "reject_too_many_parallel_requests",
    69: "reject_duplicate_request",
}


def _cat7__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat7__netbird_ip() -> str:
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


def _cat7__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat7_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat7__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat7__recv_exact(sock, length - 3)
        record_in("cat7", len(data))
        yield cat, data


def _cat7_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat7", len(record))
            yield cat, record
            offset += length


def _cat7_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat7__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat7__s16(raw: bytes) -> int:
    return struct.unpack(">h", raw)[0]


def _cat7__u16(raw: bytes) -> int:
    return struct.unpack(">H", raw)[0]


def _cat7__s24(raw: bytes) -> int:
    v = int.from_bytes(raw, "big")
    return v - (1 << 24) if v & (1 << 23) else v


def _cat7__decode_callsign(raw: bytes) -> str:
    """8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat7__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()


def _cat7__polar_to_wgs84(radar_lat: float, radar_lon: float,
                            range_nm: float, azimuth_deg: float):
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


def _cat7__cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_m: float, y_m: float):
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon


def _cat7__decode_mode_octal(data: bytes, pos: int, width: int) -> tuple[bool, bool, bool, int, int]:
    """Shared V/G/L + spare + N-bit octal-code layout (2 bytes: I007/050, I007/070)."""
    b1, b2 = data[pos], data[pos + 1]
    v = bool(b1 & 0x80); g = bool(b1 & 0x40); l = bool(b1 & 0x20)
    code = ((b1 & 0x0F) << 8) | b2
    return v, g, l, code, pos + 2


def _cat7__decode_020(data: bytes, pos: int, msg: dict) -> int:
    """I007/020 Type and Properties — 6-octet FX chain, 7 data bits + FX each."""
    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["report_type"] = _cat7__TYP.get((b >> 5) & 0x07, "undefined")
    msg["simulated"]  = bool(b & 0x10)
    msg["rdp_chain"]  = 2 if (b & 0x08) else 1
    msg["spi"]        = bool(b & 0x04)
    msg["field_monitor"] = bool(b & 0x02)
    if not (b & 0x01): return pos

    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["test_target"] = bool(b & 0x80)
    msg["extended_range"] = bool(b & 0x40)
    msg["x_pulse_present"] = bool(b & 0x20)
    msg["military_emergency"] = bool(b & 0x10)
    msg["military_id"] = bool(b & 0x08)
    msg["mode4"] = _cat7__FOEFRI.get((b >> 1) & 0x03, "undefined")
    if not (b & 0x01): return pos

    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["adsb_populated"] = bool(b & 0x80); msg["adsb_available"] = bool(b & 0x40)
    msg["scn_populated"]  = bool(b & 0x20); msg["scn_available"]  = bool(b & 0x10)
    msg["pai_populated"]  = bool(b & 0x08); msg["pai_available"]  = bool(b & 0x04)
    if not (b & 0x01): return pos

    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["acasvx_populated"] = bool(b & 0x80)
    msg["acasvx_version"] = (b >> 3) & 0x0F
    msg["poxpr_populated"] = bool(b & 0x04); msg["poxpr_available"] = bool(b & 0x02)
    if not (b & 0x01): return pos

    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["poact_populated"]  = bool(b & 0x80); msg["poact_active"]  = bool(b & 0x40)
    msg["dtfxpr_populated"] = bool(b & 0x20); msg["dtfxpr_supported"] = bool(b & 0x10)
    msg["dtfact_populated"] = bool(b & 0x08); msg["dtfact_active"] = bool(b & 0x04)
    if not (b & 0x01): return pos

    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["irmxpr_populated"] = bool(b & 0x80); msg["irmxpr_capable"] = bool(b & 0x40)
    msg["irmact_populated"] = bool(b & 0x20); msg["irmact_active"] = bool(b & 0x10)
    return pos


def _cat7__decode_170(data: bytes, pos: int, msg: dict) -> int:
    """I007/170 Track Status — 1-2 octet FX chain."""
    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["confirmed"] = not bool(b & 0x80)
    msg["radar_type"] = _cat7__RAD.get((b >> 5) & 0x03, "undefined")
    msg["low_confidence"] = bool(b & 0x10)
    msg["horizontal_maneuver"] = bool(b & 0x08)
    msg["climb_descend"] = _cat7__CDM.get((b >> 1) & 0x03, "undefined")
    if not (b & 0x01): return pos

    if pos + 1 > len(data): return pos
    b = data[pos]; pos += 1
    msg["end_of_track"] = bool(b & 0x80)
    msg["ghost_track"]  = bool(b & 0x40)
    msg["neighboring_node"] = bool(b & 0x20)
    msg["slant_range_corrected"] = bool(b & 0x10)
    return pos


def _cat7__decode_conf12(data: bytes, pos: int) -> tuple[dict, int]:
    """Shared 12-bit A/B/C/D pulse-quality group (I007/060, I007/080)."""
    raw = _cat7__u16(data[pos:pos + 2]) & 0x0FFF
    names = ("qa4", "qa2", "qa1", "qb4", "qb2", "qb1",
             "qc4", "qc2", "qc1", "qd4", "qd2", "qd1")
    quality = {}
    for index, name in enumerate(names):
        shift = len(names) - 1 - index
        quality[name] = bool(raw & (1 << shift))
    return quality, pos + 2


def _cat7__decode_085(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I007/085 Mode 5, Extended Mode 1, and X-Pulse — compound, 1 presence byte."""
    if pos + 1 > len(data): return None
    fx = data[pos]; pos += 1
    out = {}
    if fx & 0x80:                     # SUM
        if pos + 1 > len(data): return out, pos
        b = data[pos]; pos += 1
        out["mode5_interrogation"] = bool(b & 0x80)
        out["mode5_id_reply"] = bool(b & 0x40)
        out["mode5_data_reply"] = bool(b & 0x20)
        out["mode5_m1"] = bool(b & 0x10)
        out["mode5_m2"] = bool(b & 0x08)
        out["mode5_m3"] = bool(b & 0x04)
        out["mode5_mc"] = bool(b & 0x02)
    if fx & 0x40:                     # PMN — 4 bytes: spare2+PIN14+spare3+NAT5+spare2+MIS6
        if pos + 4 > len(data): return out, pos
        raw = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
        out["mode5_pin"] = (raw >> 16) & 0x3FFF
        out["mode5_nat"] = (raw >> 8) & 0x1F
        out["mode5_mis"] = raw & 0x3F
    if fx & 0x20:                     # POS — LAT/LON 24-bit signed, 180/2^23 deg
        if pos + 6 > len(data): return out, pos
        out["mode5_lat_deg"] = round(_cat7__s24(data[pos:pos + 3]) * 180.0 / 2**23, 7)
        out["mode5_lon_deg"] = round(_cat7__s24(data[pos + 3:pos + 6]) * 180.0 / 2**23, 7)
        pos += 6
    if fx & 0x10:                     # GA — GNSS altitude
        if pos + 2 > len(data): return out, pos
        b1, b2 = data[pos], data[pos + 1]
        res_25ft = bool(b1 & 0x40)
        raw14 = ((b1 & 0x3F) << 8) | b2
        if raw14 & 0x2000: raw14 -= 1 << 14
        out["mode5_gnss_alt_ft"] = round(raw14 * (25.0 if res_25ft else 100.0), 1)
        pos += 2
    if fx & 0x08:                     # EM1 — Extended Mode 1 (octal)
        if pos + 2 > len(data): return out, pos
        v, g, l, code, pos = _cat7__decode_mode_octal(data, pos, 12)
        out["extended_mode1_squawk"] = "{:04o}".format(code)
        out["extended_mode1_validated"] = not v
        out["extended_mode1_garbled"] = g
    if fx & 0x04:                     # TOS — Time offset
        if pos + 1 > len(data): return out, pos
        out["time_offset_s"] = round(struct.unpack("b", bytes((data[pos],)))[0] / 128.0, 4)
        pos += 1
    if fx & 0x02:                     # XP — X-Pulse presence
        if pos + 1 > len(data): return out, pos
        b = data[pos]; pos += 1
        out["x_pulse_5"] = bool(b & 0x10)
        out["x_pulse_c"] = bool(b & 0x08)
        out["x_pulse_3"] = bool(b & 0x04)
        out["x_pulse_2"] = bool(b & 0x02)
        out["x_pulse_1"] = bool(b & 0x01)
    return out, pos


def _cat7__decode_120(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I007/120 Radial Doppler Speed — compound, 1 presence byte."""
    if pos + 1 > len(data): return None
    fx = data[pos]; pos += 1
    out = {}
    if fx & 0x80:                     # CAL
        if pos + 2 > len(data): return out, pos
        b1, b2 = data[pos], data[pos + 1]
        doubtful = bool(b1 & 0x80)
        raw10 = ((b1 & 0x03) << 8) | b2
        if raw10 & 0x0200: raw10 -= 1 << 10
        out["doppler_speed_doubtful"] = doubtful
        out["doppler_speed_ms"] = raw10
        pos += 2
    if fx & 0x40:                     # RDS — REP(1) + N x (DOP+AMB+FRQ, 6 bytes)
        if pos >= len(data): return out, pos
        rep = data[pos]; pos += 1
        entries = []
        for _ in range(rep):
            if pos + 6 > len(data): break
            entries.append({
                "doppler_ms": _cat7__u16(data[pos:pos + 2]),
                "ambiguous_doppler_ms": _cat7__u16(data[pos + 2:pos + 4]),
                "frequency_mhz": _cat7__u16(data[pos + 4:pos + 6]),
            })
            pos += 6
        if entries: out["raw_doppler"] = entries
    return out, pos


def _cat7__decode_130(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I007/130 Radar Plot Characteristics — compound, 1 presence byte."""
    if pos + 1 > len(data): return None
    fx = data[pos]; pos += 1
    out = {}
    if fx & 0x80:
        if pos + 1 > len(data): return out, pos
        out["ssr_runlength_deg"] = round(data[pos] * (360.0 / 8192.0), 4); pos += 1
    if fx & 0x40:
        if pos + 1 > len(data): return out, pos
        out["ssr_reply_count"] = data[pos]; pos += 1
    if fx & 0x20:
        if pos + 1 > len(data): return out, pos
        out["ssr_amplitude_dbm"] = struct.unpack("b", bytes((data[pos],)))[0]; pos += 1
    if fx & 0x10:
        if pos + 1 > len(data): return out, pos
        out["psr_runlength_deg"] = round(data[pos] * (360.0 / 8192.0), 4); pos += 1
    if fx & 0x08:
        if pos + 1 > len(data): return out, pos
        out["psr_amplitude_dbm"] = struct.unpack("b", bytes((data[pos],)))[0]; pos += 1
    if fx & 0x04:
        if pos + 1 > len(data): return out, pos
        out["psr_ssr_range_diff_nm"] = round(struct.unpack("b", bytes((data[pos],)))[0] / 256.0, 5); pos += 1
    if fx & 0x02:
        if pos + 1 > len(data): return out, pos
        out["psr_ssr_azimuth_diff_deg"] = round(struct.unpack("b", bytes((data[pos],)))[0] * (360.0 / 16384.0), 4); pos += 1
    return out, pos


def _cat7__decode_415(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I007/415 Required Interrogation Modes — compound, 1 presence byte.

    RIM's ~30 individual Mode 4/5 control bits could not be resolved to a
    confident bit-order from the public spec text (internally inconsistent
    across passes) — kept as a raw 4-byte block rather than a guessed
    name-per-bit split.
    """
    if pos + 1 > len(data): return None
    fx = data[pos]; pos += 1
    out = {}
    if fx & 0x80:
        if pos + 4 > len(data): return out, pos
        out["required_interrogation_modes_raw"] = data[pos:pos + 4].hex()
        pos += 4
    if fx & 0x40:
        if pos + 1 > len(data): return out, pos
        out["mip_table_raw"] = data[pos]
        pos += 1
    return out, pos


def _cat7__decode_450(data: bytes, pos: int) -> tuple[dict, int] | None:
    """I007/450 Directed Interrogation Result — compound, 1 presence byte."""
    if pos + 1 > len(data): return None
    fx = data[pos]; pos += 1
    out = {}
    if fx & 0x80:
        if pos + 1 > len(data): return out, pos
        b = data[pos]; pos += 1
        out["not_executed"] = bool(b & 0x08)
        out["truncated_all_call"] = bool(b & 0x04)
        out["activated_once"] = bool(b & 0x02)
        out["activated_all_validity"] = bool(b & 0x01)
    if fx & 0x40:
        if pos + 1 > len(data): return out, pos
        out["mode4_interrogations_raw"] = data[pos]; pos += 1
    if fx & 0x20:
        if pos + 1 > len(data): return out, pos
        out["mode5_interrogations_raw"] = data[pos]; pos += 1
    if fx & 0x10:
        if pos + 2 > len(data): return out, pos
        w = _cat7__u16(data[pos:pos + 2])
        out["mode_s_all_call_lockout"] = ("no_lockout", "lockout_used", "lockout_override")[min((w >> 8) & 0x03, 2)]
        out["mode_s_all_call_count"] = w & 0xFF
        pos += 2
    if fx & 0x08:
        if pos + 1 > len(data): return out, pos
        out["mark_x_interrogations_raw"] = data[pos]; pos += 1
    if fx & 0x04:
        if pos + 1 > len(data): return out, pos
        out["selective_mode_s_raw"] = data[pos]; pos += 1
    return out, pos


def _cat7_decode_cat007(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat7_parse_fspec(data, 0)
    msg = {}

    if len(fspec) < 3:
        return None
    # FRN1/010, FRN2/025, FRN3/410 are shared by both UAPs and always first.
    if fspec[0]:
        if pos + 2 > len(data): return None
        msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
    if fspec[1]:
        if pos + 2 > len(data): return None
        msg["dest_sac"] = data[pos]; msg["dest_sic"] = data[pos + 1]; pos += 2
    if fspec[2]:
        if pos + 1 > len(data): return None
        msg["msg_type"] = _cat7__MSG_TYPE_410.get(data[pos], "type_{}".format(data[pos]))
        msg["is_uplink"] = data[pos] >= 5
        pos += 1

    is_uplink = msg.get("is_uplink", False)
    downlink_items = ("140", "400", "020", "040", "070", "090", "130", "220",
                        "240", "250", "161", "042", "200", "170", "210", "030",
                        "080", "100", "110", "120", "230", "260", "055", "050",
                        "065", "060", "450", "085")
    uplink_items = ("140", "400", "040", "220", "161", "042", "200", "415", "420", "440")
    items = uplink_items if is_uplink else downlink_items

    for offset, present in enumerate(fspec[3:]):
        if not present:
            continue
        if offset >= len(items):
            pos = _cat7__skip_len_field(data, pos)
            continue
        tag = items[offset]
        if tag == "140":                # Time of Day
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif tag == "400":              # Directed Interrogation Request Number
            if pos + 2 > len(data): break
            w = _cat7__u16(data[pos:pos + 2]); pos += 2
            msg["request_high_priority"] = bool(w & 0x8000)
            msg["request_number"] = w & 0x7FFF
        elif tag == "020":               # Type and Properties (downlink only)
            pos = _cat7__decode_020(data, pos, msg)
        elif tag == "040":               # Polar position
            if pos + 4 > len(data): break
            rho = _cat7__u16(data[pos:pos + 2]) / 256.0
            theta = _cat7__u16(data[pos + 2:pos + 4]) * (360.0 / 65536.0)
            msg["rho_nm"] = round(rho, 4); msg["theta_deg"] = round(theta, 4); pos += 4
        elif tag == "042":               # Cartesian position
            if pos + 4 > len(data): break
            msg["x_nm"] = round(_cat7__s16(data[pos:pos + 2]) / 128.0, 4)
            msg["y_nm"] = round(_cat7__s16(data[pos + 2:pos + 4]) / 128.0, 4)
            pos += 4
        elif tag == "070":               # Mode-3/A code
            if pos + 2 > len(data): break
            v, g, l, code, pos = _cat7__decode_mode_octal(data, pos, 12)
            msg["mode3a_squawk"] = "{:04o}".format(code)
            msg["mode3a_validated"] = not v; msg["mode3a_garbled"] = g
        elif tag == "090":               # Flight Level
            if pos + 2 > len(data): break
            b1, b2 = data[pos], data[pos + 1]
            v = bool(b1 & 0x80); g = bool(b1 & 0x40)
            raw14 = ((b1 & 0x3F) << 8) | b2
            if raw14 & 0x2000: raw14 -= 1 << 14
            msg["flight_level"] = round(raw14 * 0.25, 2)
            msg["fl_validated"] = not v; msg["fl_garbled"] = g
            pos += 2
        elif tag == "130":
            result = _cat7__decode_130(data, pos)
            if result is None: break
            fields, pos = result
            if fields: msg["plot_characteristics"] = fields
        elif tag == "220":               # Aircraft Address
            if pos + 3 > len(data): break
            msg["icao24"] = "{:06x}".format(int.from_bytes(data[pos:pos + 3], "big")); pos += 3
        elif tag == "240":               # Aircraft Identification
            if pos + 6 > len(data): break
            msg["callsign"] = _cat7__decode_callsign(data[pos:pos + 6]); pos += 6
        elif tag == "250":               # Mode S MB Data (repetitive, REP-count)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            entries = []
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb = data[pos:pos + 7]; bds = data[pos + 7]
                entries.append({"data": mb.hex(), "bds1": (bds >> 4) & 0x0F, "bds2": bds & 0x0F})
                pos += 8
            if entries: msg["mode_s_mb_data"] = entries
        elif tag == "161":               # Track Number
            if pos + 2 > len(data): break
            msg["track_num"] = _cat7__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif tag == "200":               # Calculated Track Velocity
            if pos + 4 > len(data): break
            msg["speed_ms"] = round(_cat7__u16(data[pos:pos + 2]) / 16384.0 * 1852.0, 3)
            msg["heading_deg"] = round(_cat7__u16(data[pos + 2:pos + 4]) * (360.0 / 65536.0), 4)
            pos += 4
        elif tag == "170":
            pos = _cat7__decode_170(data, pos, msg)
        elif tag == "210":               # Track Quality
            if pos + 4 > len(data): break
            msg["sigma_x_nm"] = round(data[pos] / 128.0, 4)
            msg["sigma_y_nm"] = round(data[pos + 1] / 128.0, 4)
            msg["sigma_speed_ms"] = round(data[pos + 2] / 16384.0 * 1852.0, 4)
            msg["sigma_heading_deg"] = round(data[pos + 3] * (360.0 / 4096.0), 4)
            pos += 4
        elif tag == "030":               # Warning/Error Conditions (FX-repetitive)
            codes = []
            while pos < len(data):
                b = data[pos]; pos += 1
                codes.append(b >> 1)
                if not (b & 0x01): break
            if codes: msg["warnings"] = [_cat7__WARNING_CODES.get(c, "code_{}".format(c)) for c in codes]
        elif tag == "080":               # Mode-3/A confidence
            result = _cat7__decode_conf12(data, pos)
            if result is None: break
            msg["mode3a_confidence"], pos = result
        elif tag == "060":               # Mode-2 confidence
            result = _cat7__decode_conf12(data, pos)
            if result is None: break
            msg["mode2_confidence"], pos = result
        elif tag == "100":               # Mode-C code + confidence
            if pos + 4 > len(data): break
            b1, b2 = data[pos], data[pos + 1]
            v = bool(b1 & 0x80); g = bool(b1 & 0x40)
            modec = ((b1 & 0x0F) << 8) | b2
            msg["mode_c_gray_code"] = modec
            msg["mode_c_validated"] = not v; msg["mode_c_garbled"] = g
            pos += 2
            raw = _cat7__u16(data[pos:pos + 2]); pos += 2
            names = ("qc1", "qa1", "qc2", "qa2", "qc4", "qa4", "qb1", "qd1",
                     "qb2", "qd2", "qb4", "qd4")
            quality = {}
            for index, name in enumerate(names):
                shift = len(names) - 1 - index
                quality[name] = bool(raw & (1 << shift))
            msg["mode_c_confidence"] = quality
        elif tag == "110":               # 3D Height
            if pos + 2 > len(data): break
            raw14 = _cat7__u16(data[pos:pos + 2]) & 0x3FFF
            if raw14 & 0x2000: raw14 -= 1 << 14
            msg["height_3d_ft"] = round(raw14 * 25.0, 1); pos += 2
        elif tag == "120":
            result = _cat7__decode_120(data, pos)
            if result is None: break
            fields, pos = result
            if fields: msg["radial_doppler"] = fields
        elif tag == "230":               # Comms/ACAS Capability
            if pos + 3 > len(data): break
            b1, b2, b3 = data[pos], data[pos + 1], data[pos + 2]
            msg["comm_capability"] = _cat7__COM.get((b1 >> 5) & 0x07, "undefined")
            msg["flight_status"] = _cat7__STAT.get((b1 >> 2) & 0x07, "undefined")
            msg["si_capable"] = not bool(b1 & 0x02)
            msg["mode_s_service_capable"] = bool(b2 & 0x40)
            msg["altitude_resolution_25ft"] = bool(b2 & 0x20)
            msg["aircraft_id_capable"] = bool(b2 & 0x10)
            msg["bds10_raw"] = ((b2 & 0x0F) << 4) | ((b3 >> 4) & 0x0F)
            pos += 3
        elif tag == "260":               # ACAS RA Report
            if pos + 7 > len(data): break
            msg["acas_ra_raw"] = data[pos:pos + 7].hex(); pos += 7
        elif tag == "055":               # Mode-1 code
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["mode1_validated"] = not bool(b & 0x80)
            msg["mode1_garbled"] = bool(b & 0x40)
            msg["mode1_smoothed"] = bool(b & 0x20)
            msg["mode1_code"] = "{:02o}".format(b & 0x1F)
        elif tag == "050":               # Mode-2 code
            if pos + 2 > len(data): break
            v, g, l, code, pos = _cat7__decode_mode_octal(data, pos, 12)
            msg["mode2_squawk"] = "{:04o}".format(code)
            msg["mode2_validated"] = not v; msg["mode2_garbled"] = g
        elif tag == "065":               # Mode-1 confidence
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["mode1_confidence"] = {
                "qa4": bool(b & 0x20), "qa2": bool(b & 0x10), "qa1": bool(b & 0x08),
                "qb2": bool(b & 0x02), "qb1": bool(b & 0x01),
            }
        elif tag == "450":
            result = _cat7__decode_450(data, pos)
            if result is None: break
            fields, pos = result
            if fields: msg["interrogation_result"] = fields
        elif tag == "085":
            result = _cat7__decode_085(data, pos)
            if result is None: break
            fields, pos = result
            if fields: msg["mode5"] = fields
        elif tag == "415":
            result = _cat7__decode_415(data, pos)
            if result is None: break
            fields, pos = result
            if fields: msg["required_interrogation_modes"] = fields
        elif tag == "420":               # Directed Interrogation Window
            if pos + 8 > len(data): break
            msg["window_rho_start_nm"] = round(_cat7__u16(data[pos:pos + 2]) / 256.0, 4)
            msg["window_rho_end_nm"]   = round(_cat7__u16(data[pos + 2:pos + 4]) / 256.0, 4)
            msg["window_theta_start_deg"] = round(_cat7__u16(data[pos + 4:pos + 6]) * (360.0 / 65536.0), 4)
            msg["window_theta_end_deg"]   = round(_cat7__u16(data[pos + 6:pos + 8]) * (360.0 / 65536.0), 4)
            pos += 8
        elif tag == "440":               # BDS Register Request (repetitive, REP-count)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            regs = []
            for _ in range(rep):
                if pos + 1 > len(data): break
                b = data[pos]; pos += 1
                regs.append({"bds1": (b >> 4) & 0x0F, "bds2": b & 0x0F})
            if regs: msg["bds_register_requests"] = regs
        else:
            break
    return msg if msg else None


def _cat7__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat7_CAT_007:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat7__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat7__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat7__raw_frame_payload(bytes(sample.payload), _cat7_CAT_007)
        except ValueError as exc:
            print("CAT-7 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-7 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat7__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat7__process_stream(_cat7_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-7 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-7 TCP disconnected: {}".format(addr), flush=True)


def _cat7__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat7__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-7 Ed.1.12 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-7 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat7__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-7 Ed.1.12 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat7__process_stream(_cat7_iter_frames_udp(sock), handler, verbose)


def _cat7__make_handler(session, verbose_default: bool, site_lat=None, site_lon=None):
    def _h(data: bytes, verbose: bool):
        msg = _cat7_decode_cat007(data)
        if msg is None:
            return
        if "rho_nm" in msg and "theta_deg" in msg and site_lat is not None and site_lon is not None:
            lat, lon = _cat7__polar_to_wgs84(site_lat, site_lon, msg["rho_nm"], msg["theta_deg"])
            msg.setdefault("lat_deg", round(lat, 7)); msg.setdefault("lon_deg", round(lon, 7))
        elif "x_nm" in msg and "y_nm" in msg and site_lat is not None and site_lon is not None:
            lat, lon = _cat7__cartesian_to_wgs84(site_lat, site_lon, msg["x_nm"] * 1852.0, msg["y_nm"] * 1852.0)
            msg.setdefault("lat_deg", round(lat, 7)); msg.setdefault("lon_deg", round(lon, 7))
        elif "mode5" in msg and "mode5_lat_deg" in msg["mode5"] and "mode5_lon_deg" in msg["mode5"]:
            msg["lat_deg"] = msg["mode5"]["mode5_lat_deg"]
            msg["lon_deg"] = msg["mode5"]["mode5_lon_deg"]
        if "lat_deg" not in msg or "lon_deg" not in msg:
            if verbose:
                print("CAT-7 dropped (no position): send I007/040/042 with "
                      "CAT7_RADAR_LAT/LON, or I007/085 Mode 5 position", flush=True)
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-7 Ed.1.12"
        topic = _cat7_TOPIC_TRACK.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat7Track)
        if verbose:
            print("PUB CAT-7 {} msg_type={} track={}".format(
                topic, msg.get("msg_type"), msg.get("track_num")), flush=True)
    return _h


def _cat7_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-007 Ed.1.12 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT7_PORT", "50007") or 50007))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT7_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT7_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT7_INPUT_TOPIC", _cat7_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat7__env_float("CAT7_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat7__env_float("CAT7_RADAR_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT7_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat7__make_handler(session, args.verbose,
                                   site_lat=args.site_lat or None, site_lon=args.site_lon or None)
    try:
        if args.zenoh_raw: _cat7__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat7__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-008 — Monoradar Derived Weather Information, Ed.1.3
#
# Weather-image vectors/contours from a primary radar's clutter/reflectivity
# processing — not target tracks. Coordinate items (I008/034/036/038/050)
# are all specified in units of "100/F" where F is I008/100's own Scaling
# Factor — but F is a different FRN, decoded separately, and per the public
# spec is potentially per-image (SOP/EOP) rather than guaranteed present in
# every vector-carrying record. Rather than assume a default F and silently
# misrepresent distances, coordinates are kept as their raw pre-scale
# integers; a consumer that tracks a session's own I008/100.F can convert.
# ==========================================================================

_cat8_TOPIC_ROOT = topic_root()

_cat8_TOPIC_SENSOR    = _cat8_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat8_RAW_INPUT_TOPIC = "{}/raw/asterix/cat8".format(_cat8_TOPIC_ROOT)
_cat8_CAT_008 = 8

_cat8__MSG_TYPES = {1: "polar_vector", 2: "cartesian_vector_start_length",
                     3: "contour_record", 4: "cartesian_vector_start_end",
                     254: "sop", 255: "eop"}
_cat8__FSTLST = {0: "intermediate", 1: "last", 2: "first", 3: "first_and_only"}


def _cat8__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat8__netbird_ip() -> str:
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


def _cat8__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat8_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat8__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat8__recv_exact(sock, length - 3)
        record_in("cat8", len(data))
        yield cat, data


def _cat8_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat8", len(record))
            yield cat, record
            offset += length


def _cat8_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat8__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat8__s8(b: int) -> int:
    return b - 256 if b & 0x80 else b


def _cat8__decode_rep3(data: bytes, pos: int, names: tuple) -> tuple[list, int]:
    """REP(1 byte) + N x 3-byte signed/unsigned entries (I008/034, I008/036)."""
    if pos >= len(data): return [], pos
    rep = data[pos]; pos += 1
    entries = []
    for _ in range(rep):
        if pos + 3 > len(data): break
        entries.append(dict(zip(names, (data[pos], data[pos + 1], data[pos + 2]))))
        pos += 3
    return entries, pos


def _cat8_decode_cat008(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat8_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I008/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I008/000 Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _cat8__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I008/020 Vector Qualifier (FX)
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["coord_system"] = "system" if (b & 0x80) else "local"
            msg["intensity"] = (b >> 4) & 0x07
            msg["shading_deg"] = ((b >> 1) & 0x07) * 22.5
            if b & 0x01:
                if pos + 1 > len(data): break
                b2 = data[pos]; pos += 1
                msg["test_vector"] = bool(b2 & 0x04)
                msg["error_condition"] = bool(b2 & 0x02)
        elif frn == 3:                  # I008/036 Cartesian Vectors (start/length)
            entries, pos = _cat8__decode_rep3(data, pos, ("x_raw", "y_raw", "length_raw"))
            if entries:
                for e in entries:
                    e["x_raw"] = _cat8__s8(e["x_raw"]); e["y_raw"] = _cat8__s8(e["y_raw"])
                msg["cartesian_vectors"] = entries
        elif frn == 4:                  # I008/034 Polar Vectors (STR 1B + ENDR 1B + AZ 2B)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            entries = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                entries.append({
                    "start_range_raw": data[pos],
                    "end_range_raw": data[pos + 1],
                    "azimuth_deg": round(struct.unpack(">H", data[pos + 2:pos + 4])[0] * (360.0 / 65536.0), 4),
                })
                pos += 4
            if entries: msg["polar_vectors"] = entries
        elif frn == 5:                  # I008/040 Contour Identifier
            if pos + 2 > len(data): break
            b = data[pos]; pos += 1
            msg["contour_coord_system"] = "system" if (b & 0x80) else "local"
            msg["contour_intensity"] = (b >> 4) & 0x07
            msg["contour_position"] = _cat8__FSTLST.get(b & 0x03, "undefined")
            msg["contour_serial_number"] = data[pos]; pos += 1
        elif frn == 6:                  # I008/050 Contour Points
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            points = []
            for _ in range(rep):
                if pos + 2 > len(data): break
                points.append({"x_raw": _cat8__s8(data[pos]), "y_raw": _cat8__s8(data[pos + 1])})
                pos += 2
            if points: msg["contour_points"] = points
        elif frn == 7:                  # I008/090 Time of Day
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 8:                  # I008/100 Processing Status (F+R, then Q+FX chain)
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            f5 = (b >> 3) & 0x1F
            if f5 & 0x10: f5 -= 32
            msg["scaling_factor"] = f5
            msg["reduction_stage"] = b & 0x07
            q_values = []
            while pos + 2 <= len(data):
                w = _cat8__u16_local(data[pos:pos + 2]); pos += 2
                q_values.append((w >> 1) & 0x7FFF)
                if not (w & 0x0001):
                    break
            if q_values: msg["processing_params_raw"] = q_values
        elif frn == 9:                  # I008/110 Station Configuration Status (FX-repetitive)
            values = []
            while pos < len(data):
                b = data[pos]; pos += 1
                values.append(b >> 1)
                if not (b & 0x01): break
            if values: msg["station_configuration_raw"] = values
        elif frn == 10:                 # I008/120 Total Number of Items
            if pos + 2 > len(data): break
            msg["total_items"] = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
        elif frn == 11:                 # I008/038 Weather Vectors (start/end point)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            entries = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                entries.append({
                    "x1_raw": _cat8__s8(data[pos]), "y1_raw": _cat8__s8(data[pos + 1]),
                    "x2_raw": _cat8__s8(data[pos + 2]), "y2_raw": _cat8__s8(data[pos + 3]),
                })
                pos += 4
            if entries: msg["weather_vectors"] = entries
        elif frn in (12, 13):           # SP / RFS
            pos = _cat8__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat8__u16_local(raw: bytes) -> int:
    return struct.unpack(">H", raw)[0]


def _cat8__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat8_CAT_008:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat8__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat8__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat8__raw_frame_payload(bytes(sample.payload), _cat8_CAT_008)
        except ValueError as exc:
            print("CAT-8 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-8 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat8__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat8__process_stream(_cat8_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-8 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-8 TCP disconnected: {}".format(addr), flush=True)


def _cat8__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat8__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-8 Ed.1.3 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-8 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat8__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-8 Ed.1.3 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat8__process_stream(_cat8_iter_frames_udp(sock), handler, verbose)


def _cat8__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat8_decode_cat008(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-8 Ed.1.3"
        topic = _cat8_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat8Weather, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-8 {} msg_type={}".format(topic, msg.get("msg_type")), flush=True)
    return _h


def _cat8_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-008 Ed.1.3 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT8_PORT", "50008") or 50008))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT8_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT8_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT8_INPUT_TOPIC", _cat8_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT8_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat8__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat8__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat8__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-009 — Composite Weather Reports, Ed.2.1
#
# Same weather-vector idea as CAT-008, but for a COMPOSITE picture merged
# from multiple contributing radars (I009/090 lists per-radar status). Same
# F-dependent coordinate scale caveat as CAT-008: I009/030's X/Y/L are kept
# as raw pre-scale integers rather than assuming a default scaling factor.
# ==========================================================================

_cat9_TOPIC_ROOT = topic_root()

_cat9_TOPIC_SENSOR    = _cat9_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat9_RAW_INPUT_TOPIC = "{}/raw/asterix/cat9".format(_cat9_TOPIC_ROOT)
_cat9_CAT_009 = 9

_cat9__MSG_TYPES = {2: "cartesian_vector", 253: "intermediate_update_step",
                     254: "start_of_picture", 255: "end_of_picture"}


def _cat9__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat9__netbird_ip() -> str:
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


def _cat9__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat9_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat9__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat9__recv_exact(sock, length - 3)
        record_in("cat9", len(data))
        yield cat, data


def _cat9_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat9", len(record))
            yield cat, record
            offset += length


def _cat9_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat9__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat9_decode_cat009(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat9_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I009/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I009/000 Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _cat9__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I009/020 Vector Qualifier (FX)
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["coord_system"] = "system" if (b & 0x80) else "local"
            msg["intensity"] = (b >> 4) & 0x07
            msg["shading_deg"] = ((b >> 1) & 0x07) * 22.5
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
        elif frn == 3:                  # I009/030 Cartesian Vectors (REP + N x 6 bytes, raw pre-scale)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            entries = []
            for _ in range(rep):
                if pos + 6 > len(data): break
                entries.append({
                    "x_raw": struct.unpack(">h", data[pos:pos + 2])[0],
                    "y_raw": struct.unpack(">h", data[pos + 2:pos + 4])[0],
                    "length_raw": struct.unpack(">H", data[pos + 4:pos + 6])[0],
                })
                pos += 6
            if entries: msg["cartesian_vectors"] = entries
        elif frn == 4:                  # I009/060 Synchronisation/Control Signal (FX)
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["step_number"] = (b >> 2) & 0x3F
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
        elif frn == 5:                  # I009/070 Time of Day
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 6:                  # I009/080 Processing Status (F+R fixed, then Q+FX chain)
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            f5 = (b >> 3) & 0x1F
            if f5 & 0x10: f5 -= 32
            msg["scaling_factor"] = f5
            msg["reduction_stage"] = b & 0x07
            q_values = []
            while pos + 2 <= len(data):
                w = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
                q_values.append((w >> 1) & 0x7FFF)
                if not (w & 0x0001):
                    break
            if q_values: msg["processing_params_raw"] = q_values
        elif frn == 7:                  # I009/090 Radar Configuration and Status (REP + N x 2 bytes)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            entries = []
            for _ in range(rep):
                if pos + 2 > len(data): break
                b1, b2 = data[pos], data[pos + 1]
                entries.append({
                    "sac": b1,
                    "circular_polarisation": bool(b2 & 0x10),
                    "weather_channel_overload": bool(b2 & 0x08),
                    "reduction_step": b2 & 0x07,
                })
                pos += 2
            if entries: msg["radar_status"] = entries
        elif frn == 8:                  # I009/100 Vector Count
            if pos + 2 > len(data): break
            msg["vector_count"] = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
        elif frn in (9, 10, 11, 12, 13):  # spare / SP / RFS
            pos = _cat9__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat9__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat9_CAT_009:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat9__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat9__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat9__raw_frame_payload(bytes(sample.payload), _cat9_CAT_009)
        except ValueError as exc:
            print("CAT-9 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-9 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat9__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat9__process_stream(_cat9_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-9 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-9 TCP disconnected: {}".format(addr), flush=True)


def _cat9__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat9__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-9 Ed.2.1 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-9 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat9__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-9 Ed.2.1 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat9__process_stream(_cat9_iter_frames_udp(sock), handler, verbose)


def _cat9__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat9_decode_cat009(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-9 Ed.2.1"
        topic = _cat9_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat9Weather, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-9 {} msg_type={}".format(topic, msg.get("msg_type")), flush=True)
    return _h


def _cat9_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-009 Ed.2.1 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT9_PORT", "50009") or 50009))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT9_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT9_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT9_INPUT_TOPIC", _cat9_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT9_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat9__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat9__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat9__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-010
# ==========================================================================

import argparse

import json

import math

import os

import socket

import struct

import threading

import time

from namespace_prefix import topic_root
from protocols.track_views import (publish_dual, publish_native, native_topic,
                                      semantic_topic, asterix_data_block)
from protocols.data_stats import record_in



_cat10_TOPIC_ROOT = topic_root()




# CAT-010 is airport surface movement: a surface movement radar, so `radar`.
_cat10_TOPIC_010_AIR = _cat10_TOPIC_ROOT + "/air/{source}/radar/civ/aircraft"

_cat10_TOPIC_010_GROUND = _cat10_TOPIC_ROOT + "/land/{source}/radar/unknown/vehicle"

_cat10_TOPIC_010_SENSOR = _cat10_TOPIC_ROOT + "/land/{source}/radar/neutral/radar"
_cat10_RAW_INPUT_TOPIC = "{}/raw/asterix/cat10".format(_cat10_TOPIC_ROOT)

_cat10_CAT_010 = 0x0A

_cat10__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat10__CAT010_MESSAGE_TYPES = {
    1: "target_report",
    2: "start_update_cycle",
    3: "periodic_status",
    4: "event_status",
}

_cat10__CAT010_SENSOR_TYPES = {
    0: "ssr_mlat", 1: "mode_s_mlat", 2: "adsb", 3: "psr",
    4: "magnetic_loop", 5: "hf_mlat", 6: "undefined", 7: "other",
}

_cat10__CAT010_TARGET_TYPES = {0: "undetermined", 1: "aircraft", 2: "ground_vehicle", 3: "helicopter"}

_cat10__CAT010_FLEETS = {
    0: "unknown", 1: "atc_maintenance", 2: "airport_maintenance", 3: "fire",
    4: "bird_scarer", 5: "snow_plough", 6: "runway_sweeper", 7: "emergency",
    8: "police", 9: "bus", 10: "tug", 11: "grass_cutter", 12: "fuel",
    13: "baggage", 14: "catering", 15: "aircraft_maintenance", 16: "follow_me",
}

def _cat10__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0

def _cat10__netbird_ip() -> str:
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

def _cat10__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat10_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat10__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat10__recv_exact(sock, length - 3)
        record_in("cat10", len(data))
        yield cat, data

def _cat10_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat10", len(record))
            yield cat, record
            offset += length

def _cat10_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat10__skip_len_field(data: bytes, pos: int) -> int:
    """Skip an ASTERIX length-prefixed RE/SP item (length includes itself)."""
    if pos >= len(data):
        return len(data)
    length = data[pos]
    if length < 1 or pos + length > len(data):
        return len(data)
    return pos + length

def _cat10__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat10__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat10__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat10__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat10__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat10__decode_bds50(mb: bytes) -> dict:
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
    if _bit(24): out["bds_gs_kt"]        = round(_uns(25, 34) * 2.0, 0)
    if _bit(35): out["track_rate_degs"]  = round(_sgn(36, 45) * 8.0 / 256.0, 2)
    if _bit(46): out["tas_kt"]           = round(_uns(47, 56) * 2.0, 0)
    return out

def _cat10__decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
    if _bit(13): out["ias_kt"]       = _uns(14, 23)
    if _bit(24): out["mach"]         = round(_uns(25, 34) * 2.048 / 512.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 45) * 32
    if _bit(46): out["ivv_fpm"]      = _sgn(47, 56) * 32
    return out

def _cat10__decode_bds40(mb: bytes) -> dict:
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

def _cat10__decode_bds30(mb: bytes) -> dict:
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

# BDS 1,0/1,7 bit layouts: pyModeS src/pyModeS/decoder/bds/bds10.py and
# bds17.py (github.com/junzis/pyModeS) — see docs/references/ASTERIX.md.
def _cat10__decode_bds10(mb: bytes) -> dict:
    """BDS 1,0 Data Link Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    return {
        "dlc_continuation_flag": bool(_bit(9)),
        "overlay_command_capability": bool(_bit(15)),
        "acas_operational": bool(_bit(16)),
        "mode_s_subnetwork_version": _uns(17, 23),
        "transponder_level5": bool(_bit(24)),
        "mode_s_specific_services": bool(_bit(25)),
        "uplink_elm_throughput": _uns(26, 28),
        "downlink_elm_throughput": _uns(29, 32),
        "aircraft_id_capability": bool(_bit(33)),
        "squitter_capability": bool(_bit(34)),
        "surveillance_id_capability": bool(_bit(35)),
        "common_usage_gicb_capability": bool(_bit(36)),
        "acas_hybrid_surveillance": bool(_bit(37)),
        "acas_resolution_advisory": bool(_bit(38)),
        "acas_rtca_version": _uns(39, 40),
        "dte_status": _uns(41, 56),
    }

_cat10__BDS17_CAPABILITY = (
    "0,5", "0,6", "0,7", "0,8", "0,9", "0,A", "2,0", "2,1",
    "4,0", "4,1", "4,2", "4,3", "4,4", "4,5", "4,8", "5,0",
    "5,1", "5,2", "5,3", "5,4", "5,5", "5,6", "5,F", "6,0",
)

def _cat10__decode_bds17(mb: bytes) -> dict:
    """BDS 1,7 Common Usage GICB Capability Report — which registers this
    transponder actually supports for ground-initiated Comm-B extraction."""
    v = int.from_bytes(mb[:7], "big")
    return {"supported_bds": [code for i, code in enumerate(_cat10__BDS17_CAPABILITY)
                               if (v >> (56 - (i + 1))) & 1]}

def _cat10__polar_to_wgs84(radar_lat: float, radar_lon: float,
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

def _cat10__cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_m: float, y_m: float):
    """Local CAT-010 X=east/Y=north metres to WGS-84 for short airport ranges."""
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon

def _cat10__signed_bits(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value

def _cat10_decode_cat010_record(data: bytes, pos: int, site_lat=None, site_lon=None):
    """Decode exactly the public EUROCONTROL CAT-010 Edition 1.1 UAP."""
    fspec, pos = _cat10_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-10 Ed.1.1"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I010/010 SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I010/000 Message Type
            if pos >= len(data): return track, len(data)
            track["msg_type"] = _cat10__CAT010_MESSAGE_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I010/020 Target Report Descriptor
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["sensor_type"] = _cat10__CAT010_SENSOR_TYPES[(b >> 5) & 0x07]
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
                track["target_type"] = _cat10__CAT010_TARGET_TYPES[target_type]
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
            track["lat_deg"] = round(_cat10__s32(data[pos:pos + 4]) * 180.0 / 2**31, 7)
            track["lon_deg"] = round(_cat10__s32(data[pos + 4:pos + 8]) * 180.0 / 2**31, 7); pos += 8
        elif frn == 5:                  # I010/040 Polar Position
            if pos + 4 > len(data): return track, len(data)
            range_m = _cat10__u16(data[pos:pos + 2]); azimuth = _cat10__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0
            track["range_m"] = range_m; track["azimuth_deg"] = round(azimuth, 3); pos += 4
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat10__polar_to_wgs84(site_lat, site_lon, range_m / 1852.0, azimuth)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 6:                  # I010/042 Cartesian Position
            if pos + 4 > len(data): return track, len(data)
            x_m, y_m = _cat10__s16(data[pos:pos + 2]), _cat10__s16(data[pos + 2:pos + 4]); pos += 4
            track["cart_x_m"], track["cart_y_m"] = x_m, y_m
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat10__cartesian_to_wgs84(site_lat, site_lon, x_m, y_m)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 7:                  # I010/200 Polar Velocity
            if pos + 4 > len(data): return track, len(data)
            track["speed_ms"] = round(_cat10__u16(data[pos:pos + 2]) * 1852.0 / 16384.0, 2)
            track["heading_deg"] = round(_cat10__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0, 2); pos += 4
        elif frn == 8:                  # I010/202 Cartesian Velocity
            if pos + 4 > len(data): return track, len(data)
            vx = _cat10__s16(data[pos:pos + 2]) * 0.25; vy = _cat10__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
            track["velocity_east_ms"] = round(vx, 2); track["velocity_north_ms"] = round(vy, 2)
            track.setdefault("speed_ms", round(math.hypot(vx, vy), 2))
            if vx or vy: track.setdefault("heading_deg", round((math.degrees(math.atan2(vx, vy)) + 360) % 360, 2))
        elif frn == 9:                  # I010/161 Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat10__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
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
            track["squawk"] = "{:04o}".format(_cat10__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 12:                 # I010/220 Target Address
            if pos + 3 > len(data): return track, len(data)
            track["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif frn == 13:                 # I010/245 Target Identification
            if pos + 7 > len(data): return track, len(data)
            callsign = _cat10__decode_callsign(data[pos + 1:pos + 7]); pos += 7
            if callsign: track["callsign"] = callsign
        elif frn == 14:                 # I010/250 Mode-S MB Data
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): return track, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = ((bds >> 4) & 0x0F, bds & 0x0F)
                if code == (1, 0): track.update(_cat10__decode_bds10(mb))
                elif code == (1, 7): track.update(_cat10__decode_bds17(mb))
                elif code == (3, 0): track.update(_cat10__decode_bds30(mb))
                elif code == (4, 0): track.update(_cat10__decode_bds40(mb))
                elif code == (5, 0): track.update(_cat10__decode_bds50(mb))
                elif code == (6, 0): track.update(_cat10__decode_bds60(mb))
        elif frn == 15:                 # I010/300 Vehicle Fleet ID
            if pos >= len(data): return track, len(data)
            track["vehicle_fleet"] = _cat10__CAT010_FLEETS.get(data[pos], "fleet_{}".format(data[pos])); pos += 1
            track.setdefault("target_type", "ground_vehicle"); track["on_ground"] = True
        elif frn == 16:                 # I010/090 Flight Level
            if pos + 2 > len(data): return track, len(data)
            raw = _cat10__u16(data[pos:pos + 2]); pos += 2
            fl = _cat10__signed_bits(raw & 0x3FFF, 14) * 0.25
            track["flight_level"] = round(fl, 2); track["baro_alt_m"] = round(fl * 100 * 0.3048, 2)
        elif frn == 17:                 # I010/091 Measured Height
            if pos + 2 > len(data): return track, len(data)
            feet = _cat10__s16(data[pos:pos + 2]) * 6.25; pos += 2
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
            track["sigma_xy_m2"] = _cat10__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
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
            pos = _cat10__skip_len_field(data, pos)
        else:
            return track, len(data)
    return track, pos

def _cat10__make_cat010_handler(session, site, site_name):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat10_decode_cat010_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if track.get("msg_type") == "target_report":
                if "lat_deg" not in track or "lon_deg" not in track:
                    if verbose:
                        print("cat10 target without map position; configure CAT10_SITE_LAT/LON for local coordinates", flush=True)
                    continue
                ground = track.get("target_type") == "ground_vehicle" or "vehicle_fleet" in track
                topic = (_cat10_TOPIC_010_GROUND if ground else _cat10_TOPIC_010_AIR
                         ).format(source=_asterix_source(track))
                publish_dual(session, topic, track, AsterixCat10Track)
                publish_native(session, native_topic(semantic_topic(topic, track)),
                               asterix_data_block(10, data[previous:pos]),
                               "asterix", profile="cat010")
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
                publish_dual(
                    session,
                    _cat10_TOPIC_010_SENSOR.format(source=_asterix_source(track)),
                    status,
                    AsterixCat10SensorStatus,
                    wrapper_field="sensor",
                )
    return _h

def _cat10__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat10__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat10__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat10__raw_frame_payload(bytes(sample.payload), category)
        except ValueError as exc:
            print("CAT-{} ignored invalid raw Zenoh frame: {}".format(category, exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-{} raw Zenoh input: {}".format(category, input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()

def _cat10__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat10__process_stream(_cat10_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat10__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat10__netbird_ip()
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
                target=_cat10__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat10__process_stream(_cat10_iter_frames_udp(sock), handlers, verbose)



def _cat10_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-010 Ed.1.1 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT10_PORT", "50010") or 50010))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT10_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT10_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT10_INPUT_TOPIC", _cat10_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat10__env_float("CAT10_SITE_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat10__env_float("CAT10_SITE_LON"))
    parser.add_argument("--site-name", default=os.environ.get("CAT10_SITE_NAME", ""))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT10_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = open_session()
    try:
        print("Zenoh CAT-10 topics:", _cat10_TOPIC_010_AIR, _cat10_TOPIC_010_GROUND, _cat10_TOPIC_010_SENSOR, flush=True)
        handler = _cat10__make_cat010_handler(session, site, args.site_name)
        if args.zenoh_raw:
            _cat10__run_zenoh_raw(session, args.input_topic, _cat10_CAT_010, handler, args.verbose)
        else:
            _cat10__run_inbound(args.port, args.tcp, "CAT-10 Ed.1.1", {_cat10_CAT_010: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat010_record = _cat10_decode_cat010_record


# ==========================================================================
# CAT-011 — Transmission of A-SMGCS Data, Ed.1.3
#
# Advanced Surface Movement Guidance and Control System: airport-surface
# system tracks (aircraft + vehicles), fused from multiple surface sensors
# (surface radar, Mode-S MLAT, ADS-B). Close cousin of CAT-010 (also
# airport-surface) but carries the fused *system track* picture with flight
# plan correlation, not raw sensor plots. Single UAP, 29 FRNs, no real
# captured traffic available at implementation time — decoded strictly from
# the public EUROCONTROL cat-1.3.ast DSL (zoranbosnjak/asterix-specs);
# treat as unverified against live traffic until a real feed is available,
# same caveat as every other newly-added category this session.
# ==========================================================================

_cat11_TOPIC_ROOT = topic_root()

_cat11_TOPIC_AIR    = _cat11_TOPIC_ROOT + "/air/{source}/radar/civ/aircraft"
_cat11_TOPIC_GROUND = _cat11_TOPIC_ROOT + "/land/{source}/radar/unknown/vehicle"
_cat11_RAW_INPUT_TOPIC = "{}/raw/asterix/cat11".format(_cat11_TOPIC_ROOT)
_cat11_CAT_011 = 11

_cat11__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat11__MSG_TYPES = {
    1: "target_report", 2: "attach_flight_plan", 3: "detach_flight_plan",
    4: "insert_flight_plan", 5: "suppress_flight_plan",
    6: "modify_flight_plan", 7: "holdbar_status",
}

_cat11__STI = {0: "downlinked", 1: "callsign_not_downlinked", 2: "registration_not_downlinked"}

_cat11__HEIGHT_SOURCE = {
    0: "none", 1: "gps", 2: "3d_radar", 3: "triangulation",
    4: "height_from_coverage", 5: "speed_lookup", 6: "default_height", 7: "multilateration",
}

_cat11__MODE4_FRIFOE = {0: "no_interrogation", 1: "friendly", 2: "unknown", 3: "no_reply"}

_cat11__PHASE_OF_FLIGHT = {
    0: "unknown", 1: "on_stand", 2: "taxiing_for_departure", 3: "taxiing_for_arrival",
    4: "runway_for_departure", 5: "runway_for_arrival", 6: "hold_for_departure",
    7: "hold_for_arrival", 8: "push_back", 9: "on_finals",
}

_cat11__FLEETS = {
    0: "follow_me", 1: "atc_maintenance", 2: "airport_maintenance", 3: "fire",
    4: "bird_scarer", 5: "snow_plough", 6: "runway_sweeper", 7: "emergency",
    8: "police", 9: "bus", 10: "tug", 11: "grass_cutter", 12: "fuel",
    13: "baggage", 14: "catering", 15: "aircraft_maintenance", 16: "unknown",
}

_cat11__PREPROG_MSG = {
    1: "towing_aircraft", 2: "follow_me_operation", 3: "runway_check",
    4: "emergency_operation", 5: "work_in_progress",
}

_cat11__ALERT_SVR = {0: "end_of_alert", 1: "pre_alarm", 2: "severe_alert"}

_cat11__COM = {
    0: "surveillance_only", 1: "comm_a_b", 2: "comm_a_b_uplink_elm",
    3: "comm_a_b_uplink_downlink_elm", 4: "level5_transponder",
}

_cat11__STAT = {
    0: "airborne", 1: "on_ground", 2: "alert_airborne", 3: "alert_on_ground",
    4: "alert_spi", 5: "spi", 6: "general_emergency", 7: "lifeguard_medical",
    8: "minimum_fuel", 9: "no_communications", 10: "unlawful_interference",
}

_cat11__ECAT = {
    1: "light_aircraft", 3: "medium_aircraft", 5: "heavy_aircraft",
    6: "highly_manoeuvrable_high_speed", 10: "rotorcraft", 11: "glider",
    12: "lighter_than_air", 13: "uav", 14: "space_vehicle",
    15: "ultralight", 16: "parachutist", 20: "surface_emergency_vehicle",
    21: "surface_service_vehicle", 22: "fixed_ground_obstruction",
}

_cat11__DAY = {0: "today", 1: "yesterday", 2: "tomorrow"}


def _cat11__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat11__netbird_ip() -> str:
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


def _cat11__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat11_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat11__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat11__recv_exact(sock, length - 3)
        record_in("cat11", len(data))
        yield cat, data


def _cat11_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat11", len(record))
            yield cat, record
            offset += length


def _cat11_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat11__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat11__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat11__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat11__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat11__signed_bits(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value


def _cat11__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat11__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()


def _cat11__decode_bds50(mb: bytes) -> dict:
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
    if _bit(24): out["bds_gs_kt"]        = round(_uns(25, 34) * 2.0, 0)
    if _bit(35): out["track_rate_degs"]  = round(_sgn(36, 45) * 8.0 / 256.0, 2)
    if _bit(46): out["tas_kt"]           = round(_uns(47, 56) * 2.0, 0)
    return out


def _cat11__decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
    if _bit(13): out["ias_kt"]       = _uns(14, 23)
    if _bit(24): out["mach"]         = round(_uns(25, 34) * 2.048 / 512.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 45) * 32
    if _bit(46): out["ivv_fpm"]      = _sgn(47, 56) * 32
    return out


def _cat11__decode_bds40(mb: bytes) -> dict:
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


def _cat11__decode_bds30(mb: bytes) -> dict:
    """BDS 3,0 ACAS Resolution Advisory."""
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


# BDS 1,0/1,7 bit layouts: pyModeS src/pyModeS/decoder/bds/bds10.py and
# bds17.py (github.com/junzis/pyModeS) — see docs/references/ASTERIX.md.
def _cat11__decode_bds10(mb: bytes) -> dict:
    """BDS 1,0 Data Link Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    return {
        "dlc_continuation_flag": bool(_bit(9)),
        "overlay_command_capability": bool(_bit(15)),
        "acas_operational": bool(_bit(16)),
        "mode_s_subnetwork_version": _uns(17, 23),
        "transponder_level5": bool(_bit(24)),
        "mode_s_specific_services": bool(_bit(25)),
        "uplink_elm_throughput": _uns(26, 28),
        "downlink_elm_throughput": _uns(29, 32),
        "aircraft_id_capability": bool(_bit(33)),
        "squitter_capability": bool(_bit(34)),
        "surveillance_id_capability": bool(_bit(35)),
        "common_usage_gicb_capability": bool(_bit(36)),
        "acas_hybrid_surveillance": bool(_bit(37)),
        "acas_resolution_advisory": bool(_bit(38)),
        "acas_rtca_version": _uns(39, 40),
        "dte_status": _uns(41, 56),
    }

_cat11__BDS17_CAPABILITY = (
    "0,5", "0,6", "0,7", "0,8", "0,9", "0,A", "2,0", "2,1",
    "4,0", "4,1", "4,2", "4,3", "4,4", "4,5", "4,8", "5,0",
    "5,1", "5,2", "5,3", "5,4", "5,5", "5,6", "5,F", "6,0",
)

def _cat11__decode_bds17(mb: bytes) -> dict:
    """BDS 1,7 Common Usage GICB Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    return {"supported_bds": [code for i, code in enumerate(_cat11__BDS17_CAPABILITY)
                               if (v >> (56 - (i + 1))) & 1]}


def _cat11__cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_m: float, y_m: float):
    """Local CAT-011 X=east/Y=north metres to WGS-84 for short airport ranges."""
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon


def _cat11__presence(data: bytes, pos: int, maximum: int) -> tuple[list[bool], int]:
    present: list[bool] = []
    for _ in range(maximum):
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 1:
            return present, pos
    if data[pos - 1] & 1:
        raise ValueError("unsupported ASTERIX compound presence extension")
    return present, pos


def _cat11__decode_170(data: bytes, pos: int) -> tuple[dict, int]:
    """I011/170 Track Status: FX-chained, up to 4 octets."""
    out: dict = {}
    if pos >= len(data): return out, len(data)
    b = data[pos]; pos += 1
    out["monosensor"]  = bool(b & 0x80)
    if b & 0x40: out["ground_bit"] = True
    if b & 0x20: out["geometric_alt_more_reliable"] = True
    out["height_source"] = _cat11__HEIGHT_SOURCE.get((b >> 2) & 0x07, "unknown")
    out["confirmed"] = not bool(b & 0x02)
    if not (b & 0x01): return out, pos
    if pos >= len(data): return out, pos
    b = data[pos]; pos += 1
    if b & 0x80: out["simulated"] = True
    if b & 0x40: out["track_service_end"] = True
    if b & 0x20: out["track_service_begin"] = True
    out["mode4_friend_foe"] = _cat11__MODE4_FRIFOE.get((b >> 3) & 0x03, "unknown")
    if b & 0x04: out["military_emergency"] = True
    if b & 0x02: out["military_identification"] = True
    if not (b & 0x01): return out, pos
    if pos >= len(data): return out, pos
    b = data[pos]; pos += 1
    if b & 0x80: out["amalgamated"] = True
    if b & 0x40: out["spi"] = True
    if b & 0x20: out["coasting"] = True
    out["flight_plan_correlated"] = bool(b & 0x10)
    if b & 0x08: out["adsb_inconsistent"] = True
    if not (b & 0x01): return out, pos
    if pos >= len(data): return out, pos
    b = data[pos]; pos += 1
    if b & 0x40: out["psr_update_stale"] = True
    if b & 0x20: out["ssr_update_stale"] = True
    if b & 0x10: out["mode_s_update_stale"] = True
    if b & 0x08: out["ads_update_stale"] = True
    if b & 0x04: out["special_used_code"] = True
    if b & 0x02: out["mode_a_conflict"] = True
    return out, pos


def _cat11__decode_270(data: bytes, pos: int) -> tuple[dict, int]:
    """I011/270 Target Size and Orientation: FX-chained, up to 3 octets."""
    out: dict = {}
    if pos >= len(data): return out, pos
    b = data[pos]; pos += 1
    out["target_length_m"] = b >> 1
    if not (b & 0x01): return out, pos
    if pos >= len(data): return out, pos
    b = data[pos]; pos += 1
    out["orientation_deg"] = round((b >> 1) * 360.0 / 128.0, 2)
    if not (b & 0x01): return out, pos
    if pos >= len(data): return out, pos
    b = data[pos]; pos += 1
    out["target_width_m"] = b >> 1
    return out, pos


def _cat11__decode_290(data: bytes, pos: int) -> tuple[dict, int]:
    """I011/290 System Track Update Ages: compound, up to 2 presence bytes, LSB 0.25s."""
    out: dict = {}
    flags, pos = _cat11__presence(data, pos, 2)
    names = ("psr", "ssr", "mda", "mfl", "mds", "ads", "adb", "md1", "md2", "lop", "trk", "mul")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        size = 2 if name == "ads" else 1
        if pos + size > len(data): return out, len(data)
        out["track_age_{}_s".format(name)] = round(int.from_bytes(data[pos:pos + size], "big") * 0.25, 2)
        pos += size
    return out, pos


def _cat11__decode_380(data: bytes, pos: int) -> tuple[dict, int]:
    """I011/380 Mode-S / ADS-B Related Data: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat11__presence(data, pos, 1)
    names = ("mb", "adr", "comacas", "act", "ecat", "avtech")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "mb":
            if pos >= len(data): return out, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): return out, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = ((bds >> 4) & 0x0F, bds & 0x0F)
                if code == (1, 0): out.update(_cat11__decode_bds10(mb))
                elif code == (1, 7): out.update(_cat11__decode_bds17(mb))
                elif code == (3, 0): out.update(_cat11__decode_bds30(mb))
                elif code == (4, 0): out.update(_cat11__decode_bds40(mb))
                elif code == (5, 0): out.update(_cat11__decode_bds50(mb))
                elif code == (6, 0): out.update(_cat11__decode_bds60(mb))
        elif name == "adr":
            if pos + 3 > len(data): return out, len(data)
            out["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif name == "comacas":
            if pos + 3 > len(data): return out, len(data)
            b1, b2, b3 = data[pos], data[pos + 1], data[pos + 2]; pos += 3
            out["com"] = _cat11__COM.get((b1 >> 5) & 0x07, "unknown")
            out["flight_status"] = _cat11__STAT.get((b1 >> 1) & 0x0F, "unknown")
            out["ssc"] = bool(b2 & 0x80); out["altitude_reporting"] = bool(b2 & 0x40)
            out["aircraft_id_capability"] = bool(b2 & 0x20)
            out["bds10_bit_array_available"] = bool(b2 & 0x10)
            out["bds10_bit_array_raw"] = b2 & 0x0F
            out["altitude_capability"] = bool(b3 & 0x80)
            out["mode_s_specific_service"] = bool(b3 & 0x40)
            out["dte_sub_addressing"] = bool(b3 & 0x20)
        elif name == "act":
            if pos + 4 > len(data): return out, len(data)
            act = data[pos:pos + 4].decode("ascii", "replace").strip("\x00 "); pos += 4
            if act: out["aircraft_type"] = act
        elif name == "ecat":
            if pos >= len(data): return out, len(data)
            out["emitter_category"] = _cat11__ECAT.get(data[pos], "cat_{}".format(data[pos])); pos += 1
        elif name == "avtech":
            if pos >= len(data): return out, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: out["vdl_mode4"] = True
            if b & 0x40: out["mode_s_datalink"] = True
            if b & 0x20: out["uat_datalink"] = True
    return out, pos


def _cat11__decode_390(data: bytes, pos: int) -> tuple[dict, int]:
    """I011/390 Flight Plan Related Data: compound, up to 2 presence bytes.

    IFPSFLIGHTID/FLIGHTCAT/STS sub-bit tables are FPL-office administrative
    codes not needed for track/position use and are kept as raw ints rather
    than guessed enum names (their tables weren't independently confirmed).
    """
    out: dict = {}
    flags, pos = _cat11__presence(data, pos, 2)
    names = ("fppsid", "csn", "ifpsflightid", "flightcat", "toa", "wtc", "adep",
              "ades", "rwy", "cfl", "ccp", "tod", "ast", "sts")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "fppsid":
            if pos + 2 > len(data): return out, len(data)
            out["fpps_sac"], out["fpps_sic"] = data[pos], data[pos + 1]; pos += 2
        elif name == "csn":
            if pos + 7 > len(data): return out, len(data)
            csn = data[pos:pos + 7].decode("ascii", "replace").strip("\x00 "); pos += 7
            if csn: out["callsign_fpl"] = csn
        elif name == "ifpsflightid":
            if pos + 4 > len(data): return out, len(data)
            v = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
            out["ifps_id_type_raw"] = (v >> 27) & 0x03
            out["ifps_id_number"] = v & 0x07FFFFFF
        elif name == "flightcat":
            if pos >= len(data): return out, len(data)
            b = data[pos]; pos += 1
            out["gat_oat_raw"] = (b >> 6) & 0x03
            out["fr1_fr2_raw"] = (b >> 4) & 0x03
            out["rvsm_raw"] = (b >> 2) & 0x03
            if b & 0x02: out["hpr"] = True
        elif name == "toa":
            if pos + 4 > len(data): return out, len(data)
            s = data[pos:pos + 4].decode("ascii", "replace").strip("\x00 "); pos += 4
            if s: out["time_of_arrival"] = s
        elif name == "wtc":
            if pos >= len(data): return out, len(data)
            out["wake_turbulence_cat"] = chr(data[pos]) if 32 <= data[pos] < 127 else ""; pos += 1
        elif name == "adep":
            if pos + 4 > len(data): return out, len(data)
            s = data[pos:pos + 4].decode("ascii", "replace").strip("\x00 "); pos += 4
            if s: out["departure_airport"] = s
        elif name == "ades":
            if pos + 4 > len(data): return out, len(data)
            s = data[pos:pos + 4].decode("ascii", "replace").strip("\x00 "); pos += 4
            if s: out["destination_airport"] = s
        elif name == "rwy":
            if pos + 3 > len(data): return out, len(data)
            s = data[pos:pos + 3].decode("ascii", "replace").strip("\x00 "); pos += 3
            if s: out["runway"] = s
        elif name == "cfl":
            if pos + 2 > len(data): return out, len(data)
            out["cleared_flight_level"] = round(_cat11__u16(data[pos:pos + 2]) * 0.25, 2); pos += 2
        elif name == "ccp":
            if pos + 2 > len(data): return out, len(data)
            out["control_centre"], out["control_position"] = data[pos], data[pos + 1]; pos += 2
        elif name == "tod":
            if pos >= len(data): return out, len(data)
            rep = data[pos]; pos += 1
            entries = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                v = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
                entries.append({
                    "type_raw": (v >> 27) & 0x1F,
                    "day": _cat11__DAY.get((v >> 25) & 0x03, "unknown"),
                    "hour": (v >> 16) & 0x1F,
                    "minute": (v >> 8) & 0x3F,
                    "seconds_available": not bool((v >> 7) & 0x01),
                    "second": v & 0x3F,
                })
            if entries: out["times_of_day"] = entries
        elif name == "ast":
            if pos + 6 > len(data): return out, len(data)
            s = data[pos:pos + 6].decode("ascii", "replace").strip("\x00 "); pos += 6
            if s: out["aircraft_stand"] = s
        elif name == "sts":
            if pos >= len(data): return out, len(data)
            b = data[pos]; pos += 1
            out["emp_raw"] = (b >> 6) & 0x03
            out["avl_raw"] = (b >> 4) & 0x03
    return out, pos


def _cat11__decode_500(data: bytes, pos: int) -> tuple[dict, int]:
    """I011/500 Estimated Accuracies: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat11__presence(data, pos, 1)
    names = ("apc", "apw", "ath", "avc", "arc", "aac")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "apc":
            if pos + 2 > len(data): return out, len(data)
            out["pos_accuracy_x_m"] = data[pos] * 0.25; out["pos_accuracy_y_m"] = data[pos + 1] * 0.25; pos += 2
        elif name == "apw":
            if pos + 4 > len(data): return out, len(data)
            scale = 180.0 / 2**31
            out["pos_accuracy_lat_deg"] = round(_cat11__s16(data[pos:pos + 2]) * scale, 9)
            out["pos_accuracy_lon_deg"] = round(_cat11__s16(data[pos + 2:pos + 4]) * scale, 9); pos += 4
        elif name == "ath":
            if pos + 2 > len(data): return out, len(data)
            out["height_accuracy_m"] = _cat11__s16(data[pos:pos + 2]) * 0.5; pos += 2
        elif name == "avc":
            if pos + 2 > len(data): return out, len(data)
            out["vel_accuracy_x_ms"] = data[pos] * 0.1; out["vel_accuracy_y_ms"] = data[pos + 1] * 0.1; pos += 2
        elif name == "arc":
            if pos + 2 > len(data): return out, len(data)
            out["rate_accuracy_ms"] = _cat11__s16(data[pos:pos + 2]) * 0.1; pos += 2
        elif name == "aac":
            if pos + 2 > len(data): return out, len(data)
            out["accel_accuracy_x_ms2"] = data[pos] * 0.01; out["accel_accuracy_y_ms2"] = data[pos + 1] * 0.01; pos += 2
    return out, pos


def _cat11_decode_cat011_record(data: bytes, pos: int, site_lat=None, site_lon=None):
    """Decode exactly the public EUROCONTROL CAT-011 Edition 1.3 UAP."""
    fspec, pos = _cat11_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-11 Ed.1.3"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        try:
            if frn == 0:                    # I011/010 SAC/SIC
                if pos + 2 > len(data): return track, len(data)
                track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
            elif frn == 1:                  # I011/000 Message Type
                if pos >= len(data): return track, len(data)
                track["msg_type"] = _cat11__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
            elif frn == 2:                  # I011/015 Service Identification
                if pos >= len(data): return track, len(data)
                track["service_id"] = data[pos]; pos += 1
            elif frn == 3:                  # I011/140 Time of Track Information
                if pos + 3 > len(data): return track, len(data)
                track["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
            elif frn == 4:                  # I011/041 WGS-84 Position
                if pos + 8 > len(data): return track, len(data)
                scale = 180.0 / 2**31
                track["lat_deg"] = round(_cat11__s32(data[pos:pos + 4]) * scale, 7)
                track["lon_deg"] = round(_cat11__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
            elif frn == 5:                  # I011/042 Calculated Cartesian Position
                if pos + 4 > len(data): return track, len(data)
                x_m, y_m = _cat11__s16(data[pos:pos + 2]), _cat11__s16(data[pos + 2:pos + 4]); pos += 4
                track["cart_x_m"], track["cart_y_m"] = x_m, y_m
                if site_lat is not None and site_lon is not None:
                    lat, lon = _cat11__cartesian_to_wgs84(site_lat, site_lon, x_m, y_m)
                    track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
            elif frn == 6:                  # I011/202 Calculated Cartesian Velocity
                if pos + 4 > len(data): return track, len(data)
                vx = _cat11__s16(data[pos:pos + 2]) * 0.25; vy = _cat11__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
                track["velocity_east_ms"] = round(vx, 2); track["velocity_north_ms"] = round(vy, 2)
                track["speed_ms"] = round(math.hypot(vx, vy), 2)
                if vx or vy: track["heading_deg"] = round((math.degrees(math.atan2(vx, vy)) + 360) % 360, 2)
            elif frn == 7:                  # I011/210 Calculated Acceleration
                if pos + 2 > len(data): return track, len(data)
                track["accel_east_ms2"] = struct.unpack_from("b", data, pos)[0] * 0.25
                track["accel_north_ms2"] = struct.unpack_from("b", data, pos + 1)[0] * 0.25; pos += 2
            elif frn == 8:                  # I011/060 Mode-3/A
                if pos + 2 > len(data): return track, len(data)
                track["squawk"] = "{:04o}".format(_cat11__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
            elif frn == 9:                  # I011/245 Target Identification
                if pos + 7 > len(data): return track, len(data)
                track["sti"] = _cat11__STI.get((data[pos] >> 6) & 0x03, "unknown")
                callsign = _cat11__decode_callsign(data[pos + 1:pos + 7]); pos += 7
                if callsign: track["callsign"] = callsign
            elif frn == 10:                 # I011/380 Mode-S / ADS-B Related Data
                extra, pos = _cat11__decode_380(data, pos); track.update(extra)
            elif frn == 11:                 # I011/161 Track Number
                if pos + 2 > len(data): return track, len(data)
                track["track_num"] = _cat11__u16(data[pos:pos + 2]) & 0x7FFF; pos += 2
            elif frn == 12:                 # I011/170 Track Status
                extra, pos = _cat11__decode_170(data, pos); track.update(extra)
            elif frn == 13:                 # I011/290 System Track Update Ages
                extra, pos = _cat11__decode_290(data, pos); track.update(extra)
            elif frn == 14:                 # I011/430 Phase of Flight
                if pos >= len(data): return track, len(data)
                track["phase_of_flight"] = _cat11__PHASE_OF_FLIGHT.get(data[pos], "unknown"); pos += 1
            elif frn == 15:                 # I011/090 Measured Flight Level
                if pos + 2 > len(data): return track, len(data)
                track["flight_level"] = round(_cat11__s16(data[pos:pos + 2]) * 0.25, 2); pos += 2
            elif frn == 16:                 # I011/093 Calculated Track Barometric Altitude
                if pos + 2 > len(data): return track, len(data)
                raw = _cat11__u16(data[pos:pos + 2]); pos += 2
                if raw & 0x8000: track["qnh_correction"] = True
                track["baro_flight_level"] = round(_cat11__signed_bits(raw & 0x7FFF, 15) * 0.25, 2)
            elif frn == 17:                 # I011/092 Calculated Track Geometric Altitude
                if pos + 2 > len(data): return track, len(data)
                track["geo_alt_ft"] = round(_cat11__s16(data[pos:pos + 2]) * 6.25, 2); pos += 2
            elif frn == 18:                 # I011/215 Calculated Rate of Climb/Descent
                if pos + 2 > len(data): return track, len(data)
                track["climb_rate_fpm"] = round(_cat11__s16(data[pos:pos + 2]) * 6.25, 2); pos += 2
            elif frn == 19:                 # I011/270 Target Size and Orientation
                extra, pos = _cat11__decode_270(data, pos); track.update(extra)
            elif frn == 20:                 # I011/390 Flight Plan Related Data
                extra, pos = _cat11__decode_390(data, pos); track.update(extra)
            elif frn == 21:                 # I011/300 Vehicle Fleet Identification
                if pos >= len(data): return track, len(data)
                track["vehicle_fleet"] = _cat11__FLEETS.get(data[pos], "fleet_{}".format(data[pos])); pos += 1
                track.setdefault("on_ground", True)
            elif frn == 22:                 # I011/310 Pre-programmed Message
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["in_trouble"] = True
                track["preprogrammed_message"] = _cat11__PREPROG_MSG.get(b & 0x7F, "msg_{}".format(b & 0x7F))
            elif frn == 23:                 # I011/500 Estimated Accuracies
                extra, pos = _cat11__decode_500(data, pos); track.update(extra)
            elif frn == 24:                 # I011/600 Alert Messages (3 bytes: ACK+SVR+spare, AT, AN)
                if pos + 3 > len(data): return track, len(data)
                b1, at, an = data[pos], data[pos + 1], data[pos + 2]; pos += 3
                track["alert_acknowledged"] = not bool(b1 & 0x80)
                track["alert_severity"] = _cat11__ALERT_SVR.get((b1 >> 5) & 0x03, "unknown")
                track["alert_type"] = at
                track["alert_number"] = an
            elif frn == 25:                 # I011/605 Tracks in Alert
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                tracks_in_alert = []
                for _ in range(rep):
                    if pos + 2 > len(data): break
                    tracks_in_alert.append(_cat11__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
                if tracks_in_alert: track["tracks_in_alert"] = tracks_in_alert
            elif frn == 26:                 # I011/610 Holdbar Status
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                holdbars = []
                for _ in range(rep):
                    if pos + 2 > len(data): break
                    v = _cat11__u16(data[pos:pos + 2]); pos += 2
                    holdbars.append({"bank": (v >> 12) & 0x0F, "indicators_raw": v & 0x0FFF})
                if holdbars: track["holdbar_status"] = holdbars
            elif frn in (27, 28):           # I011/SP, I011/RE
                pos = _cat11__skip_len_field(data, pos)
            else:
                return track, len(data)
        except ValueError:
            return track, len(data)
    return track, pos


def _cat11__make_handler(session, site, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat11_decode_cat011_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if "lat_deg" not in track or "lon_deg" not in track:
                if verbose:
                    print("cat11 record without map position; configure CAT11_SITE_LAT/LON for local coordinates", flush=True)
                continue
            ground = "vehicle_fleet" in track
            topic = (_cat11_TOPIC_GROUND if ground else _cat11_TOPIC_AIR).format(source=_asterix_source(track))
            publish_dual(session, topic, track, AsterixCat11Track)
            publish_native(session, native_topic(semantic_topic(topic, track)),
                           asterix_data_block(11, data[previous:pos]),
                           "asterix", profile="cat011")
            if verbose:
                print("cat11 {} -> {}".format(track.get("track_num", "target"), topic), flush=True)
    return _h


def _cat11__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat11_CAT_011:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat11__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat11__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat11__raw_frame_payload(bytes(sample.payload), _cat11_CAT_011)
        except ValueError as exc:
            print("CAT-11 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-11 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat11__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat11__process_stream(_cat11_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-11 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-11 TCP disconnected: {}".format(addr), flush=True)


def _cat11__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat11__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-11 Ed.1.3 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-11 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat11__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-11 Ed.1.3 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat11__process_stream(_cat11_iter_frames_udp(sock), handler, verbose)


def _cat11_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-011 Ed.1.3 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT11_PORT", "50011") or 50011))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT11_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT11_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT11_INPUT_TOPIC", _cat11_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat11__env_float("CAT11_SITE_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat11__env_float("CAT11_SITE_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT11_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = open_session()
    handler = _cat11__make_handler(session, site, args.verbose)
    try:
        print("Zenoh CAT-11 topics:", _cat11_TOPIC_AIR, _cat11_TOPIC_GROUND, flush=True)
        if args.zenoh_raw: _cat11__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat11__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-015 — Independent Non-Cooperative Surveillance System Target Reports,
# Ed.1.2
#
# INCS: passive/multi-static "non-cooperative" surveillance (e.g. passive
# coherent location off illuminators of opportunity) — a genuine radar-like
# track/plot for targets that carry no transponder. Single UAP, 26 FRNs.
# Unlike every other category this session, the full spec text (not just an
# AI-summarized fetch) was pulled directly into this repo and read verbatim
# before implementing — see docs/references/asterix-specs/cat015/cat-1.2.ast
# and docs/references/ASTERIX.md. No real captured traffic to validate
# against; same "unverified against live feed" caveat as every other
# category added this session.
#
# Position can arrive two ways: I015/600's P84 (WGS-84 lat/lon, itself
# presence-gated INSIDE the compound — it can be absent even when FRN13 is
# present), or sensor-centric polar via I015/625 Range + I015/627 Azimuth,
# georeferenced with --site-lat/--site-lon like CAT-001/007/010/011.
#
# I015/601 through I015/628 are compound "quality" items carrying dozens of
# correlation/precision coefficients (rms errors, cross-correlations between
# position/velocity/acceleration estimates). All of them are decoded fully
# here — the spec text was unambiguous for every field, unlike the RIM/scale
# items in other categories that had to be kept raw.
# ==========================================================================

_cat15_TOPIC_ROOT = topic_root()

_cat15_TOPIC_AIR      = _cat15_TOPIC_ROOT + "/air/{source}/radar/unknown/aircraft"
_cat15_RAW_INPUT_TOPIC = "{}/raw/asterix/cat15".format(_cat15_TOPIC_ROOT)
_cat15_CAT_015 = 15

_cat15__MSG_TYPES = {
    1: "measurement_plot", 2: "measurement_track",
    3: "sensor_centric_plot", 4: "sensor_centric_track",
    5: "track_end",
}
_cat15__MOMU = {0: "mono_static", 1: "multi_static", 2: "other", 3: "unknown"}
_cat15__TTAX = {0: "actual", 1: "reference", 2: "synthetic", 3: "simulated_replayed"}
_cat15__SCD = {0: "unknown", 1: "forward", 2: "backward", 3: "static"}


def _cat15__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat15__netbird_ip() -> str:
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


def _cat15__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat15_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat15__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat15__recv_exact(sock, length - 3)
        record_in("cat15", len(data))
        yield cat, data


def _cat15_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat15", len(record))
            yield cat, record
            offset += length


def _cat15_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat15__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat15__s8(b: int) -> int: return b - 256 if b & 0x80 else b

def _cat15__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat15__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat15__s24(b: bytes) -> int:
    v = int.from_bytes(b, "big")
    return v - (1 << 24) if v & (1 << 23) else v

def _cat15__u24(b: bytes) -> int: return int.from_bytes(b, "big")

def _cat15__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat15__signed_bits(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value


def _cat15__polar_to_wgs84(radar_lat: float, radar_lon: float,
                            range_m: float, azimuth_deg: float):
    """Haversine forward: sensor-centric range/azimuth -> WGS-84 lat/lon."""
    d    = range_m
    R    = 6_371_000.0
    lat1 = math.radians(radar_lat)
    lon1 = math.radians(radar_lon)
    az   = math.radians(azimuth_deg)
    lat2 = math.asin(math.sin(lat1) * math.cos(d / R) +
                     math.cos(lat1) * math.sin(d / R) * math.cos(az))
    lon2 = lon1 + math.atan2(math.sin(az) * math.sin(d / R) * math.cos(lat1),
                              math.cos(d / R) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def _cat15__presence(data: bytes, pos: int, maximum: int) -> tuple[list[bool], int]:
    present: list[bool] = []
    for _ in range(maximum):
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 1:
            return present, pos
    if data[pos - 1] & 1:
        raise ValueError("unsupported ASTERIX compound presence extension")
    return present, pos


def _cat15__decode_270(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/270 Target Size and Orientation: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("len", "wdt", "hgt", "ort")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if pos + 2 > len(data): return out, len(data)
        raw = _cat15__u16(data[pos:pos + 2]); pos += 2
        if name == "ort":
            out["orientation_deg"] = round(raw * 360.0 / 65536.0, 3)
        else:
            out["target_{}_m".format({"len": "length", "wdt": "width", "hgt": "height"}[name])] = round(raw * 0.01, 2)
    return out, pos


def _cat15__decode_600(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/600 Horizontal Position Information: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("p84", "hpr", "hpp")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "p84":
            if pos + 8 > len(data): return out, len(data)
            scale = 180.0 / 2**31
            out["lat_deg"] = round(_cat15__s32(data[pos:pos + 4]) * scale, 7)
            out["lon_deg"] = round(_cat15__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
        elif name == "hpr":
            if pos + 5 > len(data): return out, len(data)
            out["hpos_resolution_x_m"] = round(_cat15__u16(data[pos:pos + 2]) * 0.5, 2)
            out["hpos_resolution_y_m"] = round(_cat15__u16(data[pos + 2:pos + 4]) * 0.5, 2)
            out["hpos_resolution_corr_xy"] = round(_cat15__s8(data[pos + 4]) / 128.0, 4); pos += 5
        elif name == "hpp":
            if pos + 5 > len(data): return out, len(data)
            out["hpos_precision_x_m"] = round(_cat15__u16(data[pos:pos + 2]) * 0.25, 2)
            out["hpos_precision_y_m"] = round(_cat15__u16(data[pos + 2:pos + 4]) * 0.25, 2)
            out["hpos_precision_corr_xy"] = round(_cat15__s8(data[pos + 4]) / 128.0, 4); pos += 5
    return out, pos


def _cat15__decode_601(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/601 Geometric Height Information: compound, up to 2 presence bytes."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 2)
    names = ("gh", "rsgh", "sdgh", "ci6", "ci9", "coghhp", "coghhv", "coghha")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "gh":
            if pos + 3 > len(data): return out, len(data)
            out["geo_height_m"] = round(_cat15__s24(data[pos:pos + 3]) * 0.01, 2); pos += 3
        elif name == "rsgh":
            if pos + 3 > len(data): return out, len(data)
            out["geo_height_resolution_m"] = round(_cat15__u24(data[pos:pos + 3]) * 0.01, 2); pos += 3
        elif name == "sdgh":
            if pos + 3 > len(data): return out, len(data)
            out["geo_height_precision_m"] = round(_cat15__u24(data[pos:pos + 3]) * 0.01, 2); pos += 3
        elif name in ("ci6", "ci9"):
            if pos + 3 > len(data): return out, len(data)
            v = _cat15__u24(data[pos:pos + 3]); pos += 3
            out["geo_height_ci_{}_upper_m".format(name[2])] = (v >> 12) * 16
            out["geo_height_ci_{}_lower_m".format(name[2])] = (v & 0xFFF) * 16
        elif name in ("coghhp", "coghhv", "coghha"):
            if pos + 2 > len(data): return out, len(data)
            suffix = name[4:]
            out["corr_geo_height_{}_x".format(suffix)] = round(_cat15__s8(data[pos]) / 128.0, 4)
            out["corr_geo_height_{}_y".format(suffix)] = round(_cat15__s8(data[pos + 1]) / 128.0, 4); pos += 2
    return out, pos


def _cat15__decode_602(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/602 Horizontal Velocity Information: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("hv", "rshv", "sdhv", "cohvhp")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "hv":
            if pos + 5 > len(data): return out, len(data)
            v = int.from_bytes(data[pos:pos + 5], "big"); pos += 5
            out["hvel_x_ms"] = round(_cat15__signed_bits((v >> 20) & 0xFFFFF, 20) * 0.01, 2)
            out["hvel_y_ms"] = round(_cat15__signed_bits(v & 0xFFFFF, 20) * 0.01, 2)
        elif name == "rshv":
            if pos + 5 > len(data): return out, len(data)
            out["hvel_resolution_x_ms"] = round(_cat15__u16(data[pos:pos + 2]) * 0.01, 2)
            out["hvel_resolution_y_ms"] = round(_cat15__u16(data[pos + 2:pos + 4]) * 0.01, 2)
            out["hvel_resolution_corr_xy"] = round(_cat15__s8(data[pos + 4]) / 128.0, 4); pos += 5
        elif name == "sdhv":
            if pos + 5 > len(data): return out, len(data)
            out["hvel_precision_x_ms"] = round(_cat15__u16(data[pos:pos + 2]) * 0.01, 2)
            out["hvel_precision_y_ms"] = round(_cat15__u16(data[pos + 2:pos + 4]) * 0.01, 2)
            out["hvel_precision_corr_xy"] = round(_cat15__s8(data[pos + 4]) / 128.0, 4); pos += 5
        elif name == "cohvhp":
            if pos + 4 > len(data): return out, len(data)
            out["corr_hvel_x_hpos_x"] = round(_cat15__s8(data[pos]) / 128.0, 4)
            out["corr_hvel_x_hpos_y"] = round(_cat15__s8(data[pos + 1]) / 128.0, 4)
            out["corr_hvel_y_hpos_x"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4)
            out["corr_hvel_y_hpos_y"] = round(_cat15__s8(data[pos + 3]) / 128.0, 4); pos += 4
    return out, pos


def _cat15__decode_603(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/603 Horizontal Acceleration Information: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("ha", "sdha", "cohahp", "cohahv")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "ha":
            if pos + 3 > len(data): return out, len(data)
            v = _cat15__u24(data[pos:pos + 3]); pos += 3
            out["haccel_x_ms2"] = round(_cat15__signed_bits((v >> 12) & 0xFFF, 12) / 16.0, 3)
            out["haccel_y_ms2"] = round(_cat15__signed_bits(v & 0xFFF, 12) / 16.0, 3)
        elif name == "sdha":
            if pos + 4 > len(data): return out, len(data)
            v = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
            out["haccel_precision_x_ms2"] = round(((v >> 20) & 0xFFF) / 16.0, 3)
            out["haccel_precision_y_ms2"] = round(((v >> 8) & 0xFFF) / 16.0, 3)
            out["haccel_precision_corr_xy"] = round(_cat15__s8(v & 0xFF) / 128.0, 4)
        elif name == "cohahp":
            if pos + 4 > len(data): return out, len(data)
            out["corr_haccel_x_hpos_x"] = round(_cat15__s8(data[pos]) / 128.0, 4)
            out["corr_haccel_x_hpos_y"] = round(_cat15__s8(data[pos + 1]) / 128.0, 4)
            out["corr_haccel_y_hpos_x"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4)
            out["corr_haccel_y_hpos_y"] = round(_cat15__s8(data[pos + 3]) / 128.0, 4); pos += 4
        elif name == "cohahv":
            if pos + 4 > len(data): return out, len(data)
            out["corr_haccel_x_hvel_x"] = round(_cat15__s8(data[pos]) / 128.0, 4)
            out["corr_haccel_x_hvel_y"] = round(_cat15__s8(data[pos + 1]) / 128.0, 4)
            out["corr_haccel_y_hvel_x"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4)
            out["corr_haccel_y_hvel_y"] = round(_cat15__s8(data[pos + 3]) / 128.0, 4); pos += 4
    return out, pos


def _cat15__decode_604(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/604 Vertical Velocity Information: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("vv", "rsvv", "sdvv", "covvhp", "covvhv", "covvha")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "vv":
            if pos + 3 > len(data): return out, len(data)
            out["vvel_ms"] = round(_cat15__s24(data[pos:pos + 3]) * 0.01, 2); pos += 3
        elif name == "rsvv":
            if pos + 2 > len(data): return out, len(data)
            out["vvel_resolution_ms"] = round(_cat15__u16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif name == "sdvv":
            if pos + 3 > len(data): return out, len(data)
            out["vvel_precision_ms"] = round(_cat15__u16(data[pos:pos + 2]) * 0.01, 2)
            out["corr_vvel_geo_height"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4); pos += 3
        elif name in ("covvhp", "covvhv", "covvha"):
            if pos + 2 > len(data): return out, len(data)
            suffix = name[4:]
            out["corr_vvel_{}_x".format(suffix)] = round(_cat15__s8(data[pos]) / 128.0, 4)
            out["corr_vvel_{}_y".format(suffix)] = round(_cat15__s8(data[pos + 1]) / 128.0, 4); pos += 2
    return out, pos


def _cat15__decode_605(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/605 Vertical Acceleration Information (spec's own title mislabels
    this "Vertical Velocity Information" a second time — content and UAP
    position confirm it is acceleration): compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("va", "rsva", "covahp", "covahv", "covaha")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "va":
            if pos + 2 > len(data): return out, len(data)
            out["vaccel_ms2"] = round(_cat15__s16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif name == "rsva":
            if pos + 4 > len(data): return out, len(data)
            out["vaccel_precision_ms2"] = round(_cat15__u16(data[pos:pos + 2]) * 0.01, 2)
            out["corr_vaccel_geo_height"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4)
            out["corr_vaccel_vvel"] = round(_cat15__s8(data[pos + 3]) / 128.0, 4); pos += 4
        elif name in ("covahp", "covahv", "covaha"):
            if pos + 2 > len(data): return out, len(data)
            suffix = name[4:]
            out["corr_vaccel_{}_x".format(suffix)] = round(_cat15__s8(data[pos]) / 128.0, 4)
            out["corr_vaccel_{}_y".format(suffix)] = round(_cat15__s8(data[pos + 1]) / 128.0, 4); pos += 2
    return out, pos


def _cat15__decode_625(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/625 Range Information: compound, up to 2 presence bytes."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 2)
    names = ("r", "rsr", "sdr", "rr", "rsrr", "sdrr", "ra", "sdra")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "r":
            if pos + 3 > len(data): return out, len(data)
            out["range_m"] = round(_cat15__s24(data[pos:pos + 3]) * 0.1, 1); pos += 3
        elif name == "rsr":
            if pos + 3 > len(data): return out, len(data)
            out["range_resolution_m"] = round(_cat15__u24(data[pos:pos + 3]) * 0.1, 1); pos += 3
        elif name == "sdr":
            if pos + 3 > len(data): return out, len(data)
            out["range_precision_m"] = round(_cat15__u24(data[pos:pos + 3]) * 0.1, 1); pos += 3
        elif name == "rr":
            if pos + 3 > len(data): return out, len(data)
            out["range_rate_ms"] = round(_cat15__s24(data[pos:pos + 3]) * 0.1, 2); pos += 3
        elif name == "rsrr":
            if pos + 3 > len(data): return out, len(data)
            out["range_rate_resolution_ms"] = round(_cat15__u24(data[pos:pos + 3]) * 0.1, 2); pos += 3
        elif name == "sdrr":
            if pos + 4 > len(data): return out, len(data)
            out["range_rate_precision_ms"] = round(_cat15__u24(data[pos:pos + 3]) * 0.1, 2)
            out["corr_range_rate_range"] = round(_cat15__s8(data[pos + 3]) / 128.0, 4); pos += 4
        elif name == "ra":
            if pos + 2 > len(data): return out, len(data)
            out["range_accel_ms2"] = round(_cat15__s16(data[pos:pos + 2]) / 64.0, 3); pos += 2
        elif name == "sdra":
            if pos + 4 > len(data): return out, len(data)
            out["range_accel_precision_ms2"] = round(_cat15__u16(data[pos:pos + 2]) / 128.0, 4)
            out["corr_range_accel_range"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4)
            out["corr_range_accel_range_rate"] = round(_cat15__s8(data[pos + 3]) / 128.0, 4); pos += 4
    return out, pos


def _cat15__decode_626(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/626 Doppler Information: compound, up to 2 presence bytes."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 2)
    names = ("dv", "sddv", "da", "sdda", "codvr", "codvrr", "codvra", "codar", "codarr", "codara")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "dv":
            if pos + 3 > len(data): return out, len(data)
            out["doppler_vel_ms"] = round(_cat15__s24(data[pos:pos + 3]) * 0.01, 2); pos += 3
        elif name == "sddv":
            if pos + 2 > len(data): return out, len(data)
            out["doppler_vel_precision_ms"] = round(_cat15__u16(data[pos:pos + 2]) / 64.0, 3); pos += 2
        elif name == "da":
            if pos + 2 > len(data): return out, len(data)
            out["doppler_accel_ms2"] = round(_cat15__s16(data[pos:pos + 2]) / 64.0, 3); pos += 2
        elif name == "sdda":
            if pos + 3 > len(data): return out, len(data)
            out["doppler_accel_precision_ms2"] = round(_cat15__u16(data[pos:pos + 2]) / 64.0, 3)
            out["corr_doppler_accel_vel"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4); pos += 3
        else:
            if pos >= len(data): return out, len(data)
            key = {
                "codvr": "corr_doppler_vel_range", "codvrr": "corr_doppler_vel_range_rate",
                "codvra": "corr_doppler_vel_range_accel", "codar": "corr_doppler_accel_range",
                "codarr": "corr_doppler_accel_range_rate", "codara": "corr_doppler_accel_range_accel",
            }[name]
            out[key] = round(_cat15__s8(data[pos]) / 128.0, 4); pos += 1
    return out, pos


def _cat15__decode_627(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/627 Azimuth Information: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("az", "rsaz", "sdasz", "azr", "sdazr", "azex")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "az":
            if pos + 2 > len(data): return out, len(data)
            out["azimuth_deg"] = round(_cat15__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 3); pos += 2
        elif name == "rsaz":
            if pos + 2 > len(data): return out, len(data)
            out["azimuth_resolution_deg"] = round(_cat15__u16(data[pos:pos + 2]) * 45.0 / 65536.0, 4); pos += 2
        elif name == "sdasz":
            if pos + 2 > len(data): return out, len(data)
            out["azimuth_precision_deg"] = round(_cat15__u16(data[pos:pos + 2]) * 45.0 / 65536.0, 4); pos += 2
        elif name == "azr":
            if pos + 2 > len(data): return out, len(data)
            out["azimuth_rate_degs"] = round(_cat15__s16(data[pos:pos + 2]) * 180.0 / 65536.0, 4); pos += 2
        elif name == "sdazr":
            if pos + 3 > len(data): return out, len(data)
            out["azimuth_rate_precision_degs"] = round(_cat15__u16(data[pos:pos + 2]) * 45.0 / 65536.0, 4)
            out["corr_azimuth_rate_azimuth"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4); pos += 3
        elif name == "azex":
            if pos + 4 > len(data): return out, len(data)
            out["azimuth_extent_start_deg"] = round(_cat15__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 3)
            out["azimuth_extent_end_deg"] = round(_cat15__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0, 3); pos += 4
    return out, pos


def _cat15__decode_628(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/628 Elevation Information: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("el", "rsel", "sdel", "er", "sder", "elex")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "el":
            if pos + 2 > len(data): return out, len(data)
            out["elevation_deg"] = round(_cat15__s16(data[pos:pos + 2]) * 180.0 / 65536.0, 4); pos += 2
        elif name == "rsel":
            if pos + 2 > len(data): return out, len(data)
            out["elevation_resolution_deg"] = round(_cat15__u16(data[pos:pos + 2]) * 45.0 / 65536.0, 4); pos += 2
        elif name == "sdel":
            if pos + 2 > len(data): return out, len(data)
            out["elevation_precision_deg"] = round(_cat15__u16(data[pos:pos + 2]) * 45.0 / 65536.0, 4); pos += 2
        elif name == "er":
            if pos + 2 > len(data): return out, len(data)
            out["elevation_rate_degs"] = round(_cat15__s16(data[pos:pos + 2]) * 180.0 / 65536.0, 4); pos += 2
        elif name == "sder":
            if pos + 3 > len(data): return out, len(data)
            out["elevation_rate_precision_degs"] = round(_cat15__u16(data[pos:pos + 2]) * 45.0 / 65536.0, 4)
            out["corr_elevation_rate_elevation"] = round(_cat15__s8(data[pos + 2]) / 128.0, 4); pos += 3
        elif name == "elex":
            if pos + 4 > len(data): return out, len(data)
            out["elevation_extent_start_deg"] = round(_cat15__s16(data[pos:pos + 2]) * 180.0 / 65536.0, 3)
            out["elevation_extent_end_deg"] = round(_cat15__s16(data[pos + 2:pos + 4]) * 180.0 / 65536.0, 3); pos += 4
    return out, pos


def _cat15__decode_630(data: bytes, pos: int) -> tuple[dict, int]:
    """I015/630 Path Quality: compound, 1 presence byte."""
    out: dict = {}
    flags, pos = _cat15__presence(data, pos, 1)
    names = ("dpp", "dps", "rpp", "rps")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name in ("dpp", "dps", "rps"):
            if pos >= len(data): return out, len(data)
            key = {"dpp": "direct_path_power_db", "dps": "direct_path_snr_db", "rps": "reflected_path_snr_db"}[name]
            out[key] = _cat15__s8(data[pos]); pos += 1
        elif name == "rpp":
            if pos + 2 > len(data): return out, len(data)
            v = _cat15__u16(data[pos:pos + 2]); pos += 2
            out["reflected_path_power_db"] = _cat15__signed_bits(v & 0x1FF, 9)
    return out, pos


def _cat15_decode_cat015_record(data: bytes, pos: int, site_lat=None, site_lon=None):
    """Decode exactly the public EUROCONTROL CAT-015 Edition 1.2 UAP."""
    fspec, pos = _cat15_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-15 Ed.1.2"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        try:
            if frn == 0:                    # I015/010 SAC/SIC
                if pos + 2 > len(data): return track, len(data)
                track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
            elif frn == 1:                  # I015/000 Message Type
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                track["msg_type"] = _cat15__MSG_TYPES.get((b >> 1) & 0x7F, "type_{}".format((b >> 1) & 0x7F))
                track["event_driven"] = bool(b & 0x01)
            elif frn == 2:                  # I015/015 Service Identification
                if pos >= len(data): return track, len(data)
                track["service_id"] = data[pos]; pos += 1
            elif frn == 3:                  # I015/020 Target Report Descriptor (FX)
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                track["momu"] = _cat15__MOMU.get((b >> 6) & 0x03, "unknown")
                track["ttax"] = _cat15__TTAX.get((b >> 4) & 0x03, "unknown")
                track["scd"] = _cat15__SCD.get((b >> 2) & 0x03, "unknown")
                while b & 0x01:
                    if pos >= len(data): return track, len(data)
                    b = data[pos]; pos += 1
            elif frn == 4:                  # I015/030 Warning/Error Conditions (repetitive FX)
                if pos >= len(data): return track, len(data)
                conditions = []
                b = data[pos]; pos += 1
                conditions.append((b >> 1) & 0x7F)
                while b & 0x01:
                    if pos >= len(data): return track, len(data)
                    b = data[pos]; pos += 1
                    conditions.append((b >> 1) & 0x7F)
                if conditions: track["warning_error_conditions"] = conditions
            elif frn == 5:                  # I015/145 Time of Applicability
                if pos + 3 > len(data): return track, len(data)
                track["tod_s"] = _cat15__u24(data[pos:pos + 3]) / 128.0; pos += 3
            elif frn == 6:                  # I015/161 Track/Plot Number
                if pos + 2 > len(data): return track, len(data)
                track["track_num"] = _cat15__u16(data[pos:pos + 2]); pos += 2
            elif frn == 7:                  # I015/170 Track/Plot Status (FX)
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["in_blind_zone"] = True
                if b & 0x40: track["in_blanked_zone"] = True
                if b & 0x20: track["terminated_by_user"] = True
                if b & 0x08: track["coasted_position"] = True
                if b & 0x04: track["coasted_height"] = True
                track["confirmed"] = not bool(b & 0x02)
                while b & 0x01:
                    if pos >= len(data): return track, len(data)
                    b = data[pos]; pos += 1
            elif frn == 8:                  # I015/050 Update Period
                if pos + 2 > len(data): return track, len(data)
                track["update_period_s"] = round((_cat15__u16(data[pos:pos + 2]) & 0x3FFF) / 128.0, 3); pos += 2
            elif frn == 9:                  # I015/270 Target Size and Orientation
                extra, pos = _cat15__decode_270(data, pos); track.update(extra)
            elif frn == 10:                 # I015/300 Object Classification (repetitive 1, 2B/entry)
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                classes = []
                for _ in range(rep):
                    if pos + 2 > len(data): break
                    v = _cat15__u16(data[pos:pos + 2]); pos += 2
                    classes.append({"class": (v >> 7) & 0x1FF, "probability": v & 0x7F})
                if classes: track["object_classification"] = classes
            elif frn == 11:                 # I015/400 Measurement Identifier
                if pos + 5 > len(data): return track, len(data)
                track["pair_id"] = _cat15__u16(data[pos:pos + 2])
                track["observation_num"] = _cat15__u24(data[pos + 2:pos + 5]); pos += 5
            elif frn == 12:                 # I015/600 Horizontal Position Information
                extra, pos = _cat15__decode_600(data, pos); track.update(extra)
            elif frn == 13:                 # I015/601 Geometric Height Information
                extra, pos = _cat15__decode_601(data, pos); track.update(extra)
            elif frn == 14:                 # I015/602 Horizontal Velocity Information
                extra, pos = _cat15__decode_602(data, pos); track.update(extra)
            elif frn == 15:                 # I015/603 Horizontal Acceleration Information
                extra, pos = _cat15__decode_603(data, pos); track.update(extra)
            elif frn == 16:                 # I015/604 Vertical Velocity Information
                extra, pos = _cat15__decode_604(data, pos); track.update(extra)
            elif frn == 17:                 # I015/605 Vertical Acceleration Information
                extra, pos = _cat15__decode_605(data, pos); track.update(extra)
            elif frn == 18:                 # I015/480 Associations (repetitive 1, 5B/entry, raw)
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                assocs = []
                for _ in range(rep):
                    if pos + 5 > len(data): break
                    assocs.append(data[pos:pos + 5].hex()); pos += 5
                if assocs: track["associations_raw"] = assocs
            elif frn == 19:                 # I015/625 Range Information
                extra, pos = _cat15__decode_625(data, pos); track.update(extra)
            elif frn == 20:                 # I015/626 Doppler Information
                extra, pos = _cat15__decode_626(data, pos); track.update(extra)
            elif frn == 21:                 # I015/627 Azimuth Information
                extra, pos = _cat15__decode_627(data, pos); track.update(extra)
            elif frn == 22:                 # I015/628 Elevation Information
                extra, pos = _cat15__decode_628(data, pos); track.update(extra)
            elif frn == 23:                 # I015/630 Path Quality
                extra, pos = _cat15__decode_630(data, pos); track.update(extra)
            elif frn == 24:                 # I015/631 Contour (repetitive 1, 8B/entry)
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                contours = []
                for _ in range(rep):
                    if pos + 8 > len(data): break
                    contours.append({
                        "azimuth_deg": round(_cat15__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 3),
                        "elevation_deg": round(_cat15__s16(data[pos + 2:pos + 4]) * 180.0 / 65536.0, 3),
                        "range_stop_m": round(_cat15__u16(data[pos + 4:pos + 6]) * 10000.0 / 65536.0, 2),
                        "range_start_m": round(_cat15__u16(data[pos + 6:pos + 8]) * 10000.0 / 65536.0, 2),
                    })
                    pos += 8
                if contours: track["contours"] = contours
            elif frn == 25:                 # I015/SP
                pos = _cat15__skip_len_field(data, pos)
            else:
                return track, len(data)
        except ValueError:
            return track, len(data)

    if ("lat_deg" not in track or "lon_deg" not in track) and site_lat is not None and site_lon is not None:
        if "range_m" in track and "azimuth_deg" in track:
            lat, lon = _cat15__polar_to_wgs84(site_lat, site_lon, track["range_m"], track["azimuth_deg"])
            track["lat_deg"] = round(lat, 7); track["lon_deg"] = round(lon, 7)

    return track, pos


def _cat15__make_handler(session, site, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat15_decode_cat015_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if "lat_deg" not in track or "lon_deg" not in track:
                if verbose:
                    print("cat15 record without map position; configure CAT15_SITE_LAT/LON for range/azimuth georeferencing", flush=True)
                continue
            topic = _cat15_TOPIC_AIR.format(source=_asterix_source(track))
            publish_dual(session, topic, track, AsterixCat15Track)
            publish_native(session, native_topic(semantic_topic(topic, track)),
                           asterix_data_block(15, data[previous:pos]),
                           "asterix", profile="cat015")
            if verbose:
                print("cat15 {} -> {}".format(track.get("track_num", "target"), topic), flush=True)
    return _h


def _cat15__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat15_CAT_015:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat15__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat15__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat15__raw_frame_payload(bytes(sample.payload), _cat15_CAT_015)
        except ValueError as exc:
            print("CAT-15 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-15 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat15__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat15__process_stream(_cat15_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-15 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-15 TCP disconnected: {}".format(addr), flush=True)


def _cat15__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat15__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-15 Ed.1.2 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-15 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat15__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-15 Ed.1.2 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat15__process_stream(_cat15_iter_frames_udp(sock), handler, verbose)


def _cat15_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-015 Ed.1.2 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT15_PORT", "50015") or 50015))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT15_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT15_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT15_INPUT_TOPIC", _cat15_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat15__env_float("CAT15_SITE_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat15__env_float("CAT15_SITE_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT15_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = open_session()
    handler = _cat15__make_handler(session, site, args.verbose)
    try:
        print("Zenoh CAT-15 topics:", _cat15_TOPIC_AIR, flush=True)
        if args.zenoh_raw: _cat15__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat15__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-016 — Independent Non-Cooperative Surveillance System Configuration
# Reports, Ed.1.0
#
# CAT-015 has to CAT-016 what CAT-034 has to CAT-048, or CAT-020 has to
# CAT-019: CAT-015 carries the INCS target reports, CAT-016 carries the INCS
# ground system's own configuration (its own WGS-84 position, and the
# transmitter/receiver components that make up a multi-static system). Not a
# track: no target lat/lon, published as a sensor status record like
# CAT-002/019/023/063 — except here the position fields (I016/400/405) *are*
# the sensor's own site position, so they populate lat_deg/lon_deg directly
# for map placement, same idea as the --site-lat/--site-lon fallback used by
# other status categories but sourced from the wire instead of a CLI flag.
#
# Full spec text read directly (docs/references/asterix-specs/cat016/cat-1.0.ast),
# same as CAT-015 — no field kept raw due to ambiguity.
# ==========================================================================

_cat16_TOPIC_ROOT = topic_root()

_cat16_TOPIC_SENSOR    = _cat16_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat16_RAW_INPUT_TOPIC = "{}/raw/asterix/cat16".format(_cat16_TOPIC_ROOT)
_cat16_CAT_016 = 16

_cat16__MSG_TYPES = {1: "system_configuration", 2: "transmitter_receiver_configuration"}


def _cat16__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat16__netbird_ip() -> str:
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


def _cat16__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat16_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat16__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat16__recv_exact(sock, length - 3)
        record_in("cat16", len(data))
        yield cat, data


def _cat16_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat16", len(record))
            yield cat, record
            offset += length


def _cat16_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat16__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat16__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat16__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]


def _cat16_decode_cat016(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat16_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I016/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I016/015 Service Identification
            if pos >= len(data): break
            msg["service_id"] = data[pos]; pos += 1
        elif frn == 2:                  # I016/000 Message Type
            if pos >= len(data): break
            msg["msg_type"] = _cat16__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 3:                  # I016/140 Time of Day
            if pos + 3 > len(data): break
            msg["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 4:                  # I016/200 System Configuration Reporting Period
            if pos >= len(data): break
            msg["reporting_period_s"] = data[pos]; pos += 1
        elif frn == 5:                  # I016/300 Pair Identification (repetitive 1, 6B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            pairs = []
            for _ in range(rep):
                if pos + 6 > len(data): break
                pairs.append({
                    "pair_id": struct.unpack(">H", data[pos:pos + 2])[0],
                    "transmitter_id": struct.unpack(">H", data[pos + 2:pos + 4])[0],
                    "receiver_id": struct.unpack(">H", data[pos + 4:pos + 6])[0],
                })
                pos += 6
            if pairs: msg["pairs"] = pairs
        elif frn == 6:                  # I016/400 Position of System Reference Point
            if pos + 8 > len(data): break
            scale = 180.0 / 2**31
            msg["lat_deg"] = round(_cat16__s32(data[pos:pos + 4]) * scale, 7)
            msg["lon_deg"] = round(_cat16__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
        elif frn == 7:                  # I016/405 Height of System Reference Point
            if pos + 2 > len(data): break
            msg["height_m"] = round(_cat16__s16(data[pos:pos + 2]) * 0.25, 2); pos += 2
        elif frn == 8:                  # I016/410 Transmitter Properties (repetitive 1, 21B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            scale = 180.0 / 2**31
            transmitters = []
            for _ in range(rep):
                if pos + 21 > len(data): break
                v = int.from_bytes(data[pos + 16:pos + 21], "big")  # TTO(32)+spare(4)+ATO(20)+PCI(16) = 72 bits
                transmitters.append({
                    "transmitter_id": struct.unpack(">H", data[pos:pos + 2])[0],
                    "lat_deg": round(_cat16__s32(data[pos + 2:pos + 6]) * scale, 7),
                    "lon_deg": round(_cat16__s32(data[pos + 6:pos + 10]) * scale, 7),
                    "altitude_m": round(_cat16__s16(data[pos + 10:pos + 12]) * 0.25, 2),
                    "tx_time_offset_ns": _cat16__s32(data[pos + 12:pos + 16]) * 2,
                    "tx_time_offset_accuracy_ns": (v >> 16) & 0xFFFFF,
                    "parallel_transmitter_index": v & 0xFFFF,
                })
                pos += 21
            if transmitters: msg["transmitters"] = transmitters
        elif frn == 9:                  # I016/420 Receiver Properties (repetitive 1, 12B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            scale = 180.0 / 2**31
            receivers = []
            for _ in range(rep):
                if pos + 12 > len(data): break
                receivers.append({
                    "receiver_id": struct.unpack(">H", data[pos:pos + 2])[0],
                    "lat_deg": round(_cat16__s32(data[pos + 2:pos + 6]) * scale, 7),
                    "lon_deg": round(_cat16__s32(data[pos + 6:pos + 10]) * scale, 7),
                    "altitude_m": round(_cat16__s16(data[pos + 10:pos + 12]) * 0.25, 2),
                })
                pos += 12
            if receivers: msg["receivers"] = receivers
        elif frn == 10:                 # I016/SP
            pos = _cat16__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat16__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat16_CAT_016:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat16__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat16__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat16__raw_frame_payload(bytes(sample.payload), _cat16_CAT_016)
        except ValueError as exc:
            print("CAT-16 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-16 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat16__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat16__process_stream(_cat16_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-16 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-16 TCP disconnected: {}".format(addr), flush=True)


def _cat16__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat16__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-16 Ed.1.0 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-16 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat16__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-16 Ed.1.0 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat16__process_stream(_cat16_iter_frames_udp(sock), handler, verbose)


def _cat16__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat16_decode_cat016(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-16 Ed.1.0"
        topic = _cat16_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat16Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-16 {} msg_type={}".format(topic, msg.get("msg_type")), flush=True)
    return _h


def _cat16_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-016 Ed.1.0 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT16_PORT", "50016") or 50016))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT16_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT16_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT16_INPUT_TOPIC", _cat16_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT16_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat16__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat16__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat16__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-017 — Mode S Surveillance Coordination Function Messages, Ed.1.3
#
# Legacy (2009) inter-radar coordination protocol for Mode S ground stations
# in a Surveillance Coordination Network (SCN) / cluster topology: nodes
# exchange "Track Data" messages (with position/velocity) plus network
# management messages (track requests/stops, cluster hand-over, node lists)
# that carry no target position at all. Single UAP, 21 FRNs (14 real items,
# 5 spares, SP). Full spec text read directly
# (docs/references/asterix-specs/cat017/cat-1.3.ast), same as CAT-015/016 —
# no field kept raw.
# ==========================================================================

_cat17_TOPIC_ROOT = topic_root()

_cat17_TOPIC_AIR    = _cat17_TOPIC_ROOT + "/air/{source}/radar/civ/aircraft"
_cat17_TOPIC_SENSOR = _cat17_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat17_RAW_INPUT_TOPIC = "{}/raw/asterix/cat17".format(_cat17_TOPIC_ROOT)
_cat17_CAT_017 = 17

_cat17__MSG_TYPES = {
    0: "network_information", 10: "track_data", 20: "track_data_request",
    21: "track_data_stop", 22: "cancel_track_data_request",
    23: "track_data_stop_ack", 30: "new_node_changeover_initial",
    31: "new_node_changeover_final", 32: "new_node_changeover_initial_reply",
    33: "new_node_changeover_final_reply", 110: "move_node_new_cluster_state",
    111: "move_node_new_cluster_state_ack",
}

_cat17__CA = {
    0: "surveillance_only", 4: "comm_ab_ground", 5: "comm_ab_airborne",
    6: "comm_ab_airborne_or_ground", 7: "dr_or_fs_set",
}


def _cat17__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat17__netbird_ip() -> str:
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


def _cat17__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat17_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat17__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat17__recv_exact(sock, length - 3)
        record_in("cat17", len(data))
        yield cat, data


def _cat17_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat17", len(record))
            yield cat, record
            offset += length


def _cat17_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat17__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat17__s24(b: bytes) -> int:
    v = int.from_bytes(b, "big")
    return v - (1 << 24) if v & (1 << 23) else v


def _cat17_decode_cat017_record(data: bytes, pos: int):
    """Decode exactly the public EUROCONTROL CAT-017 Edition 1.3 UAP."""
    fspec, pos = _cat17_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-17 Ed.1.3"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I017/010 Data Source Identifier
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I017/012 Data Destination Identifier
            if pos + 2 > len(data): return track, len(data)
            track["dest_sac"], track["dest_sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 2:                  # I017/000 Message Type
            if pos >= len(data): return track, len(data)
            track["msg_type"] = _cat17__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 3:                  # I017/350 Cluster Station/Node List (repetitive 1, 2B/entry)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            nodes = []
            for _ in range(rep):
                if pos + 2 > len(data): break
                nodes.append({"sac": data[pos], "sic": data[pos + 1]}); pos += 2
            if nodes: track["cluster_nodes"] = nodes
        elif frn == 4:                  # I017/220 Aircraft Address
            if pos + 3 > len(data): return track, len(data)
            track["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif frn == 5:                  # I017/221 Duplicate Address Reference Number
            if pos + 2 > len(data): return track, len(data)
            track["duplicate_address_ref"] = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
        elif frn == 6:                  # I017/140 Time of Day
            if pos + 3 > len(data): return track, len(data)
            track["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 7:                  # I017/045 Calculated Position WGS-84
            # Spec text declares a +-90/+-180 semantic range, but a 24-bit
            # signed field at this LSB tops out near +-45 deg — decoded
            # literally as transmitted; the declared range is evidently a
            # generic lat/lon domain annotation, not a promise this field
            # reaches those extremes.
            if pos + 6 > len(data): return track, len(data)
            scale = 180.0 / 2**25
            track["lat_deg"] = round(_cat17__s24(data[pos:pos + 3]) * scale, 6)
            track["lon_deg"] = round(_cat17__s24(data[pos + 3:pos + 6]) * scale, 6); pos += 6
        elif frn == 8:                  # I017/070 Mode-3/A Code
            if pos + 2 > len(data): return track, len(data)
            b1, b2 = data[pos], data[pos + 1]; pos += 2
            if b1 & 0x80: track["squawk_not_validated"] = True
            if b1 & 0x40: track["squawk_garbled"] = True
            if b1 & 0x20: track["squawk_smoothed"] = True
            track["squawk"] = "{:04o}".format(((b1 << 8) | b2) & 0x0FFF)
        elif frn == 9:                  # I017/050 Flight Level
            if pos + 2 > len(data): return track, len(data)
            w = struct.unpack(">H", data[pos:pos + 2])[0]; pos += 2
            if w & 0x8000: track["flight_level_not_validated"] = True
            if w & 0x4000: track["flight_level_garbled"] = True
            track["flight_level"] = round((w & 0x3FFF) * 0.25, 2)
        elif frn == 10:                 # I017/200 Track Velocity Polar
            if pos + 4 > len(data): return track, len(data)
            gsp = struct.unpack(">H", data[pos:pos + 2])[0] / 16384.0
            hdg = struct.unpack(">H", data[pos + 2:pos + 4])[0] * 360.0 / 65536.0; pos += 4
            track["ground_speed_kt"] = round(gsp * 3600.0, 2)
            track["heading_deg"] = round(hdg, 2)
        elif frn == 11:                 # I017/230 Transponder Capability
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["comm_capability"] = _cat17__CA.get((b >> 5) & 0x07, "reserved")
            track["si_capable"] = not bool(b & 0x10)
        elif frn == 12:                 # I017/240 Track Status
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["coasted"] = True
            if b & 0x40: track["flight_level_predicted"] = True
        elif frn == 13:                 # I017/210 Mode S Address List (repetitive 1, 3B/entry)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            addrs = []
            for _ in range(rep):
                if pos + 3 > len(data): break
                addrs.append(data[pos:pos + 3].hex()); pos += 3
            if addrs: track["mode_s_addresses"] = addrs
        elif frn == 14:                 # I017/360 Cluster Controller Command State
            if pos >= len(data): return track, len(data)
            track["cluster_command_state"] = data[pos]; pos += 1
        elif 15 <= frn <= 19:           # spare (no encoded item)
            continue
        elif frn == 20:                 # I017/SP
            pos = _cat17__skip_len_field(data, pos)
        else:
            return track, len(data)
    return track, pos


def _cat17__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat17_decode_cat017_record(data, pos)
            if pos <= previous:
                break
            if "lat_deg" in track and "lon_deg" in track:
                topic = _cat17_TOPIC_AIR.format(source=_asterix_source(track))
                publish_dual(session, topic, track, AsterixCat17Track)
            else:
                topic = _cat17_TOPIC_SENSOR.format(source=_asterix_source(track))
                publish_dual(session, topic, track, AsterixCat17Track, wrapper_field="sensor")
            publish_native(session, native_topic(semantic_topic(topic, track)),
                           asterix_data_block(17, data[previous:pos]),
                           "asterix", profile="cat017")
            if verbose:
                print("cat17 {} -> {}".format(track.get("msg_type", "record"), topic), flush=True)
    return _h


def _cat17__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat17_CAT_017:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat17__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat17__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat17__raw_frame_payload(bytes(sample.payload), _cat17_CAT_017)
        except ValueError as exc:
            print("CAT-17 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-17 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat17__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat17__process_stream(_cat17_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-17 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-17 TCP disconnected: {}".format(addr), flush=True)


def _cat17__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat17__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-17 Ed.1.3 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-17 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat17__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-17 Ed.1.3 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat17__process_stream(_cat17_iter_frames_udp(sock), handler, verbose)


def _cat17_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-017 Ed.1.3 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT17_PORT", "50017") or 50017))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT17_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT17_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT17_INPUT_TOPIC", _cat17_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT17_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat17__make_handler(session, args.verbose)
    try:
        print("Zenoh CAT-17 topics:", _cat17_TOPIC_AIR, _cat17_TOPIC_SENSOR, flush=True)
        if args.zenoh_raw: _cat17__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat17__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-018 — Mode S Datalink Function Messages, Ed.1.8
#
# Ground Data Link Processor (GDLP) <-> interrogator coordination for Mode S
# uplink/downlink datalink management: aircraft datalink status/reports,
# uplink packet/broadcast/GICB-extraction requests and their acknowledgements.
# Single UAP, 35 FRNs, no SP field. Position is local polar/cartesian only
# (I018/014, I018/015) — no absolute WGS-84 item exists in this category —
# so georeferencing uses the same --site-lat/--site-lon polar/cartesian
# fallback pattern as CAT-001/007/010/011/015.
#
# I018/029 "GICB Extracted" is typed "bds ?" in the spec (its actual encoding
# depends on which BDS register I018/027 names) — decoded using this
# category's own BDS 3,0/4,0/5,0/6,0 helpers when the code matches a known
# one, raw hex otherwise. I018/031 "Aircraft Identity" is declared generic
# "raw" in the DSL but is explicitly a BDS 2,0 extraction per ICAO Annex 10,
# which is the same 6-bit callsign charset used elsewhere in this file, so
# it is decoded as a callsign rather than kept opaque.
#
# Full spec text read directly
# (docs/references/asterix-specs/cat018/cat-1.8.ast), no field kept raw
# purely due to ambiguity — only I018/019 "Mode S Packet" (an explicit,
# genuinely payload-opaque uplink/downlink packet) is kept as a raw hex blob,
# which is what the spec itself says it is.
# ==========================================================================

_cat18_TOPIC_ROOT = topic_root()

_cat18_TOPIC_AIR    = _cat18_TOPIC_ROOT + "/air/{source}/radar/civ/aircraft"
_cat18_TOPIC_SENSOR = _cat18_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat18_RAW_INPUT_TOPIC = "{}/raw/asterix/cat18".format(_cat18_TOPIC_ROOT)
_cat18_CAT_018 = 18

_cat18__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat18__MSG_TYPES = {
    0: "associate_req", 1: "associate_resp", 2: "release_req", 3: "release_resp",
    4: "abort_req", 5: "keep_alive", 16: "aircraft_report", 17: "aircraft_command",
    18: "ii_code_change", 32: "uplink_packet", 33: "cancel_uplink_packet",
    34: "uplink_packet_ack", 35: "downlink_packet", 38: "data_xon", 39: "data_xoff",
    48: "uplink_broadcast", 49: "cancel_uplink_broadcast", 50: "uplink_broadcast_ack",
    52: "downlink_broadcast", 64: "gicb_extraction", 65: "cancel_gicb_extraction",
    66: "gicb_extraction_ack", 67: "gicb_response",
}

_cat18__CAUSE = {
    0: "accepted", 1: "rejected", 2: "cancelled", 3: "finished",
    4: "delayed", 5: "in_progress", 6: "in_progress",
}

_cat18__DIAG = {
    0: "none", 1: "aircraft_exit", 2: "incorrect_aircraft_address",
    3: "cannot_process", 4: "insufficient_datalink_capability",
    5: "invalid_lv_field", 6: "duplicate_request_number", 7: "unknown_request_number",
    8: "timer_t3_expiry", 9: "expiry_of_ir_delivery_timer", 10: "uplink_flow_disabled_by_uc",
}

_cat18__PACKET_TYPE = {0: "svc", 1: "msp", 2: "route"}

_cat18__COM = {
    0: "surveillance_only", 1: "comm_a_b", 2: "comm_a_b_uplink_elm",
    3: "comm_a_b_uplink_downlink_elm", 4: "level5_transponder",
}

_cat18__REPLY_DEST = {0: "data_link_only", 1: "surveillance_only", 2: "both"}


def _cat18__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat18__netbird_ip() -> str:
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


def _cat18__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat18_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat18__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat18__recv_exact(sock, length - 3)
        record_in("cat18", len(data))
        yield cat, data


def _cat18_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat18", len(record))
            yield cat, record
            offset += length


def _cat18_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat18__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat18__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat18__signed_bits(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value


def _cat18__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat18__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()


def _cat18__decode_bds50(mb: bytes) -> dict:
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
    if _bit(24): out["bds_gs_kt"]        = round(_uns(25, 34) * 2.0, 0)
    if _bit(35): out["track_rate_degs"]  = round(_sgn(36, 45) * 8.0 / 256.0, 2)
    if _bit(46): out["tas_kt"]           = round(_uns(47, 56) * 2.0, 0)
    return out


def _cat18__decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
    if _bit(13): out["ias_kt"]       = _uns(14, 23)
    if _bit(24): out["mach"]         = round(_uns(25, 34) * 2.048 / 512.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 45) * 32
    if _bit(46): out["ivv_fpm"]      = _sgn(47, 56) * 32
    return out


def _cat18__decode_bds40(mb: bytes) -> dict:
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


# BDS 1,0/1,7 bit layouts: pyModeS src/pyModeS/decoder/bds/bds10.py and
# bds17.py (github.com/junzis/pyModeS) — see docs/references/ASTERIX.md.
def _cat18__decode_bds10(mb: bytes) -> dict:
    """BDS 1,0 Data Link Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    return {
        "dlc_continuation_flag": bool(_bit(9)),
        "overlay_command_capability": bool(_bit(15)),
        "acas_operational": bool(_bit(16)),
        "mode_s_subnetwork_version": _uns(17, 23),
        "transponder_level5": bool(_bit(24)),
        "mode_s_specific_services": bool(_bit(25)),
        "uplink_elm_throughput": _uns(26, 28),
        "downlink_elm_throughput": _uns(29, 32),
        "aircraft_id_capability": bool(_bit(33)),
        "squitter_capability": bool(_bit(34)),
        "surveillance_id_capability": bool(_bit(35)),
        "common_usage_gicb_capability": bool(_bit(36)),
        "acas_hybrid_surveillance": bool(_bit(37)),
        "acas_resolution_advisory": bool(_bit(38)),
        "acas_rtca_version": _uns(39, 40),
        "dte_status": _uns(41, 56),
    }

_cat18__BDS17_CAPABILITY = (
    "0,5", "0,6", "0,7", "0,8", "0,9", "0,A", "2,0", "2,1",
    "4,0", "4,1", "4,2", "4,3", "4,4", "4,5", "4,8", "5,0",
    "5,1", "5,2", "5,3", "5,4", "5,5", "5,6", "5,F", "6,0",
)

def _cat18__decode_bds17(mb: bytes) -> dict:
    """BDS 1,7 Common Usage GICB Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    return {"supported_bds": [code for i, code in enumerate(_cat18__BDS17_CAPABILITY)
                               if (v >> (56 - (i + 1))) & 1]}


def _cat18__decode_bds30(mb: bytes) -> dict:
    """BDS 3,0 ACAS Resolution Advisory."""
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


def _cat18__polar_to_wgs84(radar_lat: float, radar_lon: float,
                            range_nm: float, azimuth_deg: float):
    """Haversine forward: slant-polar radar plot -> WGS-84 lat/lon."""
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


def _cat18__cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_nm: float, y_nm: float):
    """Local CAT-018 X=east/Y=north nautical miles to WGS-84."""
    x_m, y_m = x_nm * 1852.0, y_nm * 1852.0
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon


def _cat18_decode_cat018_record(data: bytes, pos: int, site_lat=None, site_lon=None):
    """Decode exactly the public EUROCONTROL CAT-018 Edition 1.8 UAP."""
    fspec, pos = _cat18_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-18 Ed.1.8"}
    bds_code = None

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I018/036 Data Source Identifier
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I018/037 Data Destination Identifier
            if pos + 2 > len(data): return track, len(data)
            track["dest_sac"], track["dest_sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 2:                  # I018/000 Message Type
            if pos >= len(data): return track, len(data)
            track["msg_type"] = _cat18__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 3:                  # I018/001 Result
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["cause"] = _cat18__CAUSE.get((b >> 4) & 0x0F, "reserved")
            track["diagnostic"] = _cat18__DIAG.get(b & 0x0F, "reserved")
        elif frn == 4:                  # I018/005 Mode S Address
            if pos + 3 > len(data): return track, len(data)
            track["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif frn == 5:                  # I018/016 Packet Number
            if pos + 4 > len(data): return track, len(data)
            track["packet_number"] = struct.unpack(">I", data[pos:pos + 4])[0]; pos += 4
        elif frn == 6:                  # I018/017 Packet Number List (repetitive 1, 4B/entry)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            nums = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                nums.append(struct.unpack(">I", data[pos:pos + 4])[0]); pos += 4
            if nums: track["packet_numbers"] = nums
        elif frn == 7:                  # I018/018 Mode S Packet Properties
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["packet_priority"] = (b >> 2) & 0x1F
            track["packet_type"] = _cat18__PACKET_TYPE.get(b & 0x03, "reserved")
        elif frn == 8:                  # I018/019 Mode S Packet (explicit, opaque payload)
            if pos >= len(data): return track, len(data)
            length = data[pos]
            if length < 1 or pos + length > len(data): return track, len(data)
            track["mode_s_packet_hex"] = data[pos + 1:pos + length].hex(); pos += length
        elif frn == 9:                  # I018/028 GICB Extraction Periodicity
            if pos + 2 > len(data): return track, len(data)
            track["gicb_periodicity_s"] = _cat18__u16(data[pos:pos + 2]); pos += 2
        elif frn == 10:                 # I018/030 GICB Properties
            if pos + 2 > len(data): return track, len(data)
            w = _cat18__u16(data[pos:pos + 2]); pos += 2
            track["gicb_priority"] = (w >> 11) & 0x1F
            if w & 0x0400: track["gicb_periodicity_strict"] = True
            if w & 0x0200: track["gicb_async_update"] = True
            if w & 0x0100: track["gicb_no_extraction"] = True
            track["gicb_reply_destination"] = _cat18__REPLY_DEST.get((w >> 6) & 0x03, "reserved")
        elif frn == 11:                 # I018/025 GICB Number
            if pos + 4 > len(data): return track, len(data)
            track["gicb_number"] = struct.unpack(">I", data[pos:pos + 4])[0]; pos += 4
        elif frn == 12:                 # I018/027 BDS Code
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            bds_code = ((b >> 4) & 0x0F, b & 0x0F)
            track["bds_code"] = "{:d},{:d}".format(*bds_code)
        elif frn == 13:                 # I018/029 GICB Extracted (type depends on I018/027's BDS code)
            if pos + 7 > len(data): return track, len(data)
            mb = data[pos:pos + 7]; pos += 7
            if bds_code == (1, 0): track.update(_cat18__decode_bds10(mb))
            elif bds_code == (1, 7): track.update(_cat18__decode_bds17(mb))
            elif bds_code == (3, 0): track.update(_cat18__decode_bds30(mb))
            elif bds_code == (4, 0): track.update(_cat18__decode_bds40(mb))
            elif bds_code == (5, 0): track.update(_cat18__decode_bds50(mb))
            elif bds_code == (6, 0): track.update(_cat18__decode_bds60(mb))
            else: track["gicb_extracted_hex"] = mb.hex()
        elif frn == 14:                 # I018/002 Time of Day
            if pos + 3 > len(data): return track, len(data)
            track["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 15:                 # I018/006 Mode S Address List (repetitive 1, 3B/entry)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            addrs = []
            for _ in range(rep):
                if pos + 3 > len(data): break
                addrs.append(data[pos:pos + 3].hex()); pos += 3
            if addrs: track["mode_s_addresses"] = addrs
        elif frn == 16:                 # I018/007 Aircraft Data Link Command
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["uplink_mask"] = True
            if b & 0x40: track["downlink_mask"] = True
            track["uplink_stop"] = bool(b & 0x20)
            track["downlink_stop"] = bool(b & 0x10)
        elif frn == 17:                 # I018/008 Aircraft Data Link Status (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["uplink_disabled_default"] = bool(b & 0x80)
            track["downlink_disabled_default"] = bool(b & 0x40)
            track["uplink_disabled_current"] = bool(b & 0x20)
            track["downlink_disabled_current"] = bool(b & 0x10)
            if b & 0x02: track["exit_indication"] = True
            while b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["interrogator_control_locked"] = True
        elif frn == 18:                 # I018/009 Aircraft Data Link Report Request (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            flags = ("status", "com", "eca", "cqf", "cqf_method", "polar_position", "cartesian_position")
            requested = [name for i, name in enumerate(flags) if b & (0x80 >> i)]
            while b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                more = ("aircraft_id", "mode_a", "speed", "height", "heading")
                requested += [name for i, name in enumerate(more) if b & (0x80 >> i)]
            if requested: track["report_requested_fields"] = requested
        elif frn == 19:                 # I018/010 Transponder Communications Capability
            if pos >= len(data): return track, len(data)
            track["comm_capability"] = _cat18__COM.get(data[pos] & 0x07, "reserved"); pos += 1
        elif frn == 20:                 # I018/011 Capability Report
            if pos + 7 > len(data): return track, len(data)
            track["capability_report_hex"] = data[pos:pos + 7].hex(); pos += 7
        elif frn == 21:                 # I018/014 Aircraft Position Polar
            if pos + 4 > len(data): return track, len(data)
            rho = _cat18__u16(data[pos:pos + 2]) / 256.0
            theta = _cat18__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0; pos += 4
            track["range_nm"] = round(rho, 3); track["azimuth_deg"] = round(theta, 3)
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat18__polar_to_wgs84(site_lat, site_lon, rho, theta)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 22:                 # I018/015 Aircraft Position Cartesian
            if pos + 4 > len(data): return track, len(data)
            x_nm = _cat18__s16(data[pos:pos + 2]) / 128.0
            y_nm = _cat18__s16(data[pos + 2:pos + 4]) / 128.0; pos += 4
            track["cart_x_nm"] = round(x_nm, 3); track["cart_y_nm"] = round(y_nm, 3)
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat18__cartesian_to_wgs84(site_lat, site_lon, x_nm, y_nm)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 23:                 # I018/020 Broadcast Number
            if pos + 4 > len(data): return track, len(data)
            track["broadcast_number"] = struct.unpack(">I", data[pos:pos + 4])[0]; pos += 4
        elif frn == 24:                 # I018/021 Broadcast Properties
            if pos + 6 > len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["broadcast_priority"] = (b >> 4) & 0x0F
            track["broadcast_power"] = b & 0x0F
            track["broadcast_duration_s"] = data[pos]; pos += 1
            track["broadcast_coverage_raw"] = data[pos:pos + 4].hex(); pos += 4
        elif frn == 25:                 # I018/022 Broadcast Prefix
            if pos + 4 > len(data): return track, len(data)
            track["broadcast_prefix_raw"] = (int.from_bytes(data[pos:pos + 4], "big") & 0x07FFFFFF); pos += 4
        elif frn == 26:                 # I018/023 Uplink/Downlink Broadcast
            if pos + 7 > len(data): return track, len(data)
            track["broadcast_message_hex"] = data[pos:pos + 7].hex(); pos += 7
        elif frn == 27:                 # I018/004 II Code
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["previous_ii_code"] = (b >> 4) & 0x0F
            track["current_ii_code"] = b & 0x0F
        elif frn == 28:                 # I018/031 Aircraft Identity (BDS 2,0 callsign)
            if pos + 6 > len(data): return track, len(data)
            callsign = _cat18__decode_callsign(data[pos:pos + 6]); pos += 6
            if callsign: track["callsign"] = callsign
        elif frn == 29:                 # I018/032 Aircraft Mode A
            if pos + 2 > len(data): return track, len(data)
            b1, b2 = data[pos], data[pos + 1]; pos += 2
            if b1 & 0x80: track["squawk_not_validated"] = True
            if b1 & 0x40: track["squawk_garbled"] = True
            track["squawk"] = "{:04o}".format(((b1 << 8) | b2) & 0x0FFF)
        elif frn == 30:                 # I018/033 Aircraft Height
            if pos + 2 > len(data): return track, len(data)
            b1, b2 = data[pos], data[pos + 1]; pos += 2
            if b1 & 0x80: track["flight_level_not_validated"] = True
            if b1 & 0x40: track["flight_level_garbled"] = True
            raw14 = ((b1 << 8) | b2) & 0x3FFF
            track["flight_level"] = round(_cat18__signed_bits(raw14, 14) * 0.25, 2)
        elif frn == 31:                 # I018/034 Aircraft Speed
            if pos + 2 > len(data): return track, len(data)
            track["ground_speed_kt"] = round((_cat18__u16(data[pos:pos + 2]) / 16384.0) * 3600.0, 2); pos += 2
        elif frn == 32:                 # I018/035 Aircraft Heading
            if pos + 2 > len(data): return track, len(data)
            track["heading_deg"] = round(_cat18__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 2); pos += 2
        elif frn == 33:                 # I018/012 Aircraft Coverage Quality Factor
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["on_ground"] = bool(b & 0x80)
            cqf = b & 0x7F
            if cqf == 0: track["cqf_supported"] = False
            elif cqf == 127: track["cqf_defined"] = False
            else: track["cqf"] = cqf
        elif frn == 34:                 # I018/013 Aircraft CQF Calculation Method
            if pos >= len(data): return track, len(data)
            track["cqf_calculation_method"] = data[pos]; pos += 1
        else:
            return track, len(data)
    return track, pos


def _cat18__make_handler(session, site, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat18_decode_cat018_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if "lat_deg" in track and "lon_deg" in track:
                topic = _cat18_TOPIC_AIR.format(source=_asterix_source(track))
                publish_dual(session, topic, track, AsterixCat18Track)
            else:
                topic = _cat18_TOPIC_SENSOR.format(source=_asterix_source(track))
                publish_dual(session, topic, track, AsterixCat18Track, wrapper_field="sensor")
            publish_native(session, native_topic(semantic_topic(topic, track)),
                           asterix_data_block(18, data[previous:pos]),
                           "asterix", profile="cat018")
            if verbose:
                print("cat18 {} -> {}".format(track.get("msg_type", "record"), topic), flush=True)
    return _h


def _cat18__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat18_CAT_018:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat18__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat18__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat18__raw_frame_payload(bytes(sample.payload), _cat18_CAT_018)
        except ValueError as exc:
            print("CAT-18 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-18 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat18__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat18__process_stream(_cat18_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-18 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-18 TCP disconnected: {}".format(addr), flush=True)


def _cat18__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat18__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-18 Ed.1.8 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-18 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat18__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-18 Ed.1.8 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat18__process_stream(_cat18_iter_frames_udp(sock), handler, verbose)


def _cat18_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-018 Ed.1.8 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT18_PORT", "50018") or 50018))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT18_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT18_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT18_INPUT_TOPIC", _cat18_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat18__env_float("CAT18_SITE_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat18__env_float("CAT18_SITE_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT18_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = open_session()
    handler = _cat18__make_handler(session, site, args.verbose)
    try:
        print("Zenoh CAT-18 topics:", _cat18_TOPIC_AIR, _cat18_TOPIC_SENSOR, flush=True)
        if args.zenoh_raw: _cat18__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat18__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-019 — MLT System Status Messages, Ed.1.3
#
# Companion status category to CAT-020 (MLT Messages), the same relationship
# CAT-034 has to CAT-048: CAT-020 carries per-target multilateration reports,
# CAT-019 carries the multilateration ground station's own health/config.
# ==========================================================================

_cat19_TOPIC_ROOT = topic_root()

_cat19_TOPIC_SENSOR    = _cat19_TOPIC_ROOT + "/land/{source}/mlat/neutral/sensor"
_cat19_RAW_INPUT_TOPIC = "{}/raw/asterix/cat19".format(_cat19_TOPIC_ROOT)
_cat19_CAT_019 = 19

_cat19__MSG_TYPES = {1: "start_of_update_cycle", 2: "periodic_status", 3: "event_triggered_status"}
_cat19__NOGO = {0: "operational", 1: "degraded", 2: "nogo", 3: "undefined"}
_cat19__REFTR = {0: "not_present", 1: "warning", 2: "faulted", 3: "good"}


def _cat19__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat19__netbird_ip() -> str:
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


def _cat19__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat19_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat19__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat19__recv_exact(sock, length - 3)
        record_in("cat19", len(data))
        yield cat, data


def _cat19_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat19", len(record))
            yield cat, record
            offset += length


def _cat19_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat19__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat19_decode_cat019(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat19_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I019/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I019/000 Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _cat19__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I019/140 Time of Day (1/128 s)
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I019/550 System Status
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["nogo"] = _cat19__NOGO.get((b >> 6) & 0x03, "undefined")
            msg["overload"] = bool(b & 0x20)
            msg["time_source_invalid"] = bool(b & 0x10)
            msg["test_target_failure"] = bool(b & 0x08)
        elif frn == 4:                  # I019/551 Tracking Processor Detailed Status
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            procs = {}
            for index, name in enumerate(("tp1", "tp2", "tp3", "tp4")):
                shift = 6 - index * 2
                procs[name] = {
                    "exec": bool(b & (1 << (shift + 1))),
                    "good": bool(b & (1 << shift)),
                }
            msg["tracking_processors"] = procs
        elif frn == 5:                  # I019/552 Remote Sensor Detailed Status (REP x 2 bytes)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            sensors = []
            for _ in range(rep):
                if pos + 2 > len(data): break
                rsi = data[pos]; b = data[pos + 1]; pos += 2
                sensors.append({
                    "rsi": rsi,
                    "rx_1090": bool(b & 0x40),
                    "tx_1030": bool(b & 0x20),
                    "tx_1090": bool(b & 0x10),
                    "good": bool(b & 0x08),
                    "online": bool(b & 0x04),
                })
            if sensors: msg["remote_sensors"] = sensors
        elif frn == 6:                  # I019/553 Reference Transponder Detailed Status (FX)
            statuses = {}
            names = ("reftr1", "reftr2", "reftr3", "reftr4")
            index = 0
            while True:
                if pos >= len(data): break
                b = data[pos]; pos += 1
                statuses[names[index]]     = _cat19__REFTR.get((b >> 6) & 0x03, "undefined")
                statuses[names[index + 1]] = _cat19__REFTR.get((b >> 3) & 0x03, "undefined")
                index += 2
                if not (b & 0x01) or index >= len(names):
                    break
            msg["reference_transponders"] = statuses
        elif frn == 7:                  # I019/600 Position of MLT System Reference Point
            if pos + 8 > len(data): break
            scale = 180.0 / 2**30
            msg["site_lat"] = round(struct.unpack(">i", data[pos:pos + 4])[0] * scale, 7)
            msg["site_lon"] = round(struct.unpack(">i", data[pos + 4:pos + 8])[0] * scale, 7)
            pos += 8
        elif frn == 8:                  # I019/610 Height of MLT System Reference Point
            if pos + 2 > len(data): break
            msg["site_alt_m"] = round(struct.unpack(">h", data[pos:pos + 2])[0] * 0.25, 2)
            pos += 2
        elif frn == 9:                  # I019/620 WGS-84 Undulation
            if pos + 1 > len(data): break
            msg["undulation_m"] = struct.unpack("b", bytes((data[pos],)))[0]
            pos += 1
        elif frn in (12, 13):           # RE / SP
            pos = _cat19__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


decode_cat019 = _cat19_decode_cat019


def _cat19__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat19_CAT_019:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat19__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat19__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat19__raw_frame_payload(bytes(sample.payload), _cat19_CAT_019)
        except ValueError as exc:
            print("CAT-19 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-19 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat19__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat19__process_stream(_cat19_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-19 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-19 TCP disconnected: {}".format(addr), flush=True)


def _cat19__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat19__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-19 Ed.1.3 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-19 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat19__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-19 Ed.1.3 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat19__process_stream(_cat19_iter_frames_udp(sock), handler, verbose)


def _cat19__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat19_decode_cat019(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-19 Ed.1.3"
        topic = _cat19_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat19Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-19 {} nogo={} sensors={}".format(
                topic, msg.get("nogo"), len(msg.get("remote_sensors", []))), flush=True)
    return _h


def _cat19_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-019 Ed.1.3 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT19_PORT", "50019") or 50019))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT19_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT19_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT19_INPUT_TOPIC", _cat19_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT19_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat19__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat19__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat19__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-020
# ==========================================================================

import argparse

import json

import math

import os

import socket

import struct

import threading

import time

from namespace_prefix import topic_root
from protocols.track_views import (publish_dual, publish_native, native_topic,
                                      semantic_topic, asterix_data_block)
from protocols.data_stats import record_in



_cat20_TOPIC_ROOT = topic_root()




# CAT-020 is multilateration, not radar — the position is computed from time
# differences of arrival, so it gets its own modality.
_cat20_TOPIC_020    = _cat20_TOPIC_ROOT + "/air/{source}/mlat/civ/aircraft"
_cat20_RAW_INPUT_TOPIC = "{}/raw/asterix/cat20".format(_cat20_TOPIC_ROOT)

_cat20_CAT_020 = 0x14

_cat20__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

def _cat20__netbird_ip() -> str:
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

def _cat20__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat20_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat20__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat20__recv_exact(sock, length - 3)
        record_in("cat20", len(data))
        yield cat, data

def _cat20_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat20", len(record))
            yield cat, record
            offset += length

def _cat20_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat20__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat20__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat20__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat20__s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

def _cat20__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)

def _cat20__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat20__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat20__decode_bds50(mb: bytes) -> dict:
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
    if _bit(24): out["bds_gs_kt"]        = round(_uns(25, 34) * 2.0, 0)
    if _bit(35): out["track_rate_degs"]  = round(_sgn(36, 45) * 8.0 / 256.0, 2)
    if _bit(46): out["tas_kt"]           = round(_uns(47, 56) * 2.0, 0)
    return out

def _cat20__decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
    if _bit(13): out["ias_kt"]       = _uns(14, 23)
    if _bit(24): out["mach"]         = round(_uns(25, 34) * 2.048 / 512.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 45) * 32
    if _bit(46): out["ivv_fpm"]      = _sgn(47, 56) * 32
    return out

def _cat20__gillham_to_ft(code: int) -> int | None:
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

def _cat20__decode_bds40(mb: bytes) -> dict:
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

# BDS 1,0/1,7 bit layouts: pyModeS src/pyModeS/decoder/bds/bds10.py and
# bds17.py (github.com/junzis/pyModeS) — see docs/references/ASTERIX.md.
def _cat20__decode_bds10(mb: bytes) -> dict:
    """BDS 1,0 Data Link Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    return {
        "dlc_continuation_flag": bool(_bit(9)),
        "overlay_command_capability": bool(_bit(15)),
        "acas_operational": bool(_bit(16)),
        "mode_s_subnetwork_version": _uns(17, 23),
        "transponder_level5": bool(_bit(24)),
        "mode_s_specific_services": bool(_bit(25)),
        "uplink_elm_throughput": _uns(26, 28),
        "downlink_elm_throughput": _uns(29, 32),
        "aircraft_id_capability": bool(_bit(33)),
        "squitter_capability": bool(_bit(34)),
        "surveillance_id_capability": bool(_bit(35)),
        "common_usage_gicb_capability": bool(_bit(36)),
        "acas_hybrid_surveillance": bool(_bit(37)),
        "acas_resolution_advisory": bool(_bit(38)),
        "acas_rtca_version": _uns(39, 40),
        "dte_status": _uns(41, 56),
    }

_cat20__BDS17_CAPABILITY = (
    "0,5", "0,6", "0,7", "0,8", "0,9", "0,A", "2,0", "2,1",
    "4,0", "4,1", "4,2", "4,3", "4,4", "4,5", "4,8", "5,0",
    "5,1", "5,2", "5,3", "5,4", "5,5", "5,6", "5,F", "6,0",
)

def _cat20__decode_bds17(mb: bytes) -> dict:
    """BDS 1,7 Common Usage GICB Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    return {"supported_bds": [code for i, code in enumerate(_cat20__BDS17_CAPABILITY)
                               if (v >> (56 - (i + 1))) & 1]}


def _cat20__decode_bds30(mb: bytes) -> dict:
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

def _cat20_decode_cat020_record(data: bytes, pos: int):
    """Decode one EUROCONTROL CAT-020 Edition 1.11 MLAT record."""
    fspec, pos = _cat20_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-20 Ed.1.11"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I020/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I020/020 Target Report Descriptor
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            technologies = [name for mask, name in (
                (0x80, "non_mode_s_1090_mlat"), (0x40, "mode_s_1090_mlat"),
                (0x20, "hf_mlat"), (0x10, "vdl4_mlat"), (0x08, "uat_mlat"),
                (0x04, "dme_tacan_mlat"), (0x02, "other_mlat")) if b & mask]
            if technologies: track["mlat_technologies"] = technologies
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["field_monitor"] = True
                if b & 0x40: track["spi"] = True
                track["channel"] = 2 if b & 0x20 else 1
                if b & 0x10: track["on_ground"] = True
                if b & 0x08: track["corrupted_reply"] = True
                if b & 0x04: track["simulated"] = True
                if b & 0x02: track["test_target"] = True
                if b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
                    track["address_format"] = (
                        "icao24", "non_icao24", "non_adsb", "unavailable")[(b >> 6) & 0x03]
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
            track["lat_deg"] = round(_cat20__s32(data[pos:pos + 4]) * scale, 6)
            track["lon_deg"] = round(_cat20__s32(data[pos + 4:pos + 8]) * scale, 6)
            pos += 8
        elif frn == 4:                  # I020/042  Cartesian (x 3B + y 3B, 0.5 m)
            if pos + 6 > len(data): return track, len(data)
            track["x_m"] = round(_cat20__s24(data[pos:pos + 3]) * 0.5, 1)
            track["y_m"] = round(_cat20__s24(data[pos + 3:pos + 6]) * 0.5, 1)
            pos += 6
        elif frn == 5:                  # I020/161 Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat20__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 6:                  # I020/170 Track Status
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["confirmed"] = not bool(b & 0x80)
            if b & 0x40: track["_delete"] = True
            if b & 0x20: track["coasting"] = True
            trend = (b >> 3) & 0x03
            if trend: track["vertical_trend"] = ("", "climbing", "descending", "invalid")[trend]
            if b & 0x04: track["manoeuvring"] = True
            if b & 0x02: track["smoothed_position"] = True
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["ghost_track"] = True
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 7:                  # I020/070 Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            w = _cat20__u16(data[pos:pos + 2]); pos += 2
            track["squawk"] = "{:04o}".format(w & 0x0FFF)
            if w & 0x8000: track["squawk_invalid"] = True
            if w & 0x4000: track["squawk_garbled"] = True
            if w & 0x2000: track["squawk_from_track"] = True
        elif frn == 8:                  # I020/202 Cartesian Velocity
            if pos + 4 > len(data): return track, len(data)
            vx = _cat20__s16(data[pos:pos + 2]) * 0.25
            vy = _cat20__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
            track["velocity_east_ms"], track["velocity_north_ms"] = vx, vy
            track["speed_ms"] = round(math.hypot(vx, vy), 2)
            if vx or vy: track["heading_deg"] = round(math.degrees(math.atan2(vx, vy)) % 360, 2)
        elif frn == 9:                  # I020/090 Barometric FL
            if pos + 2 > len(data): return track, len(data)
            w = _cat20__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x3FFF
            if raw & 0x2000: raw -= 0x4000
            track["alt_baro_ft"] = raw * 25
            if w & 0x8000: track["alt_baro_invalid"] = True
            if w & 0x4000: track["alt_baro_garbled"] = True
        elif frn == 10:                 # I020/100 Mode-C + Confidence
            if pos + 4 > len(data): return track, len(data)
            alt = _cat20__gillham_to_ft(_cat20__u16(data[pos:pos + 2]))
            if alt is not None: track["mode_c_alt_ft"] = alt
            pos += 4
        elif frn == 11:                 # I020/220 ICAO 24-bit
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 12:                 # I020/245 Target ID
            if pos + 7 > len(data): return track, len(data)
            pos += 1
            track["callsign"] = _cat20__decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 13:                 # I020/110 Measured Height
            if pos + 2 > len(data): return track, len(data)
            track["measured_height_ft"] = _cat20__s16(data[pos:pos + 2]) * 6.25; pos += 2
        elif frn == 14:                 # I020/105 Geometric Height
            if pos + 2 > len(data): return track, len(data)
            track["alt_geom_ft"] = round(_cat20__s16(data[pos:pos + 2]) * 6.25); pos += 2
        elif frn == 15:                 # I020/210 Calculated Acceleration
            if pos + 2 > len(data): return track, len(data)
            track["accel_east_ms2"] = struct.unpack_from("b", data, pos)[0] * 0.25
            track["accel_north_ms2"] = struct.unpack_from("b", data, pos + 1)[0] * 0.25; pos += 2
        elif frn == 16:                 # I020/300 Vehicle Fleet ID
            if pos + 1 > len(data): return track, len(data)
            track["fleet_id"] = data[pos]; pos += 1
        elif frn == 17:                 # I020/310 Pre-programmed Message
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["in_trouble"] = True
            msg_type = b & 0x7F
            _MSG310 = (
                "",
                "towing_aircraft",
                "follow_me",
                "runway_check",
                "emergency_operation",
                "work_in_progress",
            )
            if msg_type:
                track["preprog_msg"] = (
                    _MSG310[msg_type]
                    if msg_type < len(_MSG310)
                    else "type_{}".format(msg_type)
                )
        elif frn == 18:                 # I020/500 Position Accuracy
            if pos >= len(data): return track, len(data)
            psf = data[pos]; pos += 1
            if psf & 0x80:
                if pos + 6 > len(data): return track, len(data)
                track["dop_x"] = _cat20__u16(data[pos:pos + 2]) * 0.25
                track["dop_y"] = _cat20__u16(data[pos + 2:pos + 4]) * 0.25
                track["dop_xy"] = _cat20__s16(data[pos + 4:pos + 6]) * 0.25
                pos += 6
            if psf & 0x40:
                if pos + 6 > len(data): return track, len(data)
                track["pos_accuracy_x_m"] = _cat20__u16(data[pos:pos + 2]) * 0.25
                track["pos_accuracy_y_m"] = _cat20__u16(data[pos + 2:pos + 4]) * 0.25
                track["pos_correlation"] = _cat20__s16(data[pos + 4:pos + 6]) * 0.25
                pos += 6
            if psf & 0x20:
                if pos + 2 > len(data): return track, len(data)
                track["height_accuracy_m"] = _cat20__u16(data[pos:pos + 2]) * 0.5; pos += 2
        elif frn == 19:                 # I020/400 Contributing Devices
            if pos + 1 > len(data): return track, len(data)
            count = data[pos]; pos += 1
            if pos + count > len(data): return track, len(data)
            track["contributing_device_masks"] = ["{:08b}".format(v) for v in data[pos:pos + count]]
            pos += count
        elif frn == 20:                 # I020/250 BDS Register Data
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb   = bytes(data[pos:pos + 7])
                bds  = data[pos + 7]; pos += 8
                bds1 = (bds >> 4) & 0x0F; bds2 = bds & 0x0F
                if   bds1 == 1 and bds2 == 0: track.update(_cat20__decode_bds10(mb))
                elif bds1 == 1 and bds2 == 7: track.update(_cat20__decode_bds17(mb))
                elif bds1 == 3 and bds2 == 0: track.update(_cat20__decode_bds30(mb))
                elif bds1 == 4 and bds2 == 0: track.update(_cat20__decode_bds40(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_cat20__decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_cat20__decode_bds60(mb))
        elif frn == 21:                 # I020/230 Comms/ACAS capability
            if pos + 2 > len(data): return track, len(data)
            track["comms_acas_raw"] = data[pos:pos + 2].hex(); pos += 2
        elif frn == 22:                 # I020/260 ACAS RA
            if pos + 7 > len(data): return track, len(data)
            track.update(_cat20__decode_bds30(data[pos:pos + 7])); pos += 7
        elif frn == 23:                 # I020/030 Warning/Error Conditions
            codes = []
            while pos < len(data):
                b = data[pos]; pos += 1; codes.append(b >> 1)
                if not b & 1: break
            if codes: track["warning_error_codes"] = codes
        elif frn == 24:                 # I020/055 Mode-1
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["mode1"] = "{:02o}".format(b & 0x1F)
        elif frn == 25:                 # I020/050 Mode-2
            if pos + 2 > len(data): return track, len(data)
            w = _cat20__u16(data[pos:pos + 2]); pos += 2
            track["mode2"] = "{:04o}".format(w & 0x0FFF)
        elif frn in (26, 27):           # RE / SP
            pos = _cat20__skip_len_field(data, pos)
        else:
            break
    return track, pos

def _cat20__pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, _cat20_TOPIC_020.format(source=_asterix_source(track)),
                 track, AsterixCat20Track)
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

def _cat20__make_cat020_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat20_decode_cat020_record(data, pos)
            if pos <= previous:
                break
            if len(track) > 2:
                _cat20__pub(pub, track, "cat20", verbose)
                publish_native(pub, native_topic(semantic_topic(
                                   _cat20_TOPIC_020.format(source=_asterix_source(track)), track)),
                               asterix_data_block(20, data[previous:pos]),
                               "asterix", profile="cat020")
    return _h

def _cat20__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat20__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat20__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat20__raw_frame_payload(bytes(sample.payload), category)
        except ValueError as exc:
            print("CAT-{} ignored invalid raw Zenoh frame: {}".format(category, exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-{} raw Zenoh input: {}".format(category, input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()

def _cat20__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat20__process_stream(_cat20_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat20__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat20__netbird_ip()
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
                target=_cat20__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat20__process_stream(_cat20_iter_frames_udp(sock), handlers, verbose)



def _cat20_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-020 Ed.1.11 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT20_PORT", "50020") or 50020))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT20_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT20_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT20_INPUT_TOPIC", _cat20_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT20_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat20__make_cat020_handler(session)
    try:
        if args.zenoh_raw: _cat20__run_zenoh_raw(session, args.input_topic, _cat20_CAT_020, handler, args.verbose)
        else: _cat20__run_inbound(args.port, args.tcp, "CAT-20 Ed.1.11", {_cat20_CAT_020: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat020_record = _cat20_decode_cat020_record


# ==========================================================================
# CAT-021
# ==========================================================================

import argparse

import json

import math

import os

import socket

import struct

import threading

import time

from namespace_prefix import topic_root
from protocols.track_views import (publish_dual, publish_native, native_topic,
                                      semantic_topic, asterix_data_block)
from protocols.data_stats import record_in



_cat21_TOPIC_ROOT = topic_root()




# CAT-021 carries ADS-B reports. The ground station relayed them; it did not
# sense the target, so the modality is `adsb`, not `radar`.
_cat21_TOPIC_021    = _cat21_TOPIC_ROOT + "/air/{source}/adsb/civ/aircraft"
_cat21_RAW_INPUT_TOPIC = "{}/raw/asterix/cat21".format(_cat21_TOPIC_ROOT)

_cat21_CAT_021 = 0x15

_cat21__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat21__EMITTER_CATEGORY = {
    0: "no info",    1: "light",      2: "small",       3: "medium",
    4: "high vortex large", 5: "heavy", 6: "manoeuvrable/high speed",
    10: "glider",    11: "airship",   12: "UAV",        13: "space vehicle",
    14: "emergency vehicle", 15: "service vehicle",    16: "ground obstruction",
}

# I021/090 NACp -> 95% horizontal Estimated Position Uncertainty, meters.
# RTCA DO-260B Table 2-14 (Navigation Accuracy Category for Position). Codes
# 12-15 are reserved/unused, deliberately absent rather than guessed. Cross-checked
# against pyModeS's own table (github.com/junzis/pyModeS,
# src/pyModeS/_uncertainty.py, dict `NACp`) rather than transcribed from memory.
_cat21__NACP_EPU_M = {
    0: None,   1: 18520, 2: 7408, 3: 3704, 4: 1852, 5: 926,
    6: 556,    7: 185,   8: 93,   9: 30,   10: 10,   11: 3,
}

def _cat21__netbird_ip() -> str:
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

def _cat21__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat21_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat21__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat21__recv_exact(sock, length - 3)
        record_in("cat21", len(data))
        yield cat, data

def _cat21_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat21", len(record))
            yield cat, record
            offset += length

def _cat21_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat21__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat21__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat21__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat21__s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

def _cat21__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat21__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat21__decode_bds50(mb: bytes) -> dict:
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
    if _bit(24): out["bds_gs_kt"]        = round(_uns(25, 34) * 2.0, 0)
    if _bit(35): out["track_rate_degs"]  = round(_sgn(36, 45) * 8.0 / 256.0, 2)
    if _bit(46): out["tas_kt"]           = round(_uns(47, 56) * 2.0, 0)
    return out

def _cat21__decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
    if _bit(13): out["ias_kt"]       = _uns(14, 23)
    if _bit(24): out["mach"]         = round(_uns(25, 34) * 2.048 / 512.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 45) * 32
    if _bit(46): out["ivv_fpm"]      = _sgn(47, 56) * 32
    return out

def _cat21__decode_bds40(mb: bytes) -> dict:
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

# BDS 1,0/1,7 bit layouts: pyModeS src/pyModeS/decoder/bds/bds10.py and
# bds17.py (github.com/junzis/pyModeS) — see docs/references/ASTERIX.md.
def _cat21__decode_bds10(mb: bytes) -> dict:
    """BDS 1,0 Data Link Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    return {
        "dlc_continuation_flag": bool(_bit(9)),
        "overlay_command_capability": bool(_bit(15)),
        "acas_operational": bool(_bit(16)),
        "mode_s_subnetwork_version": _uns(17, 23),
        "transponder_level5": bool(_bit(24)),
        "mode_s_specific_services": bool(_bit(25)),
        "uplink_elm_throughput": _uns(26, 28),
        "downlink_elm_throughput": _uns(29, 32),
        "aircraft_id_capability": bool(_bit(33)),
        "squitter_capability": bool(_bit(34)),
        "surveillance_id_capability": bool(_bit(35)),
        "common_usage_gicb_capability": bool(_bit(36)),
        "acas_hybrid_surveillance": bool(_bit(37)),
        "acas_resolution_advisory": bool(_bit(38)),
        "acas_rtca_version": _uns(39, 40),
        "dte_status": _uns(41, 56),
    }

_cat21__BDS17_CAPABILITY = (
    "0,5", "0,6", "0,7", "0,8", "0,9", "0,A", "2,0", "2,1",
    "4,0", "4,1", "4,2", "4,3", "4,4", "4,5", "4,8", "5,0",
    "5,1", "5,2", "5,3", "5,4", "5,5", "5,6", "5,F", "6,0",
)

def _cat21__decode_bds17(mb: bytes) -> dict:
    """BDS 1,7 Common Usage GICB Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    return {"supported_bds": [code for i, code in enumerate(_cat21__BDS17_CAPABILITY)
                               if (v >> (56 - (i + 1))) & 1]}


def _cat21__decode_bds30(mb: bytes) -> dict:
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

def _cat21__pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, _cat21_TOPIC_021.format(source=_asterix_source(track)),
                 track, AsterixCat21Track)
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

def _cat21__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat21__presence(data: bytes, pos: int, maximum: int) -> tuple[list[bool], int]:
    """Read all compound primary-subfield octets before their payloads."""
    present: list[bool] = []
    for _ in range(maximum):
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 1:
            return present, pos
    if data[pos - 1] & 1:
        raise ValueError("unsupported ASTERIX compound presence extension")
    return present, pos


def _cat21_decode_cat021_record(data: bytes, pos: int):
    """Decode one EUROCONTROL CAT-021 Edition 2.7 ADS-B record."""
    fspec, pos = _cat21_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-21 Ed.2.7"}

    def need(size: int) -> bool:
        return pos + size <= len(data)

    def time24(name: str) -> bool:
        nonlocal pos
        if not need(3): return False
        track[name] = int.from_bytes(data[pos:pos + 3], "big") / 128.0
        pos += 3
        return True

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I021/010
            if not need(2): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I021/040
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["addr_type"] = ("icao24", "duplicate", "surface", "anonymous",
                                  "reserved", "reserved", "reserved", "reserved")[(b >> 5) & 7]
            track["alt_res"] = ("25ft", "100ft", "unknown", "invalid")[(b >> 3) & 3]
            if b & 0x04: track["range_check_passed_cpr_pending"] = True
            if b & 0x02: track["field_monitor"] = True
            extent = 0
            while b & 1:
                if not need(1): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1:
                    if b & 0x80: track["differential_correction"] = True
                    if b & 0x40: track["on_ground"] = True
                    if b & 0x20: track["simulated"] = True
                    if b & 0x10: track["test_target"] = True
                    if b & 0x08: track["selected_altitude_unavailable"] = True
                    track["confidence_level"] = (b >> 1) & 3
                elif extent == 2:
                    for mask, name in ((0x40, "list_lookup_suspect"),
                                       (0x20, "independent_position_check_failed"),
                                       (0x10, "ground_station_nogo"),
                                       (0x08, "cpr_validation_failed"),
                                       (0x04, "local_position_jump"),
                                       (0x02, "range_check_failed")):
                        if b & mask: track[name] = True
                elif extent == 3 and b & 0x80:
                    track["total_bits_corrected"] = (b >> 1) & 0x3F
                elif extent == 4 and b & 0x80:
                    track["maximum_bits_corrected"] = (b >> 1) & 0x3F
        elif frn == 2:                  # I021/161
            if not need(2): return track, len(data)
            track["track_num"] = _cat21__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 3:                  # I021/015
            if not need(1): return track, len(data)
            track["service_id"] = data[pos]; pos += 1
        elif frn == 4:                  # I021/071
            if not time24("position_time_s"): return track, len(data)
        elif frn == 5:                  # I021/130
            if not need(6): return track, len(data)
            scale = 180.0 / 2**23
            track["lat_deg"] = round(_cat21__s24(data[pos:pos + 3]) * scale, 7)
            track["lon_deg"] = round(_cat21__s24(data[pos + 3:pos + 6]) * scale, 7); pos += 6
        elif frn == 6:                  # I021/131
            if not need(8): return track, len(data)
            scale = 180.0 / 2**30
            track["lat_deg"] = round(_cat21__s32(data[pos:pos + 4]) * scale, 8)
            track["lon_deg"] = round(_cat21__s32(data[pos + 4:pos + 8]) * scale, 8); pos += 8
        elif frn == 7:                  # I021/072
            if not time24("velocity_time_s"): return track, len(data)
        elif frn == 8:                  # I021/150
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            if w & 0x8000: track["mach"] = round((w & 0x7FFF) * 0.001, 3)
            else: track["ias_kt"] = round((w & 0x7FFF) * 3600.0 / 16384.0, 2)
        elif frn == 9:                  # I021/151
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            track["tas_kt"] = w & 0x7FFF
            if w & 0x8000: track["tas_range_exceeded"] = True
        elif frn == 10:                 # I021/080
            if not need(3): return track, len(data)
            track["icao24"] = data[pos:pos + 3].hex(); pos += 3
        elif frn in (11, 13, 27):       # I021/073, /075, /077
            names = {11: "position_reception_time_s", 13: "velocity_reception_time_s",
                     27: "report_transmission_time_s"}
            if not time24(names[frn]): return track, len(data)
        elif frn in (12, 14):           # I021/074, /076 high precision time
            if not need(4): return track, len(data)
            w = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
            track["position_reception_time_hp" if frn == 12 else "velocity_reception_time_hp"] = \
                round((w & 0x3FFFFFFF) / 2**30, 9)
        elif frn == 15:                 # I021/140
            if not need(2): return track, len(data)
            track["alt_geom_ft"] = _cat21__s16(data[pos:pos + 2]) * 6.25; pos += 2
        elif frn == 16:                 # I021/090
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["nac_v"] = (b >> 5) & 7; track["nic"] = (b >> 1) & 15
            extent = 0
            while b & 1:
                if not need(1): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1:
                    track["nic_baro"] = bool(b & 0x80)
                    track["sil"] = (b >> 5) & 3; track["nac_p"] = (b >> 1) & 15
                    epu_m = _cat21__NACP_EPU_M.get(track["nac_p"])
                    if epu_m is not None:
                        track["position_accuracy_m"] = epu_m
                elif extent == 2:
                    track["sil_per_sample"] = bool(b & 0x20)
                    track["sda"] = (b >> 3) & 3; track["gva"] = (b >> 1) & 3
                elif extent == 3:
                    track["pic"] = (b >> 4) & 15; track["pic_direct"] = bool(b & 0x08)
        elif frn == 17:                 # I021/210
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["mops_version_unsupported"] = bool(b & 0x40)
            track["mops_version"] = (b >> 3) & 7
            track["link_technology"] = ("other", "uat", "1090es", "vdl4",
                                        "unassigned", "unassigned", "unassigned", "unassigned")[b & 7]
        elif frn == 18:                 # I021/070
            if not need(2): return track, len(data)
            track["squawk"] = "{:04o}".format(_cat21__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 19:                 # I021/230
            if not need(2): return track, len(data)
            track["roll_deg"] = round(_cat21__s16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif frn == 20:                 # I021/145
            if not need(2): return track, len(data)
            track["alt_baro_ft"] = _cat21__s16(data[pos:pos + 2]) * 25; pos += 2
        elif frn == 21:                 # I021/152
            if not need(2): return track, len(data)
            track["mag_hdg_deg"] = round(_cat21__u16(data[pos:pos + 2]) * 360 / 65536, 3); pos += 2
        elif frn == 22:                 # I021/200
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x80: track["intent_change"] = True
            track["lnav_engaged"] = not bool(b & 0x40)
            if b & 0x20: track["mil_emergency"] = True
            track["priority_status"] = (b >> 2) & 7
            status = b & 3
            if status == 1: track["alert"] = "permanent"
            elif status == 2: track["alert"] = "temporary"
            elif status == 3: track["spi"] = True
        elif frn in (23, 24):           # I021/155, /157
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x7FFF
            if raw & 0x4000: raw -= 0x8000
            track["baro_vr_fpm" if frn == 23 else "geo_vr_fpm"] = raw * 6.25
            if w & 0x8000: track["vertical_rate_range_exceeded"] = True
        elif frn == 25:                 # I021/160
            if not need(4): return track, len(data)
            speed = _cat21__u16(data[pos:pos + 2]); angle = _cat21__u16(data[pos + 2:pos + 4]); pos += 4
            track["speed_ms"] = round((speed & 0x7FFF) * 2**-14 * 1852, 3)
            track["heading_deg"] = round(angle * 360 / 65536, 3)
            if speed & 0x8000: track["ground_speed_range_exceeded"] = True
        elif frn == 26:                 # I021/165
            if not need(2): return track, len(data)
            raw = _cat21__u16(data[pos:pos + 2]) & 0x03FF; pos += 2
            if raw & 0x0200: raw -= 0x0400
            track["track_angle_rate_degs"] = round(raw / 32.0, 3)
        elif frn == 28:                 # I021/170
            if not need(6): return track, len(data)
            track["callsign"] = _cat21__decode_callsign(data[pos:pos + 6]); pos += 6
        elif frn == 29:                 # I021/020
            if not need(1): return track, len(data)
            ec = data[pos]; pos += 1
            track["emitter_category"] = ec
            track["emitter_category_str"] = _cat21__EMITTER_CATEGORY.get(ec, "cat{}".format(ec))
        elif frn == 30:                 # I021/220
            try: flags, pos = _cat21__presence(data, pos, 1)
            except ValueError: return track, len(data)
            sizes = (2, 2, 2, 1)
            for index, size in enumerate(sizes):
                if index < len(flags) and flags[index]:
                    if not need(size): return track, len(data)
                    if index == 0: track["wind_speed_kt"] = _cat21__u16(data[pos:pos + 2])
                    elif index == 1: track["wind_dir_deg"] = _cat21__u16(data[pos:pos + 2])
                    elif index == 2: track["temp_c"] = _cat21__s16(data[pos:pos + 2]) * 0.25
                    else: track["turbulence"] = data[pos]
                    pos += size
        elif frn in (31, 32):           # I021/146, /148
            if not need(2): return track, len(data)
            w = _cat21__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x1FFF
            if raw & 0x1000: raw -= 0x2000
            track["selected_alt_ft" if frn == 31 else "final_alt_ft"] = raw * 25
            if frn == 31:
                source = (w >> 13) & 3
                track["selected_alt_source_available"] = bool(w & 0x8000)
                track["selected_alt_source"] = (
                    "unknown",
                    "aircraft_altitude",
                    "mcp_fcu",
                    "fms",
                )[source]
            else:
                track["managed_vertical_mode"] = bool(w & 0x8000)
                track["altitude_hold_mode"] = bool(w & 0x4000)
                track["approach_mode"] = bool(w & 0x2000)
        elif frn == 33:                 # I021/110
            try: flags, pos = _cat21__presence(data, pos, 1)
            except ValueError: return track, len(data)
            if flags[0]:
                if not need(1): return track, len(data)
                pos += 1
            if flags[1]:
                if not need(1): return track, len(data)
                rep = data[pos]; pos += 1
                if not need(rep * 15): return track, len(data)
                pos += rep * 15
        elif frn == 34:                 # I021/016
            if not need(1): return track, len(data)
            track["report_period_s"] = data[pos] * 0.5; pos += 1
        elif frn == 35:                 # I021/008
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            track["aircraft_operational_status_raw"] = b
            track["acas_ra_active"] = bool(b & 0x80)
            track["trajectory_change_capability"] = (
                "none",
                "tc_plus_zero",
                "multiple",
                "reserved",
            )[(b >> 5) & 0x03]
            track["target_state_report_capable"] = bool(b & 0x10)
            track["air_ref_velocity_capable"] = bool(b & 0x08)
            track["cdti_airborne_operational"] = bool(b & 0x04)
            track["tcas_operational"] = not bool(b & 0x02)
            track["single_antenna"] = bool(b & 0x01)
        elif frn == 36:                 # I021/271
            if not need(1): return track, len(data)
            b = data[pos]; pos += 1
            if b & 0x20: track["position_offset_applied"] = True
            if b & 0x10: track["cdti_surface_operational"] = True
            if b & 0x08: track["class_b2_low_power"] = True
            if b & 0x04: track["receiving_atc_services"] = True
            if b & 0x02: track["ident_switch"] = True
            if b & 1:
                if not need(1): return track, len(data)
                b = data[pos]; pos += 1
                track["surface_length_width_code"] = (b >> 4) & 15
                while b & 1:
                    if not need(1): return track, len(data)
                    b = data[pos]; pos += 1
        elif frn == 37:                 # I021/132
            if not need(1): return track, len(data)
            track["message_amplitude_dbm"] = struct.unpack_from("b", data, pos)[0]; pos += 1
        elif frn == 38:                 # I021/250
            if not need(1): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if not need(8): return track, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = (bds >> 4, bds & 15)
                if code == (1, 0): track.update(_cat21__decode_bds10(mb))
                elif code == (1, 7): track.update(_cat21__decode_bds17(mb))
                elif code == (3, 0): track.update(_cat21__decode_bds30(mb))
                elif code == (4, 0): track.update(_cat21__decode_bds40(mb))
                elif code == (5, 0): track.update(_cat21__decode_bds50(mb))
                elif code == (6, 0): track.update(_cat21__decode_bds60(mb))
        elif frn == 39:                 # I021/260
            if not need(7): return track, len(data)
            track.update(_cat21__decode_bds30(data[pos:pos + 7])); pos += 7
        elif frn == 40:                 # I021/400
            if not need(1): return track, len(data)
            track["receiver_id"] = data[pos]; pos += 1
        elif frn == 41:                 # I021/295
            try: flags, pos = _cat21__presence(data, pos, 4)
            except ValueError: return track, len(data)
            names = ("aircraft_status", "target_descriptor", "mode3a", "quality", "trajectory",
                     "amplitude", "geometric_height", "flight_level", "selected_altitude",
                     "final_altitude", "air_speed", "true_air_speed", "mag_heading", "baro_vr",
                     "geo_vr", "ground_vector", "track_angle_rate", "target_id", "target_status",
                     "met", "roll", "acas_ra", "surface_capabilities")
            for index, name in enumerate(names):
                if index < len(flags) and flags[index]:
                    if not need(1): return track, len(data)
                    track["data_age_{}_s".format(name)] = round(data[pos] * 0.1, 1); pos += 1
        elif frn in (47, 48):           # RE / SP
            pos = _cat21__skip_len_field(data, pos)
        elif frn >= 42:
            continue                    # explicitly unused FRNs 43..47
        else:
            break
    return track, pos


def _cat21__make_cat021_handler(pub):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat21_decode_cat021_record(data, pos)
            if pos <= previous:
                break
            if len(track) > 2:
                _cat21__pub(pub, track, "cat21", verbose)
                publish_native(pub, native_topic(semantic_topic(
                                   _cat21_TOPIC_021.format(source=_asterix_source(track)), track)),
                               asterix_data_block(21, data[previous:pos]),
                               "asterix", profile="cat021")
    return _h

def _cat21__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat21__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat21__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat21__raw_frame_payload(bytes(sample.payload), category)
        except ValueError as exc:
            print("CAT-{} ignored invalid raw Zenoh frame: {}".format(category, exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-{} raw Zenoh input: {}".format(category, input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()

def _cat21__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat21__process_stream(_cat21_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat21__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat21__netbird_ip()
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
                target=_cat21__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat21__process_stream(_cat21_iter_frames_udp(sock), handlers, verbose)



def _cat21_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-021 Ed.2.7 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT21_PORT", "50021") or 50021))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT21_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT21_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT21_INPUT_TOPIC", _cat21_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT21_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat21__make_cat021_handler(session)
    try:
        if args.zenoh_raw: _cat21__run_zenoh_raw(session, args.input_topic, _cat21_CAT_021, handler, args.verbose)
        else: _cat21__run_inbound(args.port, args.tcp, "CAT-21 Ed.2.7", {_cat21_CAT_021: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat021_record = _cat21_decode_cat021_record


# ==========================================================================
# CAT-023 — CNS/ATM Ground Station Service Messages, Ed.1.3
#
# Status/health/config reporting for ADS-B, TIS-B, FIS-B, GRAS, and MLT
# ground stations — the service infrastructure, not target reports. Same
# "system status" relationship to CAT-021 (ADS-B target reports) that
# CAT-019 has to CAT-020, and CAT-034 has to CAT-048.
# ==========================================================================

_cat23_TOPIC_ROOT = topic_root()

_cat23_TOPIC_SENSOR    = _cat23_TOPIC_ROOT + "/land/{source}/cns_atm/neutral/sensor"
_cat23_RAW_INPUT_TOPIC = "{}/raw/asterix/cat23".format(_cat23_TOPIC_ROOT)
_cat23_CAT_023 = 23

_cat23__REPORT_TYPES = {1: "ground_station_status", 2: "service_status", 3: "service_statistics"}
_cat23__SERVICE_TYPES = {
    1: "adsb_vdl4", 2: "adsb_ext_squitter", 3: "adsb_uat",
    4: "tisb_vdl4", 5: "tisb_ext_squitter", 6: "tisb_uat",
    7: "fisb_vdl4", 8: "gras_vdl4", 9: "mlt",
}
_cat23__SERVICE_STATUS = {
    0: "unknown", 1: "failed", 2: "disabled", 3: "degraded",
    4: "normal", 5: "initialisation",
}
_cat23__STAT_TYPES = {
    0: "unidentified_messages", 1: "messages_too_old", 2: "messages_format_error",
    3: "total_messages_in", 4: "total_messages_out",
    20: "tisb_admin_in", 21: "basic_in", 22: "high_dynamic_in",
    23: "full_position_in", 24: "basic_ground_in", 25: "tcp_in",
    26: "utc_time_in", 27: "data_in", 28: "high_resolution_in",
    29: "aircraft_target_airborne_in", 30: "aircraft_target_ground_in",
    31: "ground_vehicle_target_in", 32: "tcp_2slot_in",
}


def _cat23__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat23__netbird_ip() -> str:
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


def _cat23__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat23_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat23__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat23__recv_exact(sock, length - 3)
        record_in("cat23", len(data))
        yield cat, data


def _cat23_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat23", len(record))
            yield cat, record
            offset += length


def _cat23_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat23__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat23_decode_cat023(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat23_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I023/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I023/000 Report Type
            if pos + 1 > len(data): break
            msg["report_type"] = _cat23__REPORT_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I023/015 Service Type and Identification
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["service_id"] = (b >> 4) & 0x0F
            msg["service_type"] = _cat23__SERVICE_TYPES.get(b & 0x0F, "type_{}".format(b & 0x0F))
        elif frn == 3:                  # I023/070 Time of Day (1/128 s)
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 4:                  # I023/100 Ground Station Status (FX)
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["nogo"]      = bool(b & 0x80)
            msg["overload_processor"] = bool(b & 0x40)
            msg["overload_comms"]     = bool(b & 0x20)
            msg["monitoring_connected"] = bool(b & 0x10)
            msg["time_source_invalid"] = bool(b & 0x08)
            msg["spoofing_suspected"]   = bool(b & 0x04)
            msg["track_id_renumbered"]  = bool(b & 0x02)
            if b & 0x01:
                if pos + 1 > len(data): break
                b2 = data[pos]; pos += 1
                msg["status_report_period_s"] = (b2 >> 1) & 0x7F
        elif frn == 5:                  # I023/101 Service Configuration (FX)
            if pos + 2 > len(data): break
            msg["cat021_report_period_s"] = data[pos] * 0.5
            b2 = data[pos + 1]; pos += 2
            msg["service_class"] = (b2 >> 5) & 0x07
            if b2 & 0x01:
                if pos + 1 > len(data): break
                b3 = data[pos]; pos += 1
                msg["service_status_report_period_s"] = (b3 >> 1) & 0x7F
        elif frn == 6:                  # I023/200 Operational Range (NM)
            if pos + 1 > len(data): break
            msg["operational_range_nm"] = data[pos]; pos += 1
        elif frn == 7:                  # I023/110 Service Status
            if pos + 1 > len(data): break
            b = data[pos]; pos += 1
            msg["service_status"] = _cat23__SERVICE_STATUS.get((b >> 1) & 0x07, "undefined")
        elif frn == 8:                  # I023/120 Service Statistics (REP + n * 5 bytes)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            stats = {}
            for _ in range(rep):
                if pos + 5 > len(data): break
                type_code = data[pos]
                cv = struct.unpack(">I", data[pos + 1:pos + 5])[0]
                pos += 5
                name = _cat23__STAT_TYPES.get(type_code, "type_{}".format(type_code))
                stats[name] = cv
            if stats: msg["service_statistics"] = stats
        elif frn in (12, 13, 14):       # spares / RE / SP
            pos = _cat23__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat23__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat23_CAT_023:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat23__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat23__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat23__raw_frame_payload(bytes(sample.payload), _cat23_CAT_023)
        except ValueError as exc:
            print("CAT-23 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-23 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat23__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat23__process_stream(_cat23_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-23 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-23 TCP disconnected: {}".format(addr), flush=True)


def _cat23__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat23__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-23 Ed.1.3 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-23 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat23__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-23 Ed.1.3 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat23__process_stream(_cat23_iter_frames_udp(sock), handler, verbose)


def _cat23__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat23_decode_cat023(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-23 Ed.1.3"
        topic = _cat23_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat23Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-23 {} type={} service={}".format(
                topic, msg.get("report_type"), msg.get("service_type")), flush=True)
    return _h


def _cat23_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-023 Ed.1.3 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT23_PORT", "50023") or 50023))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT23_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT23_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT23_INPUT_TOPIC", _cat23_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT23_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat23__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat23__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat23__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-025 — CNS/ATM Ground System Status Reports, Ed.1.6
#
# Successor/companion to CAT-023's "CNS/ATM Ground Station Service Messages"
# (I023/I025 share the same "system status" role) — this edition adds a
# split system/service status octet, per-component status list, service
# statistics counters, and the ground system's own WGS-84 site position.
# Single UAP, 13 FRNs. Full spec text read directly
# (docs/references/asterix-specs/cat025/cat-1.6.ast); the large
# error-code/statistics-type tables (values 10-63/4-255) are almost entirely
# "reserved for allocation" placeholders in the spec itself, so only the
# meaningful low values are named — everything else keeps its numeric code
# rather than fabricating reserved-slot names.
# ==========================================================================

_cat25_TOPIC_ROOT = topic_root()

_cat25_TOPIC_SENSOR    = _cat25_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat25_RAW_INPUT_TOPIC = "{}/raw/asterix/cat25".format(_cat25_TOPIC_ROOT)
_cat25_CAT_025 = 25

_cat25__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat25__STATUS = {0: "running", 1: "failed", 2: "degraded", 3: "undefined"}
_cat25__COMPONENT_STATE = {0: "running", 1: "failed", 2: "maintenance", 3: "reserved"}
_cat25__OPS = {0: "operational", 1: "operational_standby", 2: "maintenance", 3: "reserved"}

_cat25__ERROR_CODES = {
    0: "no_error", 1: "undefined", 2: "time_source_invalid", 3: "time_source_coasting",
    4: "track_id_renumbered", 5: "data_processor_overload",
    6: "ground_interface_comms_overload", 7: "stopped_by_operator",
    8: "cbit_failed", 9: "test_target_failure",
}

_cat25__COMPONENT_ERROR_CODES = {0: "no_error", 1: "undefined", 2: "alert", 3: "alarm"}

_cat25__STAT_TYPES = {
    0: "unknown_messages", 1: "too_old_messages", 2: "failed_conversions",
    3: "total_received", 4: "total_transmitted",
}


def _cat25__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat25__netbird_ip() -> str:
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


def _cat25__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat25_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat25__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat25__recv_exact(sock, length - 3)
        record_in("cat25", len(data))
        yield cat, data


def _cat25_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat25", len(record))
            yield cat, record
            offset += length


def _cat25_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat25__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat25__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat25__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]


def _cat25__decode_designator(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit service designator from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat25__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()


def _cat25_decode_cat025(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat25_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I025/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I025/000 Report Type
            if pos >= len(data): break
            b = data[pos]; pos += 1
            msg["report_type_raw"] = (b >> 1) & 0x7F
            msg["event_driven"] = bool(b & 0x01)
        elif frn == 2:                  # I025/200 Message Identification
            if pos + 3 > len(data): break
            msg["message_id"] = int.from_bytes(data[pos:pos + 3], "big"); pos += 3
        elif frn == 3:                  # I025/015 Service Identification
            if pos >= len(data): break
            msg["service_id"] = data[pos]; pos += 1
        elif frn == 4:                  # I025/020 Service Designator
            if pos + 6 > len(data): break
            designator = _cat25__decode_designator(data[pos:pos + 6]); pos += 6
            if designator: msg["service_designator"] = designator
        elif frn == 5:                  # I025/070 Time of Day
            if pos + 3 > len(data): break
            msg["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 6:                  # I025/100 System and Service Status (FX)
            if pos >= len(data): break
            b = data[pos]; pos += 1
            if b & 0x80: msg["nogo"] = True
            msg["ops_mode"] = _cat25__OPS.get((b >> 5) & 0x03, "reserved")
            msg["overall_status"] = _cat25__STATUS.get((b >> 1) & 0x0F, "reserved")
            while b & 0x01:
                if pos >= len(data): break
                b = data[pos]; pos += 1
                msg["system_status"] = _cat25__STATUS.get((b >> 4) & 0x07, "reserved")
                msg["service_status"] = _cat25__STATUS.get((b >> 1) & 0x07, "reserved")
        elif frn == 7:                  # I025/105 System and Service Error Codes (repetitive 1)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            codes = []
            for _ in range(rep):
                if pos >= len(data): break
                codes.append(_cat25__ERROR_CODES.get(data[pos], "code_{}".format(data[pos]))); pos += 1
            if codes: msg["error_codes"] = codes
        elif frn == 8:                  # I025/120 Component Status (repetitive 1, 3B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            components = []
            for _ in range(rep):
                if pos + 3 > len(data): break
                cid = struct.unpack(">H", data[pos:pos + 2])[0]
                b3 = data[pos + 2]; pos += 3
                components.append({
                    "component_id": cid,
                    "error_code": _cat25__COMPONENT_ERROR_CODES.get((b3 >> 2) & 0x3F, "code_{}".format((b3 >> 2) & 0x3F)),
                    "state": _cat25__COMPONENT_STATE.get(b3 & 0x03, "reserved"),
                })
            if components: msg["components"] = components
        elif frn == 9:                  # I025/140 Service Statistics (repetitive 1, 6B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            stats = []
            for _ in range(rep):
                if pos + 6 > len(data): break
                type_code = data[pos]
                ref_from_previous = bool(data[pos + 1] & 0x80)
                count = struct.unpack(">I", data[pos + 2:pos + 6])[0]
                pos += 6
                stats.append({
                    "type": _cat25__STAT_TYPES.get(type_code, "type_{}".format(type_code)),
                    "reference_previous_report": ref_from_previous,
                    "count": count,
                })
            if stats: msg["service_statistics"] = stats
        elif frn == 10:                 # I025/SP
            pos = _cat25__skip_len_field(data, pos)
        elif frn == 11:                 # I025/600 Position of System Reference Point
            if pos + 8 > len(data): break
            scale = 180.0 / 2**32
            msg["lat_deg"] = round(_cat25__s32(data[pos:pos + 4]) * scale, 7)
            msg["lon_deg"] = round(_cat25__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
        elif frn == 12:                 # I025/610 Height of System Reference Point
            if pos + 2 > len(data): break
            msg["height_m"] = round(_cat25__s16(data[pos:pos + 2]) * 0.25, 2); pos += 2
        else:
            break
    return msg if msg else None


def _cat25__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat25_CAT_025:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat25__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat25__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat25__raw_frame_payload(bytes(sample.payload), _cat25_CAT_025)
        except ValueError as exc:
            print("CAT-25 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-25 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat25__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat25__process_stream(_cat25_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-25 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-25 TCP disconnected: {}".format(addr), flush=True)


def _cat25__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat25__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-25 Ed.1.6 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-25 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat25__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-25 Ed.1.6 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat25__process_stream(_cat25_iter_frames_udp(sock), handler, verbose)


def _cat25__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat25_decode_cat025(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-25 Ed.1.6"
        topic = _cat25_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat25Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-25 {} status={}".format(topic, msg.get("overall_status")), flush=True)
    return _h


def _cat25_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-025 Ed.1.6 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT25_PORT", "50025") or 50025))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT25_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT25_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT25_INPUT_TOPIC", _cat25_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT25_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat25__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat25__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat25__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-032 — Miniplan Reports to an SDPS, Ed.1.2
#
# Flight-plan/track-number correlation traffic between an FPPS (Flight Plan
# Processing System) and an SDPS (Surveillance Data Processing System): no
# position field exists anywhere in this category (it correlates a flight
# plan to a track number, it isn't a surveillance report). Like CAT-002's
# handler, publish_dual's protobuf (binary) view silently no-ops when
# lat_deg/lon_deg are absent from the dict (its own try/except around the
# protobuf encode step) — the JSON view still publishes unconditionally, so
# this is the same accepted behaviour as every other position-less status
# category in this file, not a gap specific to this one.
#
# Full spec text read directly
# (docs/references/asterix-specs/cat032/cat-1.2.ast). I032/035's NATURE
# subfield is a `case 035/FAMILY` construct (its table depends on the
# FAMILY value decoded earlier in the same byte) — decoded by reading
# FAMILY first and selecting the matching table, not guessed.
# ==========================================================================

_cat32_TOPIC_ROOT = topic_root()

_cat32_TOPIC_SENSOR    = _cat32_TOPIC_ROOT + "/air/{source}/fpps/civ/miniplan"
_cat32_RAW_INPUT_TOPIC = "{}/raw/asterix/cat32".format(_cat32_TOPIC_ROOT)
_cat32_CAT_032 = 32

_cat32__FAMILY = {0: "invalid", 1: "fpps", 2: "suc_fdps"}

_cat32__NATURE_FPPS = {
    0: "invalid", 1: "initial_correlation", 2: "miniplan_update",
    3: "end_of_correlation", 4: "miniplan_cancellation", 5: "retained_miniplan",
}
_cat32__NATURE_SUC = {
    0: "invalid", 1: "initial_suc_correlation", 2: "end_of_suc_correlation",
    3: "change_of_suc_correlation",
}

_cat32__GATOAT = {0: "unknown", 1: "gat", 2: "oat", 3: "not_applicable"}
_cat32__FR = {0: "ifr", 1: "vfr", 2: "not_applicable", 3: "cvfr"}
_cat32__WTC = {76: "light", 77: "medium", 72: "heavy", 74: "super"}
_cat32__IFI_TYPE = {0: "plan_number", 1: "unit1_internal", 2: "unit2_internal", 3: "unit3_internal"}
_cat32__RVSM = {0: "unknown", 1: "approved", 2: "exempt", 3: "not_approved"}
_cat32__TOD_TYPE = {
    0: "scheduled_off_block", 1: "estimated_off_block", 2: "estimated_takeoff",
    3: "actual_off_block", 4: "predicted_runway_hold", 5: "actual_runway_hold",
    6: "actual_line_up", 7: "actual_takeoff", 8: "estimated_arrival",
    9: "predicted_landing", 10: "actual_landing", 11: "actual_off_runway",
    12: "predicted_gate", 13: "actual_on_block",
}
_cat32__DAY = {0: "today", 1: "yesterday", 2: "tomorrow", 3: "invalid"}
_cat32__STAND_STATUS = {0: "empty", 1: "occupied", 2: "unknown", 3: "invalid"}
_cat32__STAND_AVAIL = {0: "available", 1: "not_available", 2: "unknown", 3: "invalid"}


def _cat32__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat32__netbird_ip() -> str:
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


def _cat32__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat32_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat32__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat32__recv_exact(sock, length - 3)
        record_in("cat32", len(data))
        yield cat, data


def _cat32_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat32", len(record))
            yield cat, record
            offset += length


def _cat32_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat32__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat32__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]


def _cat32__presence(data: bytes, pos: int, maximum: int) -> tuple[list[bool], int]:
    present: list[bool] = []
    for _ in range(maximum):
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 1:
            return present, pos
    if data[pos - 1] & 1:
        raise ValueError("unsupported ASTERIX compound presence extension")
    return present, pos


def _cat32__decode_500(data: bytes, pos: int) -> tuple[dict, int]:
    """I032/500 Supplementary Flight Data: compound, up to 2 presence bytes."""
    out: dict = {}
    flags, pos = _cat32__presence(data, pos, 2)
    names = ("ifi", "rvp", "rds", "tod", "ast", "sts", "sid", "star")
    for index, name in enumerate(names):
        if index >= len(flags) or not flags[index]: continue
        if name == "ifi":
            if pos + 4 > len(data): return out, len(data)
            v = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
            out["ifps_id_type"] = _cat32__IFI_TYPE.get((v >> 30) & 0x03, "unknown")
            out["ifps_id_number"] = v & 0x07FFFFFF
        elif name == "rvp":
            if pos >= len(data): return out, len(data)
            b = data[pos]; pos += 1
            out["rvsm"] = _cat32__RVSM.get((b >> 1) & 0x03, "unknown")
            if b & 0x01: out["high_priority_flight"] = True
        elif name == "rds":
            if pos + 3 > len(data): return out, len(data)
            rwy = data[pos:pos + 3].decode("ascii", "replace").strip("\x00 "); pos += 3
            if rwy: out["runway"] = rwy
        elif name == "tod":
            if pos >= len(data): return out, len(data)
            rep = data[pos]; pos += 1
            entries = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                v = int.from_bytes(data[pos:pos + 4], "big"); pos += 4
                entries.append({
                    "type": _cat32__TOD_TYPE.get((v >> 27) & 0x1F, "unknown"),
                    "day": _cat32__DAY.get((v >> 25) & 0x03, "unknown"),
                    "hour": (v >> 16) & 0x1F,
                    "minute": (v >> 8) & 0x3F,
                    "seconds_available": not bool((v >> 7) & 0x01),
                    "second": v & 0x3F,
                })
            if entries: out["times_of_departure_arrival"] = entries
        elif name == "ast":
            if pos + 6 > len(data): return out, len(data)
            s = data[pos:pos + 6].decode("ascii", "replace").strip("\x00 "); pos += 6
            if s: out["aircraft_stand"] = s
        elif name == "sts":
            if pos >= len(data): return out, len(data)
            b = data[pos]; pos += 1
            out["stand_status"] = _cat32__STAND_STATUS.get((b >> 6) & 0x03, "unknown")
            out["stand_availability"] = _cat32__STAND_AVAIL.get((b >> 4) & 0x03, "unknown")
        elif name == "sid":
            if pos + 7 > len(data): return out, len(data)
            s = data[pos:pos + 7].decode("ascii", "replace").strip("\x00 "); pos += 7
            if s: out["sid"] = s
        elif name == "star":
            if pos + 7 > len(data): return out, len(data)
            s = data[pos:pos + 7].decode("ascii", "replace").strip("\x00 "); pos += 7
            if s: out["star"] = s
    return out, pos


def _cat32_decode_cat032(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat32_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I032/010 Server Identification Tag
            if pos + 2 > len(data): break
            msg["server_sac"] = data[pos]; msg["server_sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I032/015 User Number
            if pos + 2 > len(data): break
            msg["user_number"] = _cat32__u16(data[pos:pos + 2]); pos += 2
        elif frn == 2:                  # I032/018 Data Source Identification Tag
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 3:                  # I032/035 Type of Message (NATURE depends on FAMILY)
            if pos >= len(data): break
            b = data[pos]; pos += 1
            family_code = (b >> 4) & 0x0F
            nature_code = b & 0x0F
            msg["family"] = _cat32__FAMILY.get(family_code, "family_{}".format(family_code))
            if family_code == 1: msg["nature"] = _cat32__NATURE_FPPS.get(nature_code, "nature_{}".format(nature_code))
            elif family_code == 2: msg["nature"] = _cat32__NATURE_SUC.get(nature_code, "nature_{}".format(nature_code))
            else: msg["nature_raw"] = nature_code
        elif frn == 4:                  # I032/020 Time of ASTERIX Report Generation
            if pos + 3 > len(data): break
            msg["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 5:                  # I032/040 Track Number
            if pos + 2 > len(data): break
            msg["track_num"] = _cat32__u16(data[pos:pos + 2]); pos += 2
        elif frn == 6:                  # I032/050 Composed Track Number (FX, 3B/segment)
            segments = []
            while True:
                if pos + 3 > len(data): break
                v = int.from_bytes(data[pos:pos + 3], "big"); pos += 3
                segments.append({"system_unit_id": (v >> 16) & 0xFF, "system_track_num": (v >> 1) & 0x7FFF})
                if not (v & 0x01): break
            if segments: msg["composed_track_numbers"] = segments
        elif frn == 7:                  # I032/060 Track Mode 3/A
            if pos + 2 > len(data): break
            msg["squawk"] = "{:04o}".format(_cat32__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 8:                  # I032/400 Callsign
            if pos + 7 > len(data): break
            callsign = data[pos:pos + 7].decode("ascii", "replace").strip("\x00 "); pos += 7
            if callsign: msg["callsign"] = callsign
        elif frn == 9:                  # I032/410 Plan Number
            if pos + 2 > len(data): break
            msg["plan_number"] = _cat32__u16(data[pos:pos + 2]); pos += 2
        elif frn == 10:                 # I032/420 Flight Category
            if pos >= len(data): break
            b = data[pos]; pos += 1
            msg["gat_oat"] = _cat32__GATOAT.get((b >> 6) & 0x03, "unknown")
            msg["flight_rules"] = _cat32__FR.get((b >> 4) & 0x03, "unknown")
            msg["special_priority_raw"] = (b >> 1) & 0x07
        elif frn == 11:                 # I032/440 Departure Aerodrome
            if pos + 4 > len(data): break
            s = data[pos:pos + 4].decode("ascii", "replace").strip("\x00 "); pos += 4
            if s: msg["departure_airport"] = s
        elif frn == 12:                 # I032/450 Destination Aerodrome
            if pos + 4 > len(data): break
            s = data[pos:pos + 4].decode("ascii", "replace").strip("\x00 "); pos += 4
            if s: msg["destination_airport"] = s
        elif frn == 13:                 # I032/480 Current Cleared Flight Level
            if pos + 2 > len(data): break
            msg["cleared_flight_level"] = round(_cat32__u16(data[pos:pos + 2]) * 0.25, 2); pos += 2
        elif frn == 14:                 # I032/490 Current Control Position
            if pos + 2 > len(data): break
            msg["control_centre"] = data[pos]; msg["control_position"] = data[pos + 1]; pos += 2
        elif frn == 15:                 # I032/430 Type of Aircraft
            if pos + 4 > len(data): break
            s = data[pos:pos + 4].decode("ascii", "replace").strip("\x00 "); pos += 4
            if s: msg["aircraft_type"] = s
        elif frn == 16:                 # I032/435 Wake Turbulence Category
            if pos >= len(data): break
            msg["wake_turbulence_cat"] = _cat32__WTC.get(data[pos], "code_{}".format(data[pos])); pos += 1
        elif frn == 17:                 # I032/460 Allocated SSR Codes (repetitive 1, 2B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            codes = []
            for _ in range(rep):
                if pos + 2 > len(data): break
                v = _cat32__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
                codes.append("{:04o}".format(v))
            if codes: msg["allocated_ssr_codes"] = codes
        elif frn == 18:                 # I032/500 Supplementary Flight Data
            try:
                extra, pos = _cat32__decode_500(data, pos); msg.update(extra)
            except ValueError:
                break
        elif frn == 19:                 # spare (no encoded item)
            continue
        elif frn == 20:                 # I032/RE
            pos = _cat32__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat32__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat32_CAT_032:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat32__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat32__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat32__raw_frame_payload(bytes(sample.payload), _cat32_CAT_032)
        except ValueError as exc:
            print("CAT-32 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-32 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat32__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat32__process_stream(_cat32_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-32 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-32 TCP disconnected: {}".format(addr), flush=True)


def _cat32__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat32__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-32 Ed.1.2 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-32 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat32__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-32 Ed.1.2 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat32__process_stream(_cat32_iter_frames_udp(sock), handler, verbose)


def _cat32__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat32_decode_cat032(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-32 Ed.1.2"
        topic = _cat32_TOPIC_SENSOR.format(source=_asterix_source({"sac": msg.get("server_sac", 0), "sic": msg.get("server_sic", 0)}))
        publish_dual(session, topic, msg, AsterixCat32Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-32 {} nature={}".format(topic, msg.get("nature", msg.get("nature_raw"))), flush=True)
    return _h


def _cat32_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-032 Ed.1.2 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT32_PORT", "50032") or 50032))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT32_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT32_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT32_INPUT_TOPIC", _cat32_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT32_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat32__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat32__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat32__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-034
# ==========================================================================

import argparse

import math

import os

import struct

import threading

import time

_cat34_TOPIC_ROOT = topic_root()

# The observer and the observed are both a radar here: CAT-034 reports the
# radar's own service status, so the radar is the tracked object.
_cat34_TOPIC_SENSOR = _cat34_TOPIC_ROOT + "/land/{source}/radar/neutral/radar"
_cat34_RAW_INPUT_TOPIC = "{}/raw/asterix/cat34".format(_cat34_TOPIC_ROOT)

_cat34_CAT_034 = 0x22

_cat34__MSG_TYPES_034 = {1: "north_marker", 2: "sector_crossing",
                  3: "geo_filter",   4: "jamming_strobe"}

_cat34__COUNT_LABELS = {
    0: "no_detection", 1: "psr",        2: "ssr",         3: "psr_ssr",
    4: "all",          5: "no_det_psr", 6: "no_det_ssr",  7: "mode5",
    11: "mil_id",
}

def _cat34__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0

def _cat34__coverage_range_m(msg: dict, configured_range_m: float = 0.0) -> tuple[int | None, str | None]:
    """Return (range_m, source): an advertised CAT-034 range first, then an
    explicit operator fallback. ``source`` is ``"advertised"`` (decoded from
    I034/100), ``"configured"`` (operator instrumented maximum — NOT measured
    coverage), or ``None`` when neither is available. Provenance travels with the
    range so the C2 card can label a configured ring honestly."""
    try:
        advertised_nm = float(msg.get("coverage_rho_end_nm") or 0.0)
    except (TypeError, ValueError):
        advertised_nm = 0.0
    if math.isfinite(advertised_nm) and advertised_nm > 0:
        return round(advertised_nm * 1852.0), "advertised"
    if math.isfinite(configured_range_m) and configured_range_m > 0:
        return round(configured_range_m), "configured"
    return None, None

def _cat34_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat34__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat34__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat34__s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

def _cat34__decode_i034_050(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/050 System Configuration and Status — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    primary = []
    while pos < len(data):
        sf = data[pos]; pos += 1
        primary.append(sf)
        if not sf & 0x01:
            break
    if not primary:
        return out, pos
    sf = primary[0]
    if sf & 0x80:
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        out["sys_nogo"] = bool(com & 0x80)
        out["rdp_chain_2"] = bool(com & 0x40)
        out["rdp_restart"] = bool(com & 0x20)
        out["sys_ovl_rdp"] = bool(com & 0x10)
        out["sys_ovl_xmt"] = bool(com & 0x08)
        out["sys_msc_connected"] = not bool(com & 0x04)
        out["sys_tsv_invalid"] = bool(com & 0x02)
    channel_names = ("none", "a", "b", "diversity")
    for prefix, mask, size in (("psr", 0x10, 1), ("ssr", 0x08, 1),
                               ("mds", 0x04, 2)):
        if sf & mask:
            if pos + size > len(data): return out, len(data)
            first = data[pos]; pos += 1
            out[prefix + "_antenna"] = 2 if first & 0x80 else 1
            out[prefix + "_channel"] = channel_names[(first >> 5) & 0x03]
            overload = bool(first & 0x10)
            msc_disconnected = bool(first & 0x08)
            if size == 2:
                second = data[pos]
                pos += 1
                out["mds_scf_channel"] = 2 if first & 0x04 else 1
                out["mds_dlf_channel"] = 2 if first & 0x02 else 1
                out["mds_scf_overload"] = bool(first & 0x01)
                out["mds_dlf_overload"] = bool(second & 0x80)
            out[prefix + "_status"] = "overload" if overload else "operational"
            out[prefix + "_overload"] = overload
            out[prefix + "_msc_connected"] = not msc_disconnected
    return out, pos

def _cat34__decode_i034_060(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/060 System Processing Mode — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    primary = []
    while pos < len(data):
        sf = data[pos]; pos += 1
        primary.append(sf)
        if not sf & 0x01:
            break
    if not primary:
        return out, pos
    sf = primary[0]
    if sf & 0x80:               # COM sub-field
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        out["rdp_reduction_level"] = (com >> 4) & 0x07
        out["xmt_reduction_level"] = (com >> 1) & 0x07
    if sf & 0x10:               # PSR sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        out["psr_polarization"]    = "circular" if (b & 0x80) else "linear"
        out["psr_reduction_level"] = (b >> 4) & 0x07
        out["psr_stc_map"] = (b >> 2) & 0x03
    if sf & 0x08:               # SSR sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        out["ssr_reduction_level"] = (b >> 5) & 0x07
    if sf & 0x04:               # MDS sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        out["mds_reduction_level"] = (b >> 5) & 0x07
        if b & 0x10: out["mds_cluster_state"] = True
    return out, pos

def _cat34__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)

def _cat34_decode_cat034(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat34_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I034/010  SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I034/000  Message Type
            if pos + 1 > len(data): break
            msg["msg_type"] = _cat34__MSG_TYPES_034.get(data[pos], data[pos]); pos += 1
        elif frn == 2:                  # I034/030  Time of Day (1/128 s)
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I034/020  Sector Number (360/256 °)
            if pos + 1 > len(data): break
            msg["sector_deg"] = round(data[pos] * 360.0 / 256.0, 2); pos += 1
        elif frn == 4:                  # I034/041  Antenna Rotation Period (1/128 s)
            if pos + 2 > len(data): break
            msg["rotation_s"] = round(_cat34__u16(data[pos:pos+2]) / 128.0, 2); pos += 2
        elif frn == 5:                  # I034/050  System Configuration (compound)
            extra, pos = _cat34__decode_i034_050(data, pos)
            msg.update(extra)
        elif frn == 6:                  # I034/060  System Processing Mode (compound)
            extra, pos = _cat34__decode_i034_060(data, pos)
            msg.update(extra)
        elif frn == 7:                  # I034/070  Message Count Values (REP × 2 bytes)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            counts = {}
            for _ in range(rep):
                if pos + 2 > len(data): break
                word = _cat34__u16(data[pos:pos+2]); pos += 2
                counts[_cat34__COUNT_LABELS.get((word >> 11) & 0x1F,
                                          "type{}".format((word >> 11) & 0x1F))] = word & 0x7FF
            if counts: msg["msg_counts"] = counts
        elif frn == 8:                  # I034/100  Generic Polar Window (8 bytes)
            if pos + 8 > len(data): break
            msg["coverage_rho_start_nm"] = round(_cat34__u16(data[pos:pos+2]) / 256.0, 3)
            msg["coverage_rho_end_nm"]   = round(_cat34__u16(data[pos+2:pos+4]) / 256.0, 3)
            msg["coverage_az_start_deg"] = round(_cat34__u16(data[pos+4:pos+6]) * 360.0 / 65536.0, 2)
            msg["coverage_az_end_deg"]   = round(_cat34__u16(data[pos+6:pos+8]) * 360.0 / 65536.0, 2)
            pos += 8
        elif frn == 9:                  # I034/110  Data Filter (1 byte)
            if pos + 1 > len(data): break
            _FILT034 = {0: "invalid", 1: "weather", 2: "jamming", 3: "psr",
                        4: "ssr_mode_s", 5: "combined", 6: "enhanced_surveillance",
                        7: "psr_enhanced", 8: "psr_enhanced_ssr_not_aoi", 9: "all"}
            msg["data_filter"] = _FILT034.get(data[pos], "type_{}".format(data[pos]))
            pos += 1
        elif frn == 10:                 # I034/120 3D Position of Data Source (fixed 8 bytes)
            if pos + 8 > len(data): break
            msg["site_alt_m"] = _cat34__s16(data[pos:pos + 2])
            msg["site_lat"] = round(_cat34__s24(data[pos + 2:pos + 5]) * (180.0 / 2**23), 7)
            msg["site_lon"] = round(_cat34__s24(data[pos + 5:pos + 8]) * (180.0 / 2**23), 7)
            pos += 8
        elif frn == 11:                 # I034/090 Collimation Error (fixed 2 bytes)
            if pos + 2 > len(data): break
            msg["collimation_rng_nm"] = round(struct.unpack_from("b", data, pos)[0] / 128.0, 4)
            msg["collimation_az_deg"] = round(struct.unpack_from("b", data, pos + 1)[0] * 360.0 / 16384.0, 4)
            pos += 2
        elif frn in (12, 13):           # RE / SP
            pos = _cat34__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None

def _cat34__make_cat034_handler(pub_sensor, site, radar_name, configured_range_m=0.0):
    # A configured site is a fallback for feeds that omit I034/120. Live
    # positions are stored per SAC/SIC: one process may receive several radar
    # heads, and a single mutable site would make them overwrite each other.
    default_site = (
        (float(site[0]), float(site[1]))
        if site[0] is not None and site[1] is not None
        else None
    )
    sites:          dict[str, tuple[float, float]] = {}
    missing_sites:  set[str] = set()
    _first_seen:   dict[str, float] = {}
    _sweep:        dict[str, dict]  = {}   # key → {north_ts, rotation_s, status}
    _sweep_lock    = threading.Lock()
    _sweep_active: set[str]         = set()
    _keepalive:    dict[str, dict]  = {}   # key → last full status
    _keepalive_active: set[str]     = set()
    _ka_lock       = threading.Lock()
    _pos_hist:     dict[str, tuple] = {}   # key → (ts, lat, lon) for speed/course
    _ranges:       dict[str, int] = {}     # key → last advertised/configured range
    _range_sources: dict[str, str] = {}    # key → "advertised" | "configured"
    KEEPALIVE_S    = 60   # republish site marker every 60 s so ATAK never loses it

    def _keepalive_thread(key: str):
        """Republish the last known full status every KEEPALIVE_S seconds.
        This keeps the ATAK radar site marker alive even when the radar is offline."""
        while True:
            time.sleep(KEEPALIVE_S)
            with _ka_lock:
                status = _keepalive.get(key)
            if status is None:
                return
            payload = dict(status)
            payload["_ts"] = time.time()
            publish_dual(
                pub_sensor,
                _cat34_TOPIC_SENSOR.format(source=_asterix_source(payload)),
                payload,
                AsterixCat34Status,
                wrapper_field="sensor",
            )

    def _sweep_thread(key: str):
        """Publish radar beam CoT at 5 Hz using dead-reckoned antenna azimuth."""
        while True:
            time.sleep(0.2)
            with _sweep_lock:
                s = _sweep.get(key)
            if s is None:
                return
            rot = s.get("rotation_s")
            if not rot:
                continue
            # Stop animating if no north marker for > 3 rotations (radar offline)
            if time.time() - s["north_ts"] > rot * 3:
                continue
            az = (time.time() - s["north_ts"]) / rot * 360.0 % 360.0
            payload = dict(s["status"])
            payload["sweep_azimuth_deg"] = round(az, 1)
            payload["_ts"] = time.time()
            publish_dual(
                pub_sensor,
                _cat34_TOPIC_SENSOR.format(source=_asterix_source(payload)),
                payload,
                AsterixCat34Status,
                wrapper_field="sensor",
            )

    def _h(data: bytes, verbose: bool):
        msg = _cat34_decode_cat034(data)
        if not msg:
            return
        mtype = msg.get("msg_type", "?")
        tod   = msg.get("tod_s")
        rot   = msg.get("rotation_s")
        if verbose:
            print("CAT-034 {} tod={} rot={}s psr={} ssr={} mds={} "
                  "cal_az={} cal_rng={}".format(
                mtype,
                "{:.2f}".format(tod) if tod else "-",
                "{:.2f}".format(rot) if rot else "-",
                msg.get("psr_status", "-"), msg.get("ssr_status", "-"),
                msg.get("mds_status", "-"),
                msg.get("collimation_az_deg", "-"),
                msg.get("collimation_rng_nm", "-"),
            ), flush=True)

        if not pub_sensor:
            return

        sac = msg.get("sac", 0); sic = msg.get("sic", 0)
        key = "{}-{}".format(sac, sic)
        # Self-configure each radar independently from I034/120. VERA-NG and
        # other multi-sensor feeds commonly multiplex several SAC/SIC sources.
        if msg.get("site_lat") is not None and msg.get("site_lon") is not None:
            sites[key] = (float(msg["site_lat"]), float(msg["site_lon"]))
            missing_sites.discard(key)
        active_site = sites.get(key, default_site)
        if active_site is None:
            if key not in missing_sites:
                print(
                    "CAT-034 SAC{}/SIC{} has no site position; "
                    "send I034/120 or configure CAT34_RADAR_LAT/LON".format(sac, sic),
                    flush=True,
                )
                missing_sites.add(key)
            return
        site_lat, site_lon = active_site

        now = time.time()
        range_m, range_source = _cat34__coverage_range_m(msg, configured_range_m)
        if range_m is not None:
            _ranges[key] = range_m
            _range_sources[key] = range_source

        if mtype == "north_marker":
            _first_seen.setdefault(key, now)

            # Compute speed and course from successive position reports (mobile platform support)
            speed_ms = heading_deg = None
            prev = _pos_hist.get(key)
            if prev:
                dt = now - prev[0]
                if 0 < dt < 3600:
                    dlat = (site_lat - prev[1]) * 111320.0
                    dlon = (site_lon - prev[2]) * 111320.0 * math.cos(math.radians(site_lat))
                    dist_m = math.hypot(dlat, dlon)
                    speed_ms = round(dist_m / dt, 2)
                    if dist_m > 1.0:
                        heading_deg = round((math.degrees(math.atan2(dlon, dlat)) + 360) % 360, 1)
            _pos_hist[key] = (now, site_lat, site_lon)

            status = {
                "_src":        "ASTERIX CAT-34 Ed.1.29",
                "_ts":         now,
                "sensor_type": "radar",
                "sensor_id":   "CAT34-{}-{}".format(sac, sic),
                "sensor_name": radar_name or "RADAR SAC{}/SIC{}".format(sac, sic),
                "lat_deg":     site_lat,
                "lon_deg":     site_lon,
                "online_since": _first_seen[key],
            }
            if key in _ranges:
                status["radar_range_m"] = _ranges[key]
                if _range_sources.get(key):
                    status["radar_range_source"] = _range_sources[key]
            if speed_ms is not None:
                status["speed_ms"]    = speed_ms
            if heading_deg is not None:
                status["heading_deg"] = heading_deg
            for k, v in msg.items():
                if k == "tod_s":
                    status["radar_clock_s"] = v
                else:
                    status[k] = v

            with _sweep_lock:
                existing = _sweep.get(key, {})
                _sweep[key] = {
                    "north_ts":   now,
                    "rotation_s": rot or existing.get("rotation_s", 4.0),
                    "status":     status,
                }
                start_thread = key not in _sweep_active
                _sweep_active.add(key)

            # Update keepalive store and start keepalive thread on first north marker
            with _ka_lock:
                _keepalive[key] = status
                start_ka = key not in _keepalive_active
                _keepalive_active.add(key)

            # Publish the full status update (no sweep azimuth — just the site marker)
            publish_dual(
                pub_sensor,
                _cat34_TOPIC_SENSOR.format(source=_asterix_source(status)),
                status,
                AsterixCat34Status,
                wrapper_field="sensor",
            )

            if start_thread:
                threading.Thread(target=_sweep_thread, args=(key,),
                                  daemon=True).start()
            if start_ka:
                threading.Thread(target=_keepalive_thread, args=(key,),
                                  daemon=True).start()

        elif mtype == "sector_crossing":
            # Re-sync virtual north_ts so azimuth stays accurate between north markers
            sector_deg = msg.get("sector_deg", 0.0)
            with _sweep_lock:
                s = _sweep.get(key)
                if s and s.get("rotation_s"):
                    s["north_ts"] = now - sector_deg / 360.0 * s["rotation_s"]

    return _h

def _cat34_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-034 Ed.1.29 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT34_PORT", "50034") or 50034))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT34_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT34_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT34_INPUT_TOPIC", _cat34_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat34__env_float("CAT34_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat34__env_float("CAT34_RADAR_LON"))
    parser.add_argument("--site-name", default=os.environ.get("CAT34_RADAR_NAME", ""))
    parser.add_argument(
        "--radar-range-m",
        type=float,
        default=_cat34__env_float("CAT34_RADAR_RANGE_M"),
        help="operator-confirmed fallback range; CAT-034 I034/100 takes precedence",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT34_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = open_session()
    handler = _cat34__make_cat034_handler(
        session, site, args.site_name or None, args.radar_range_m
    )
    try:
        if args.zenoh_raw: run_zenoh_raw(session, args.input_topic, _cat34_CAT_034, handler, args.verbose)
        else: run_inbound(args.port, args.tcp, "CAT-34 Ed.1.29", "cat34", {_cat34_CAT_034: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat034 = _cat34_decode_cat034


# ==========================================================================
# CAT-048
# ==========================================================================

import argparse

import json

import math

import os

import socket

import struct

import threading

import time

from namespace_prefix import topic_root
from protocols.track_views import (publish_dual, publish_native, native_topic,
                                      semantic_topic, asterix_data_block)
from protocols.data_stats import record_in



_cat48_TOPIC_ROOT = topic_root()




_cat48_TOPIC_048    = _cat48_TOPIC_ROOT + "/air/{source}/radar/unknown/aircraft"
_cat48_RAW_INPUT_TOPIC = "{}/raw/asterix/cat48".format(_cat48_TOPIC_ROOT)
_cat48_SITE_INPUT_TOPIC = (
    _cat48_TOPIC_ROOT + "/land/*/radar/neutral/radar/*/*/tracks/v1"
)

_cat48_CAT_048 = 0x30

_cat48__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

def _cat48__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0

def _cat48__netbird_ip() -> str:
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

def _cat48__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat48_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat48__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat48__recv_exact(sock, length - 3)
        record_in("cat48", len(data))
        yield cat, data

def _cat48_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat48", len(record))
            yield cat, record
            offset += length

def _cat48_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat48__skip_len_field(data: bytes, pos: int) -> int:
    """Skip an ASTERIX length-prefixed RE/SP item (length includes itself)."""
    if pos >= len(data):
        return len(data)
    length = data[pos]
    if length < 1 or pos + length > len(data):
        return len(data)
    return pos + length

def _cat48__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat48__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat48__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat48__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat48__decode_bds50(mb: bytes) -> dict:
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
    if _bit(24): out["bds_gs_kt"]        = round(_uns(25, 34) * 2.0, 0)
    if _bit(35): out["track_rate_degs"]  = round(_sgn(36, 45) * 8.0 / 256.0, 2)
    if _bit(46): out["tas_kt"]           = round(_uns(47, 56) * 2.0, 0)
    return out

def _cat48__decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
    if _bit(13): out["ias_kt"]       = _uns(14, 23)
    if _bit(24): out["mach"]         = round(_uns(25, 34) * 2.048 / 512.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 45) * 32
    if _bit(46): out["ivv_fpm"]      = _sgn(47, 56) * 32
    return out

def _cat48__gillham_to_ft(code: int) -> int | None:
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

def _cat48__decode_bds40(mb: bytes) -> dict:
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

# BDS 1,0/1,7 bit layouts: pyModeS src/pyModeS/decoder/bds/bds10.py and
# bds17.py (github.com/junzis/pyModeS) — see docs/references/ASTERIX.md.
def _cat48__decode_bds10(mb: bytes) -> dict:
    """BDS 1,0 Data Link Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    return {
        "dlc_continuation_flag": bool(_bit(9)),
        "overlay_command_capability": bool(_bit(15)),
        "acas_operational": bool(_bit(16)),
        "mode_s_subnetwork_version": _uns(17, 23),
        "transponder_level5": bool(_bit(24)),
        "mode_s_specific_services": bool(_bit(25)),
        "uplink_elm_throughput": _uns(26, 28),
        "downlink_elm_throughput": _uns(29, 32),
        "aircraft_id_capability": bool(_bit(33)),
        "squitter_capability": bool(_bit(34)),
        "surveillance_id_capability": bool(_bit(35)),
        "common_usage_gicb_capability": bool(_bit(36)),
        "acas_hybrid_surveillance": bool(_bit(37)),
        "acas_resolution_advisory": bool(_bit(38)),
        "acas_rtca_version": _uns(39, 40),
        "dte_status": _uns(41, 56),
    }

_cat48__BDS17_CAPABILITY = (
    "0,5", "0,6", "0,7", "0,8", "0,9", "0,A", "2,0", "2,1",
    "4,0", "4,1", "4,2", "4,3", "4,4", "4,5", "4,8", "5,0",
    "5,1", "5,2", "5,3", "5,4", "5,5", "5,6", "5,F", "6,0",
)

def _cat48__decode_bds17(mb: bytes) -> dict:
    """BDS 1,7 Common Usage GICB Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    return {"supported_bds": [code for i, code in enumerate(_cat48__BDS17_CAPABILITY)
                               if (v >> (56 - (i + 1))) & 1]}


def _cat48__decode_bds30(mb: bytes) -> dict:
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

def _cat48__polar_to_wgs84(radar_lat: float, radar_lon: float,
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


def _cat48__cache_cat34_site(
    track: dict,
    sites: dict[tuple[int, int], tuple[float, float]],
) -> bool:
    """Cache a CAT-34 radar position for CAT-48 polar geolocation."""
    if not str(track.get("_src", "")).startswith("ASTERIX CAT-34"):
        return False
    try:
        sac = int(track["sac"])
        sic = int(track["sic"])
        lat = float(track["lat_deg"])
        lon = float(track["lon_deg"])
    except (KeyError, TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    sites[(sac, sic)] = (lat, lon)
    return True


def _cat48__geolocate_from_site(
    track: dict,
    default_site: list[float | None],
    sites: dict[tuple[int, int], tuple[float, float]],
) -> bool:
    """Add WGS-84 coordinates to a polar CAT-48 plot when a site is known."""
    if "lat_deg" in track and "lon_deg" in track:
        return True
    try:
        range_nm = float(track["range_nm"])
        azimuth_deg = float(track["azimuth_deg"])
    except (KeyError, TypeError, ValueError):
        return False
    if range_nm <= 0:
        return False
    site = sites.get((int(track.get("sac", 0)), int(track.get("sic", 0))))
    if site is None and None not in default_site:
        site = (float(default_site[0]), float(default_site[1]))
    if site is None:
        return False
    lat, lon = _cat48__polar_to_wgs84(site[0], site[1], range_nm, azimuth_deg)
    track["lat_deg"] = round(lat, 6)
    track["lon_deg"] = round(lon, 6)
    return True

def _cat48__compound_presence(data: bytes, pos: int) -> tuple[list[bool], int]:
    """Read all primary-subfield octets before any compound payload."""
    present: list[bool] = []
    while True:
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 0x01:
            return present, pos

def _cat48_decode_cat048_record(data: bytes, pos: int,
                         radar_lat: float | None, radar_lon: float | None):
    """Decode one CAT-048 record. Returns (track_dict, new_pos)."""
    fspec, pos = _cat48_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-48 Ed.1.32"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I048/010  SAC/SIC
            if pos + 2 > len(data): return track, len(data)
            track["sac"] = data[pos]; track["sic"] = data[pos+1]; pos += 2
        elif frn == 1:                  # I048/140  Time of Day (1/128 s)
            if pos + 3 > len(data): return track, len(data)
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            track["tod_s"] = raw / 128.0; pos += 3
        elif frn == 2:                  # I048/020  Target Report Descriptor (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            typ = (b >> 5) & 0x07
            _TYP048 = ("no_detection", "psr", "ssr", "ssr_psr", "mode_s_all_call",
                       "mode_s_roll_call", "mode_s_all_call_psr", "mode_s_roll_call_psr")
            track["detection_type"] = _TYP048[typ]
            if b & 0x10: track["simulated"]       = True   # SIM
            if b & 0x08: track["rdp_chain"]       = True   # RDP
            if b & 0x04: track["spi"]             = True
            if b & 0x02: track["field_monitor"] = True     # RAB
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["test_target"]    = True   # TST
                if b & 0x40: track["extended_range"] = True   # ERR
                if b & 0x20: track["x_pulse"]        = True   # XPP
                if b & 0x10: track["mil_emergency"]  = True   # ME
                if b & 0x08: track["mil_ident"]      = True   # MI
                foe = (b >> 1) & 0x03
                if foe: track["iff"] = ("", "friendly", "unknown", "no_reply")[foe]
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 3:                  # I048/040  Slant Polar Coordinates
            if pos + 4 > len(data): return track, len(data)
            range_raw = _cat48__u16(data[pos:pos+2])
            az_raw    = _cat48__u16(data[pos+2:pos+4])
            range_nm  = range_raw / 256.0
            az_deg    = az_raw * 360.0 / 65536.0
            track["range_nm"]    = round(range_nm, 3)
            track["azimuth_deg"] = round(az_deg, 3)
            if radar_lat is not None and radar_lon is not None and range_nm > 0:
                lat, lon = _cat48__polar_to_wgs84(radar_lat, radar_lon, range_nm, az_deg)
                track.setdefault("lat_deg", round(lat, 6))
                track.setdefault("lon_deg", round(lon, 6))
            pos += 4
        elif frn == 4:                  # I048/070  Mode-3/A squawk
            if pos + 2 > len(data): return track, len(data)
            w = _cat48__u16(data[pos:pos + 2]); pos += 2
            track["squawk"] = "{:04o}".format(w & 0x0FFF)
            if w & 0x8000: track["squawk_invalid"] = True
            if w & 0x4000: track["squawk_garbled"] = True
            if w & 0x2000: track["squawk_not_extracted"] = True
        elif frn == 5:                  # I048/090  Flight Level (1/4 FL, signed)
            if pos + 2 > len(data): return track, len(data)
            w = _cat48__u16(data[pos:pos + 2]); pos += 2
            raw = w & 0x3FFF
            if raw & 0x2000: raw -= 0x4000
            track["alt_baro_ft"] = raw * 25
            if w & 0x8000: track["alt_baro_invalid"] = True
            if w & 0x4000: track["alt_baro_garbled"] = True
        elif frn == 6:                  # I048/130  Radar Plot Characteristics (compound)
            try:
                present, pos = _cat48__compound_presence(data, pos)
            except ValueError:
                return track, len(data)
            if any(present[7:]):
                return track, len(data)
            names = (
                "ssr_runlength_deg",
                "ssr_reply_count",
                "ssr_amplitude_dbm",
                "psr_runlength_deg",
                "psr_amplitude_dbm",
                "psr_ssr_range_diff_nm",
                "psr_ssr_az_diff_deg",
            )
            for index, name in enumerate(names):
                if not present[index]:
                    continue
                if pos >= len(data):
                    return track, len(data)
                value = data[pos]; pos += 1
                if index in (0, 3):
                    track[name] = round(value * 360.0 / 8192.0, 3)
                elif index in (2, 4):
                    track[name] = struct.unpack("b", bytes((value,)))[0]
                elif index == 5:
                    track[name] = round(struct.unpack("b", bytes((value,)))[0] / 256.0, 4)
                elif index == 6:
                    track[name] = round(
                        struct.unpack("b", bytes((value,)))[0] * 360.0 / 16384.0,
                        4,
                    )
                else:
                    track[name] = value
        elif frn == 7:                  # I048/220  Aircraft Address (ICAO 24-bit)
            if pos + 3 > len(data): return track, len(data)
            addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            track["icao24"] = "{:06x}".format(addr); pos += 3
        elif frn == 8:                  # I048/240  Aircraft Identification
            if pos + 6 > len(data): return track, len(data)
            track["callsign"] = _cat48__decode_callsign(data[pos:pos+6]); pos += 6
        elif frn == 9:                  # I048/250  Mode-S MB Data (REP × 8-byte records)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): break
                mb   = bytes(data[pos:pos + 7])
                bds  = data[pos + 7]; pos += 8
                bds1 = (bds >> 4) & 0x0F; bds2 = bds & 0x0F
                if   bds1 == 1 and bds2 == 0: track.update(_cat48__decode_bds10(mb))
                elif bds1 == 1 and bds2 == 7: track.update(_cat48__decode_bds17(mb))
                elif bds1 == 3 and bds2 == 0: track.update(_cat48__decode_bds30(mb))
                elif bds1 == 4 and bds2 == 0: track.update(_cat48__decode_bds40(mb))
                elif bds1 == 5 and bds2 == 0: track.update(_cat48__decode_bds50(mb))
                elif bds1 == 6 and bds2 == 0: track.update(_cat48__decode_bds60(mb))
        elif frn == 10:                 # I048/161  Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat48__u16(data[pos:pos+2]) & 0x0FFF; pos += 2
        elif frn == 11:                 # I048/042  Cartesian Position (1/128 NM, s16×2)
            if pos + 4 > len(data): return track, len(data)
            track["cart_x_nm"] = round(_cat48__s16(data[pos:pos+2]) / 128.0, 3)
            track["cart_y_nm"] = round(_cat48__s16(data[pos+2:pos+4]) / 128.0, 3)
            pos += 4
        elif frn == 12:                 # I048/200  Track Velocity Polar
            if pos + 4 > len(data): return track, len(data)
            spd_raw = _cat48__u16(data[pos:pos+2])
            hdg_raw = _cat48__u16(data[pos+2:pos+4])
            track["speed_ms"]    = round(spd_raw * 1852.0 / 16384.0, 2)   # LSB = 2^-14 NM/s
            track["heading_deg"] = round(hdg_raw * 360.0 / 65536.0, 2)
            pos += 4
        elif frn == 13:                 # I048/170  Track Status (FX)
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["confirmed"] = not bool(b & 0x80)
            if b & 0x80: track["track_tentative"] = True
            rad = (b >> 5) & 0x03
            track["track_sensor"] = ("combined", "psr", "ssr_mode_s", "invalid")[rad]
            if b & 0x10: track["track_doubtful"]  = True
            if b & 0x08: track["track_manoeuvre"] = True
            cdm = (b >> 1) & 0x03
            if   cdm == 1: track["vertical_trend"] = "climbing"
            elif cdm == 2: track["vertical_trend"] = "descending"
            elif cdm == 3: track["vertical_trend"] = "unknown"
            if b & 0x01:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1
                if b & 0x80: track["track_end"]   = True
                if b & 0x40: track["track_ghost"]  = True
                if b & 0x20: track["supported_by_neighbour_node"] = True
                if b & 0x10: track["slant_range_correction"] = True
                while b & 0x01:
                    if pos >= len(data): break
                    b = data[pos]; pos += 1
        elif frn == 14:                 # I048/210  Track Quality (4 bytes)
            if pos + 4 > len(data): return track, len(data)
            track["track_sigma_x_nm"] = round(data[pos]     / 128.0, 4)
            track["track_sigma_y_nm"] = round(data[pos + 1] / 128.0, 4)
            track["track_sigma_v_kt"] = round(data[pos + 2] * (2 ** -14) * 3600.0, 2)
            track["track_sigma_heading_deg"] = round(data[pos + 3] * 360.0 / 4096.0, 2)
            pos += 4
        elif frn == 15:                 # I048/030  Warning/Error Conditions (FX repeating type codes)
            # Each octet: bits 7-1 = condition code (1-127), bit 0 = FX
            _WE048 = {
                1: "multipath_reply", 2: "sidelobe_reply", 3: "split_plot",
                4: "second_time_around", 5: "angel", 6: "terrestrial_vehicle",
                7: "fixed_psr_plot", 8: "slow_psr_target", 9: "low_quality_psr",
                10: "phantom_ssr", 11: "mode3a_mismatch", 12: "abnormal_altitude",
                13: "clutter", 14: "max_zero_filter_doppler", 15: "transponder_anomaly",
                16: "duplicate_illegal_address", 17: "mode_s_error_correction",
                18: "undecodable_altitude", 19: "bird", 20: "flock_of_birds",
                21: "mode1_present", 22: "mode2_present", 23: "wind_turbine",
                24: "helicopter", 25: "max_surveillance_reinterrogations",
                26: "max_bds_reinterrogations", 27: "bds_overlay_incoherence",
                28: "potential_bds_swap", 29: "zenithal_gap_update",
                30: "mode_s_track_reacquired", 31: "duplicate_mode5_pair",
                32: "wrong_df_format", 33: "mode_ac_all_call_anomaly",
                34: "si_capability_anomaly", 35: "potential_ic_conflict",
                36: "ic_conflict_detection_possible", 37: "duplicate_mode5_pin",
            }
            codes = []
            while True:
                if pos >= len(data): break
                b = data[pos]; pos += 1
                code = (b >> 1) & 0x7F
                codes.append(code)
                name = _WE048.get(code)
                if name: track.setdefault("warning_error_names", []).append(name)
                if code == 6:
                    track["target_type"] = "ground_vehicle"; track["on_ground"] = True
                if not (b & 0x01): break
            if codes: track["we_conditions"] = codes
        elif frn == 16:                 # I048/080  Mode-3/A Confidence (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            track["squawk_quality_mask"] = (
                _cat48__u16(data[pos:pos + 2]) & 0x0FFF
            )
            pos += 2
        elif frn == 17:                 # I048/100  Mode-C Gillham (2 + 2 confidence bytes)
            if pos + 4 > len(data): return track, len(data)
            alt = _cat48__gillham_to_ft(_cat48__u16(data[pos:pos + 2]))
            if alt is not None: track["mode_c_alt_ft"] = alt
            pos += 4
        elif frn == 18:                 # I048/110  3D Height (25 ft/LSB, signed)
            if pos + 2 > len(data): return track, len(data)
            track["alt_3d_ft"] = _cat48__s16(data[pos:pos+2]) * 25; pos += 2
        elif frn == 19:                 # I048/120  Radial Doppler (compound)
            try:
                present, pos = _cat48__compound_presence(data, pos)
            except ValueError:
                return track, len(data)
            if any(present[2:]):
                return track, len(data)
            if present[0]:              # CAL: doubtful bit + signed 10-bit m/s
                if pos + 2 > len(data): return track, len(data)
                w = _cat48__u16(data[pos:pos + 2])
                raw = w & 0x03FF
                if raw & 0x0200: raw -= 0x0400
                track["doppler_ms"] = raw
                track["doppler_kt"] = round(raw * 1.943844, 1)
                if w & 0x8000: track["doppler_doubtful"] = True
                pos += 2
            if present[1]:              # RDS sub-field: REP × 6 bytes
                if pos >= len(data): return track, len(data)
                rep = data[pos]; pos += 1
                if pos + rep * 6 > len(data): return track, len(data)
                if rep > 0:
                    speed = _cat48__s16(data[pos:pos + 2])
                    track["doppler_raw_ms"] = speed
                    track["doppler_ambiguity_ms"] = _cat48__u16(data[pos + 2:pos + 4])
                    track["doppler_frequency_mhz"] = _cat48__u16(data[pos + 4:pos + 6])
                pos += rep * 6
        elif frn == 20:                 # I048/230  Communications/ACAS Capability (2 bytes)
            if pos + 2 > len(data): return track, len(data)
            b0 = data[pos]; b1 = data[pos + 1]
            com  = (b0 >> 5) & 0x07
            stat = (b0 >> 2) & 0x07
            track["com_capability"] = com
            _STAT230 = (
                "no_alert_no_spi_airborne", "no_alert_no_spi_ground",
                "alert_no_spi_airborne", "alert_no_spi_ground",
                "alert_spi", "no_alert_spi", "unassigned", "unknown",
            )
            track["transponder_status"] = _STAT230[stat]
            if stat in (1, 3): track["on_ground"] = True
            if stat in (2, 3, 4): track["alert"] = True
            if stat in (4, 5): track["spi"] = True
            track["interrogator_code_capability"] = "II" if (b0 & 0x02) else "SI"
            if b1 & 0x80: track["mssc"]          = True
            if b1 & 0x40: track["altitude_25ft"] = True
            if b1 & 0x20: track["aic"]           = True
            track["bds10_b1a"] = bool(b1 & 0x10)
            track["bds10_b1b"] = b1 & 0x0F
            pos += 2
        elif frn == 21:                 # I048/260  ACAS RA (7 bytes = BDS 3,0)
            if pos + 7 > len(data): return track, len(data)
            track.update(_cat48__decode_bds30(data[pos:pos + 7]))
            pos += 7
        elif frn == 22:                 # I048/055  Mode-1
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["mode1"] = "{:02o}".format(b & 0x1F)
            if b & 0x80: track["mode1_invalid"]  = True
            if b & 0x40: track["mode1_garbled"]  = True
            if b & 0x20: track["mode1_smoothed"] = True
        elif frn == 23:                 # I048/050  Mode-2 Code (2 bytes, lower 12 bits)
            if pos + 2 > len(data): return track, len(data)
            w = _cat48__u16(data[pos:pos + 2]); pos += 2
            track["mode2"] = "{:04o}".format(w & 0x0FFF)
            if w & 0x8000: track["mode2_invalid"]  = True
            if w & 0x4000: track["mode2_garbled"]  = True
            if w & 0x2000: track["mode2_smoothed"] = True
        elif frn == 24:                 # I048/065  Mode-1 confidence/quality
            if pos >= len(data): return track, len(data)
            track["mode1_quality_mask"] = data[pos]; pos += 1
        elif frn == 25:                 # I048/060  Mode-2 confidence/quality
            if pos + 2 > len(data): return track, len(data)
            track["mode2_quality_mask"] = _cat48__u16(data[pos:pos + 2]); pos += 2
        elif frn == 26:                 # I048/SP Special Purpose Field
            pos = _cat48__skip_len_field(data, pos)
        elif frn == 27:                 # I048/RE Reserved Expansion Field
            pos = _cat48__skip_len_field(data, pos)
        else: break

    if "icao24" not in track:
        sac = track.get("sac", 0); sic = track.get("sic", 0)
        track["radar_id"] = "CAT48-{:03d}-{:03d}-{:04d}".format(
            sac, sic, track.get("track_num", 0))
    return track, pos

def _cat48__pub(pub, track: dict, label: str, verbose: bool):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(pub, _cat48_TOPIC_048.format(source=_asterix_source(track)),
                 track, AsterixCat48Track)
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

def _cat48__make_cat048_handler(pub, site, sites=None, site_lock=None):
    # CAT-34 and CAT-48 translators are separate processes. CAT-34 site
    # positions therefore arrive through the semantic JSON topic and are kept
    # by SAC/SIC here rather than relying on process-local shared memory.
    sites = sites if sites is not None else {}
    site_lock = site_lock or threading.Lock()

    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat48_decode_cat048_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if len(track) > 2:
                with site_lock:
                    _cat48__geolocate_from_site(track, site, sites)
                if verbose and "lat_deg" not in track:
                    ident = track.get("icao24") or track.get("radar_id") or "PSR"
                    print("cat48 {} no-position (awaiting CAT-34 site or set --radar-lat/--radar-lon)".format(
                        ident), flush=True)
                _cat48__pub(pub, track, "cat48", verbose)
                publish_native(pub, native_topic(semantic_topic(
                                   _cat48_TOPIC_048.format(source=_asterix_source(track)), track)),
                               asterix_data_block(48, data[previous:pos]),
                               "asterix", profile="cat048")
    return _h

def _cat48__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat48__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat48__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat48__raw_frame_payload(bytes(sample.payload), category)
        except ValueError as exc:
            print("CAT-{} ignored invalid raw Zenoh frame: {}".format(category, exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-{} raw Zenoh input: {}".format(category, input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()

def _cat48__process_tcp_conn(conn, addr, label, handlers, verbose):
    try:
        _cat48__process_stream(_cat48_iter_frames_tcp(conn), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)

def _cat48__run_inbound(port: int, use_tcp: bool, label: str, handlers: dict, verbose: bool):
    ip = _cat48__netbird_ip()
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
                target=_cat48__process_tcp_conn,
                args=(conn, addr, label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        _cat48__process_stream(_cat48_iter_frames_udp(sock), handlers, verbose)



def _cat48_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-048 Ed.1.32 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT48_PORT", "50048") or 50048))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT48_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT48_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT48_INPUT_TOPIC", _cat48_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat48__env_float("CAT48_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat48__env_float("CAT48_RADAR_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT48_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    if None in site: print("INFO: set CAT48_RADAR_LAT/LON for local polar positions", flush=True)
    session = open_session()
    sites: dict[tuple[int, int], tuple[float, float]] = {}
    site_lock = threading.Lock()

    def _on_cat34_site(sample):
        try:
            track = json.loads(bytes(sample.payload))
        except (TypeError, ValueError, UnicodeDecodeError):
            return
        with site_lock:
            learned = _cat48__cache_cat34_site(track, sites)
        if learned and args.verbose:
            print(
                "CAT-48 learned site SAC{}/SIC{} at {},{}".format(
                    track["sac"], track["sic"], track["lat_deg"], track["lon_deg"]
                ),
                flush=True,
            )

    site_subscriber = subscribe(
        session,
        os.environ.get("CAT48_SITE_INPUT_TOPIC", _cat48_SITE_INPUT_TOPIC),
        _on_cat34_site,
    )
    handler = _cat48__make_cat048_handler(session, site, sites, site_lock)
    try:
        if args.zenoh_raw: _cat48__run_zenoh_raw(session, args.input_topic, _cat48_CAT_048, handler, args.verbose)
        else: _cat48__run_inbound(args.port, args.tcp, "CAT-48 Ed.1.32", {_cat48_CAT_048: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally:
        site_subscriber.undeclare()
        session.close()

decode_cat048_record = _cat48_decode_cat048_record


# ==========================================================================
# CAT-062
# ==========================================================================

import argparse

import json

import math

import os

import socket

import struct

import threading

import time

from namespace_prefix import topic_root
from protocols.track_views import (publish_dual, publish_native, native_topic,
                                      semantic_topic, asterix_data_block)
from protocols.data_stats import record_in



_cat62_TOPIC_ROOT = topic_root()




# CAT-062 is the output of a tracker that has already combined several sensors,
# so the modality is `fused` rather than any one sensing method.
_cat62_TOPIC_062    = _cat62_TOPIC_ROOT + "/air/{source}/fused/unknown/aircraft"
_cat62_RAW_INPUT_TOPIC = "{}/raw/asterix/cat62".format(_cat62_TOPIC_ROOT)

_cat62_CAT_062 = 0x3E

_cat62_RECONNECT_DELAY_S = 5.0
_cat62_ZENOH_RETRY_S = 5.0

_cat62__CHARSET_6BIT = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"

_cat62__EMERGENCY_CODES = {
    0: None,
    1: "GENERAL EMERGENCY",
    2: "LIFEGUARD/MEDICAL",
    3: "MIN FUEL",
    4: "NO COMMS",
    5: "UNLAWFUL INTERFERENCE",
    6: "DOWNED AIRCRAFT",
}

def _cat62__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf

def _cat62_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat62__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream.  Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat62__recv_exact(sock, length - 3)
        record_in("cat62", len(data))
        yield cat, data

def _cat62_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat62", len(record))
            yield cat, record
            offset += length

def _cat62_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos

def _cat62__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat62__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat62__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]

def _cat62__u32(b: bytes) -> int: return struct.unpack(">I", b)[0]

def _cat62__s24(b: bytes) -> int:
    raw = int.from_bytes(b, "big")
    return raw - (1 << 24) if raw & (1 << 23) else raw

def _cat62__decode_callsign(raw: bytes) -> str:
    """Decode 8-char ICAO 6-bit aircraft identification from 6 bytes (48 bits)."""
    bits = int.from_bytes(raw, "big")
    return "".join(_cat62__CHARSET_6BIT[(bits >> (i * 6)) & 0x3F]
                   for i in range(7, -1, -1)).strip()

def _cat62__decode_bds50(mb: bytes) -> dict:
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
    if _bit(24): out["bds_gs_kt"]        = round(_uns(25, 34) * 2.0, 0)
    if _bit(35): out["track_rate_degs"]  = round(_sgn(36, 45) * 8.0 / 256.0, 2)
    if _bit(46): out["tas_kt"]           = round(_uns(47, 56) * 2.0, 0)
    return out

def _cat62__decode_bds60(mb: bytes) -> dict:
    """BDS 6,0 Heading and Speed Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n):    return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    def _sgn(a, b):
        w = b - a + 1; r = _uns(a, b)
        return r - (1 << w) if r >= (1 << (w - 1)) else r
    out = {}
    if _bit(1):  out["mag_hdg_deg"]  = round(_uns(2, 11) * 360.0 / 1024.0, 1)
    if _bit(13): out["ias_kt"]       = _uns(14, 23)
    if _bit(24): out["mach"]         = round(_uns(25, 34) * 2.048 / 512.0, 3)
    if _bit(35): out["baro_vr_fpm"]  = _sgn(36, 45) * 32
    if _bit(46): out["ivv_fpm"]      = _sgn(47, 56) * 32
    return out

def _cat62__gillham_to_ft(code: int) -> int | None:
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

def _cat62__decode_bds40(mb: bytes) -> dict:
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

# BDS 1,0/1,7 bit layouts: pyModeS src/pyModeS/decoder/bds/bds10.py and
# bds17.py (github.com/junzis/pyModeS) — see docs/references/ASTERIX.md.
def _cat62__decode_bds10(mb: bytes) -> dict:
    """BDS 1,0 Data Link Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    def _bit(n): return (v >> (56 - n)) & 1
    def _uns(a, b): return (v >> (56 - b)) & ((1 << (b - a + 1)) - 1)
    return {
        "dlc_continuation_flag": bool(_bit(9)),
        "overlay_command_capability": bool(_bit(15)),
        "acas_operational": bool(_bit(16)),
        "mode_s_subnetwork_version": _uns(17, 23),
        "transponder_level5": bool(_bit(24)),
        "mode_s_specific_services": bool(_bit(25)),
        "uplink_elm_throughput": _uns(26, 28),
        "downlink_elm_throughput": _uns(29, 32),
        "aircraft_id_capability": bool(_bit(33)),
        "squitter_capability": bool(_bit(34)),
        "surveillance_id_capability": bool(_bit(35)),
        "common_usage_gicb_capability": bool(_bit(36)),
        "acas_hybrid_surveillance": bool(_bit(37)),
        "acas_resolution_advisory": bool(_bit(38)),
        "acas_rtca_version": _uns(39, 40),
        "dte_status": _uns(41, 56),
    }

_cat62__BDS17_CAPABILITY = (
    "0,5", "0,6", "0,7", "0,8", "0,9", "0,A", "2,0", "2,1",
    "4,0", "4,1", "4,2", "4,3", "4,4", "4,5", "4,8", "5,0",
    "5,1", "5,2", "5,3", "5,4", "5,5", "5,6", "5,F", "6,0",
)

def _cat62__decode_bds17(mb: bytes) -> dict:
    """BDS 1,7 Common Usage GICB Capability Report."""
    v = int.from_bytes(mb[:7], "big")
    return {"supported_bds": [code for i, code in enumerate(_cat62__BDS17_CAPABILITY)
                               if (v >> (56 - (i + 1))) & 1]}


def _cat62__decode_bds30(mb: bytes) -> dict:
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

def _cat62__decode_i062_380(data: bytes, pos: int) -> tuple[dict, int]:
    """I062/380 Aircraft Derived Data — PSF-gated sub-fields."""
    out: dict = {}
    if pos >= len(data):
        return out, pos
    psf = data[pos]; pos += 1
    # Sub 01: Aircraft Address (3 bytes)
    if psf & 0x80:
        if pos + 3 > len(data): return out, pos
        addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
        out["icao24"] = "{:06x}".format(addr); pos += 3
    # Sub 02: Aircraft ID (1B flags + 6B callsign)
    if psf & 0x40:
        if pos + 7 > len(data): return out, pos
        pos += 1
        out["callsign"] = _cat62__decode_callsign(data[pos:pos+6]); pos += 6
    # Sub 03: Roll Angle (2 bytes, s16, 45/512 deg)
    if psf & 0x20:
        if pos + 2 > len(data): return out, pos
        out["roll_deg"] = round(_cat62__s16(data[pos:pos+2]) * 45.0 / 512.0, 1); pos += 2
    # Sub 04: Track Angle (2 bytes, u16, 360/65536)
    if psf & 0x10:
        if pos + 2 > len(data): return out, pos
        out["true_track_deg"] = round(_cat62__u16(data[pos:pos+2]) * 360.0 / 65536.0, 2); pos += 2
    # Sub 05: Airspeed (2 bytes, IM + 15-bit)
    if psf & 0x08:
        if pos + 2 > len(data): return out, pos
        w = _cat62__u16(data[pos:pos+2])
        im = (w >> 15) & 1; val = w & 0x7FFF
        if im: out["mach"]   = round(val * 2.0 / (2**14), 3)
        else:  out["ias_kt"] = round(val * 3600.0 / 16384.0, 1)
        pos += 2
    # Sub 06: TAS (2 bytes, u16, 1 kt/LSB)
    if psf & 0x04:
        if pos + 2 > len(data): return out, pos
        w = _cat62__u16(data[pos:pos+2]) & 0x7FFF
        if w: out["tas_kt"] = w
        pos += 2
    # Sub 07: SSR modes (2 bytes, skip)
    if psf & 0x02:
        pos += 2
    # FX bit → read next PSF byte
    if not (psf & 0x01):
        return out, pos
    if pos >= len(data):
        return out, pos
    psf2 = data[pos]; pos += 1
    # Sub 08: Emergency (1 byte)
    if psf2 & 0x80:
        if pos + 1 > len(data): return out, pos
        ec = data[pos] & 0x07; pos += 1
        out["emergency_code"] = ec
        s = _cat62__EMERGENCY_CODES.get(ec)
        if s: out["emergency_str"] = s
    # Sub 09: Met (wind 2B + dir 2B + temp 2B + turb 2B = 8 bytes)
    if psf2 & 0x40:
        if pos + 8 > len(data): return out, pos
        out["wind_speed_kt"] = round(_cat62__u16(data[pos:pos+2]) * 0.5, 1)
        out["wind_dir_deg"]  = round(_cat62__u16(data[pos+2:pos+4]) * 360.0 / 65536.0, 1)
        out["temp_c"]        = round(_cat62__s16(data[pos+4:pos+6]) * 0.25, 1)
        pos += 8
    # Sub 10: ACAS RA (7 bytes, BDS 3,0)
    if psf2 & 0x20:
        if pos + 7 > len(data): return out, pos
        out.update(_cat62__decode_bds30(data[pos:pos+7])); pos += 7
    # Sub 11: Barometric Alt (2 bytes, s16, 0.25 FL)
    if psf2 & 0x10:
        if pos + 2 > len(data): return out, pos
        out["alt_baro_ft"] = round(_cat62__s16(data[pos:pos+2]) * 0.25 * 100); pos += 2
    # Sub 12: Mode-C code (2 bytes, Gillham)
    if psf2 & 0x08:
        if pos + 2 > len(data): return out, pos
        alt = _cat62__gillham_to_ft(_cat62__u16(data[pos:pos+2]))
        if alt is not None: out["mode_c_alt_ft"] = alt
        pos += 2
    # Sub 13: ICAO address (3 bytes, fallback)
    if psf2 & 0x04:
        if pos + 3 > len(data): return out, pos
        addr = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
        out.setdefault("icao24", "{:06x}".format(addr)); pos += 3
    # Sub 14: Mode-S MB data (REP × 8 bytes)
    if psf2 & 0x02:
        if pos >= len(data): return out, pos
        rep = data[pos]; pos += 1
        for _ in range(rep):
            if pos + 8 > len(data): break
            mb = bytes(data[pos:pos+7]); bds = data[pos+7]; pos += 8
            b1 = (bds >> 4) & 0xF; b2 = bds & 0xF
            if   b1 == 1 and b2 == 0: out.update(_cat62__decode_bds10(mb))
            elif b1 == 1 and b2 == 7: out.update(_cat62__decode_bds17(mb))
            elif b1 == 3 and b2 == 0: out.update(_cat62__decode_bds30(mb))
            elif b1 == 4 and b2 == 0: out.update(_cat62__decode_bds40(mb))
            elif b1 == 5 and b2 == 0: out.update(_cat62__decode_bds50(mb))
            elif b1 == 6 and b2 == 0: out.update(_cat62__decode_bds60(mb))
    # Consume any further FX extension bytes
    while psf2 & 0x01:
        if pos >= len(data): break
        psf2 = data[pos]; pos += 1
    return out, pos

def _cat62__pub(pub, track: dict, label: str, verbose: bool, topic: str = _cat62_TOPIC_062):
    if "lat_deg" not in track or "lon_deg" not in track:
        return
    publish_dual(
        pub,
        # A --topic override without `{source}` formats to itself, so an
        # operator-supplied literal topic still works unchanged.
        topic.format(source=_asterix_source(track)),
        track,
        Cat62Track,
        wrapper_field="normalized",
    )
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

def _cat62__presence(data: bytes, pos: int, maximum: int) -> tuple[list[bool], int]:
    present: list[bool] = []
    for _ in range(maximum):
        if pos >= len(data):
            raise ValueError("truncated ASTERIX compound presence map")
        byte = data[pos]; pos += 1
        present.extend(bool(byte & (1 << shift)) for shift in range(7, 0, -1))
        if not byte & 1:
            return present, pos
    if data[pos - 1] & 1:
        raise ValueError("unsupported ASTERIX compound presence extension")
    return present, pos


def _cat62__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data): return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat62__decode_380_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 4)
    fixed = (3, 6, 2, 2, 2, 2, 2, 1, None, 2, 2, 7, 2, 2,
             2, 2, 2, 2, 1, None, 1, 6, 2, 1, None, 2, 2, 2)
    for index, size in enumerate(fixed):
        if index >= len(flags) or not flags[index]: continue
        if index == 8:                  # trajectory intent: REP + 15 octets each
            if pos >= len(data): return out, len(data)
            size = 1 + data[pos] * 15
        elif index == 19:               # meteorological data, compound
            met_flags, payload_pos = _cat62__presence(data, pos, 1)
            pos = payload_pos
            for met_index, met_size in enumerate((2, 2, 2, 1)):
                if met_flags[met_index]:
                    if pos + met_size > len(data): return out, len(data)
                    if met_index == 0: out["wind_speed_kt"] = _cat62__u16(data[pos:pos + 2])
                    elif met_index == 1: out["wind_dir_deg"] = _cat62__u16(data[pos:pos + 2])
                    elif met_index == 2: out["temp_c"] = _cat62__s16(data[pos:pos + 2]) * 0.25
                    else: out["turbulence"] = data[pos]
                    pos += met_size
            continue
        elif index == 24:               # BDS data: REP + 8 octets each
            if pos >= len(data): return out, len(data)
            rep = data[pos]; pos += 1
            for _ in range(rep):
                if pos + 8 > len(data): return out, len(data)
                mb, bds = data[pos:pos + 7], data[pos + 7]; pos += 8
                code = (bds >> 4, bds & 15)
                if code == (1, 0): out.update(_cat62__decode_bds10(mb))
                elif code == (1, 7): out.update(_cat62__decode_bds17(mb))
                elif code == (3, 0): out.update(_cat62__decode_bds30(mb))
                elif code == (4, 0): out.update(_cat62__decode_bds40(mb))
                elif code == (5, 0): out.update(_cat62__decode_bds50(mb))
                elif code == (6, 0): out.update(_cat62__decode_bds60(mb))
            continue
        assert size is not None
        if pos + size > len(data): return out, len(data)
        raw = data[pos:pos + size]
        if index == 0: out["icao24"] = raw.hex()
        elif index == 1: out["callsign"] = _cat62__decode_callsign(raw)
        elif index == 2: out["mag_hdg_deg"] = round(_cat62__u16(raw) * 360 / 65536, 3)
        elif index == 3:
            w = _cat62__u16(raw)
            if w & 0x8000: out["mach"] = round((w & 0x7FFF) * 0.001, 3)
            else: out["ias_kt"] = round((w & 0x7FFF) * 3600 / 16384, 2)
        elif index == 4: out["tas_kt"] = _cat62__u16(raw) & 0x7FFF
        elif index in (5, 6):
            w = _cat62__u16(raw); value = w & 0x1FFF
            if value & 0x1000: value -= 0x2000
            out["selected_alt_ft" if index == 5 else "final_alt_ft"] = value * 25
        elif index in (12, 13):
            w = _cat62__u16(raw); value = w & 0x7FFF
            if value & 0x4000: value -= 0x8000
            out["baro_vr_fpm" if index == 12 else "geo_vr_fpm"] = value * 6.25
        elif index == 14: out["roll_deg"] = round(_cat62__s16(raw) * 0.01, 2)
        elif index == 15:
            value = _cat62__u16(raw) & 0x03FF
            if value & 0x0200: value -= 0x0400
            out["track_angle_rate_degs"] = round(value / 32, 3)
        elif index == 16: out["heading_deg"] = round(_cat62__u16(raw) * 360 / 65536, 3)
        elif index == 17: out["speed_ms"] = round(_cat62__s16(raw) * 2**-14 * 1852, 3)
        elif index == 20: out["emitter_category"] = raw[0]
        elif index == 21:
            scale = 180 / 2**23
            out["aircraft_lat_deg"] = round(_cat62__s24(raw[:3]) * scale, 7)
            out["aircraft_lon_deg"] = round(_cat62__s24(raw[3:]) * scale, 7)
        elif index == 22: out["aircraft_geo_alt_ft"] = _cat62__s16(raw) * 6.25
        elif index == 25: out["ias_kt"] = _cat62__u16(raw)
        elif index == 26: out["mach"] = round(_cat62__u16(raw) * 0.008, 3)
        elif index == 27: out["baro_setting_mb"] = 800 + (_cat62__u16(raw) & 0x0FFF) * 0.1
        pos += size
    return out, pos


def _cat62__decode_390_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 3)
    sizes = (2, 7, 4, 1, 4, 1, 4, 4, 3, 2, 2, None, 6, 1, 7, 7, 2, 7)
    names = (None, "flight_plan_callsign", None, None, "aircraft_type", "wake_turb_cat",
             "departure_icao", "destination_icao", None, None, None, None,
             "aircraft_stand", None, "sid", "star", None, "pre_emergency_callsign")
    for index, size in enumerate(sizes):
        if index >= len(flags) or not flags[index]: continue
        if index == 11:                 # TOD: Time of Departure/Arrival, REP x 4 bytes
            if pos >= len(data): return out, len(data)
            rep = data[pos]
            size = 1 + rep * 4
            if pos + size > len(data): return out, len(data)
            entries = []
            entry_pos = pos + 1
            for _ in range(rep):
                val = _cat62__u32(data[entry_pos:entry_pos + 4])
                entries.append({
                    "type": (val >> 27) & 0x1F,
                    "day": (val >> 25) & 0x03,
                    "hour": (val >> 16) & 0x1F,
                    "minute": (val >> 8) & 0x3F,
                    "seconds_available": bool(val & 0x80),
                    "second": val & 0x3F,
                })
                entry_pos += 4
            out["departure_arrival_times"] = entries
            pos += size
            continue
        assert size is not None
        if pos + size > len(data): return out, len(data)
        name = names[index]
        if name:
            raw = data[pos:pos + size]
            if name == "wake_turb_cat": out[name] = chr(raw[0])
            else: out[name] = raw.decode("ascii", errors="replace").strip("\x00 ")
        if index == 0:                  # TAG: FPPS Identification (SAC/SIC)
            out["fpps_sac"] = data[pos]; out["fpps_sic"] = data[pos + 1]
        elif index == 2:                 # IFI: IFPS_FLIGHT_ID
            val = _cat62__u32(data[pos:pos + 4])
            out["ifps_id_type"] = (val >> 30) & 0x03
            out["ifps_flight_id"] = val & 0x07FFFFFF
        elif index == 3:                 # FCT: Flight Category
            b = data[pos]
            gat, fr, rvsm = (b >> 6) & 0x03, (b >> 4) & 0x03, (b >> 2) & 0x03
            if gat:  out["flight_gat"]   = ("", "GAT", "OAT", "GAT+OAT")[gat]
            if fr:   out["flight_rules"] = ("", "IFR", "VFR", "IFR+VFR")[fr]
            if rvsm == 1: out["rvsm"] = "approved"
            elif rvsm == 2: out["rvsm"] = "exempt"
            elif rvsm == 3: out["rvsm"] = "not_approved"
            if b & 0x02: out["high_priority"] = True
        elif index == 8:                  # RDS: Runway Designation (NU1/NU2/LTR digits + letter)
            nu1, nu2, ltr = data[pos], data[pos + 1], data[pos + 2]
            letter = chr(ltr) if 32 < ltr < 127 else ""
            out["runway"] = "{}{}{}".format(nu1, nu2, letter)
            out["runway_nu1"] = nu1; out["runway_nu2"] = nu2
        elif index == 9:                  # CFL: Current Cleared Flight Level
            out["cleared_fl"] = _cat62__s16(data[pos:pos + 2]) * 0.25
        elif index == 10:                 # CTL: Current Control Position
            out["control_centre"] = data[pos]; out["control_position"] = data[pos + 1]
        elif index == 13:                 # STS: Stand Status
            b = data[pos]
            out["stand_emplacement"] = (b >> 6) & 0x03
            out["stand_availability"] = (b >> 4) & 0x03
        elif index == 16:                 # PEM: Pre-Emergency Mode 3/A
            val = _cat62__u16(data[pos:pos + 2])
            out["pre_emergency_squawk_valid"] = bool(val & 0x1000)
            out["pre_emergency_squawk"] = "{:04o}".format(val & 0x0FFF)
        pos += size
    return out, pos


def _cat62__decode_500_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 2)
    sizes = (4, 2, 4, 1, 1, 2, 2, 1)
    for index, size in enumerate(sizes):
        if index >= len(flags) or not flags[index]: continue
        if pos + size > len(data): return out, len(data)
        if index == 0:
            out["pos_accuracy_x_m"] = _cat62__u16(data[pos:pos + 2]) * 0.5
            out["pos_accuracy_y_m"] = _cat62__u16(data[pos + 2:pos + 4]) * 0.5
        elif index == 3: out["geo_alt_accuracy_ft"] = data[pos] * 6.25
        elif index == 4: out["baro_alt_accuracy_ft"] = data[pos] * 25
        pos += size
    return out, pos


def _cat62__decode_340_ed121(data: bytes, pos: int) -> tuple[dict, int]:
    out: dict = {}
    flags, pos = _cat62__presence(data, pos, 1)
    sizes = (2, 4, 2, 4, 2, 1)
    for index, size in enumerate(sizes):
        if index >= len(flags) or not flags[index]: continue
        if pos + size > len(data): return out, len(data)
        if index == 0: out["measured_by"] = "{}/{}".format(data[pos], data[pos + 1])
        elif index == 1:
            out["meas_range_nm"] = round(_cat62__u16(data[pos:pos + 2]) / 256, 3)
            out["meas_az_deg"] = round(_cat62__u16(data[pos + 2:pos + 4]) * 360 / 65536, 3)
        elif index == 2: out["meas_alt_ft"] = _cat62__s16(data[pos:pos + 2]) * 25
        elif index == 4: out["squawk"] = "{:04o}".format(_cat62__u16(data[pos:pos + 2]) & 0x0FFF)
        pos += size
    return out, pos


def _cat62_decode_cat62_record(data: bytes, pos: int):
    """Decode one EUROCONTROL CAT-062 Edition 1.21 system-track record."""
    fspec, pos = _cat62_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-62 Ed.1.21"}
    for frn, present in enumerate(fspec):
        if not present: continue
        if frn == 0:
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # reserved FRN 2; no bytes
            continue
        elif frn == 2:
            if pos >= len(data): return track, len(data)
            track["service_id"] = data[pos]; pos += 1
        elif frn == 3:
            if pos + 3 > len(data): return track, len(data)
            track["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128; pos += 3
        elif frn == 4:
            if pos + 8 > len(data): return track, len(data)
            scale = 180 / 2**25
            track["lat_deg"] = round(_cat62__s32(data[pos:pos + 4]) * scale, 7)
            track["lon_deg"] = round(_cat62__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
        elif frn == 5:
            if pos + 6 > len(data): return track, len(data)
            track["x_m"] = _cat62__s24(data[pos:pos + 3]) * 0.5
            track["y_m"] = _cat62__s24(data[pos + 3:pos + 6]) * 0.5; pos += 6
        elif frn == 6:
            if pos + 4 > len(data): return track, len(data)
            vx = _cat62__s16(data[pos:pos + 2]) * 0.25; vy = _cat62__s16(data[pos + 2:pos + 4]) * 0.25; pos += 4
            track["vx_ms"], track["vy_ms"] = vx, vy
            track["speed_ms"] = round(math.hypot(vx, vy), 2)
            if vx or vy: track["heading_deg"] = round(math.degrees(math.atan2(vx, vy)) % 360, 2)
        elif frn == 7:
            if pos + 2 > len(data): return track, len(data)
            track["ax_ms2"] = struct.unpack_from("b", data, pos)[0] * 0.25
            track["ay_ms2"] = struct.unpack_from("b", data, pos + 1)[0] * 0.25; pos += 2
        elif frn == 8:
            if pos + 2 > len(data): return track, len(data)
            track["squawk"] = "{:04o}".format(_cat62__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 9:
            if pos + 7 > len(data): return track, len(data)
            track["callsign"] = _cat62__decode_callsign(data[pos + 1:pos + 7]); pos += 7
        elif frn == 10:
            try: extra, pos = _cat62__decode_380_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn == 11:
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat62__u16(data[pos:pos + 2]) & 0x0FFF; pos += 2
        elif frn == 12:                 # track status, FX
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["track_monosensor"] = bool(b & 0x80); track["confirmed"] = not bool(b & 0x02)
            extent = 0
            while b & 1:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1:
                    if b & 0x40: track["_delete"] = True
                    if b & 0x20: track["track_begin"] = True
        elif frn == 13:                 # I062/290
            try: flags, pos = _cat62__presence(data, pos, 2)
            except ValueError: return track, len(data)
            names = ("track", "psr", "ssr", "mode_s", "ads_c", "ads_b_es", "vdl4", "uat", "loop", "mlat")
            for index, name in enumerate(names):
                if index < len(flags) and flags[index]:
                    size = 2 if index == 4 else 1
                    if pos + size > len(data): return track, len(data)
                    track["track_age_{}_s".format(name)] = int.from_bytes(data[pos:pos + size], "big") * 0.25
                    pos += size
        elif frn == 14:
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1
            track["lateral_trend"] = ("constant", "right", "left", "undetermined")[(b >> 6) & 3]
            track["speed_trend"] = ("constant", "increasing", "decreasing", "undetermined")[(b >> 4) & 3]
            track["vertical_trend"] = ("level", "climb", "descent", "undetermined")[(b >> 2) & 3]
            if b & 0x02: track["alt_discrepancy"] = True
        elif frn == 15:                 # I062/295, 31 one-byte age fields
            try: flags, pos = _cat62__presence(data, pos, 5)
            except ValueError: return track, len(data)
            _I062_295_NAMES = (
                "mfl", "md1", "md2", "mda", "md4", "md5", "mhg", "ias", "tas",
                "sal", "fss", "tid", "com", "sab", "acs", "bvr", "gvr", "ran",
                "tar", "tan", "gsp", "vun", "met", "emc", "pos", "gal", "pun",
                "mb", "iar", "mac", "bps",
            )
            ages = {}
            for index, name in enumerate(_I062_295_NAMES):
                if index >= len(flags) or not flags[index]: continue
                if pos >= len(data): return track, len(data)
                ages[name] = data[pos] * 0.25
                pos += 1
            if ages: track["data_ages_s"] = ages
        elif frn in (16, 17, 18, 19):
            if pos + 2 > len(data): return track, len(data)
            w = _cat62__u16(data[pos:pos + 2]); pos += 2
            if frn == 16: track["measured_alt_ft"] = _cat62__s16(w.to_bytes(2, "big")) * 25
            elif frn == 17: track["calc_geo_alt_ft"] = _cat62__s16(w.to_bytes(2, "big")) * 6.25
            elif frn == 18:
                raw = w & 0x7FFF
                if raw & 0x4000: raw -= 0x8000
                track["calc_baro_alt_ft"] = raw * 25; track["qnh_corrected"] = bool(w & 0x8000)
            else: track["vertical_rate_fpm"] = _cat62__s16(w.to_bytes(2, "big")) * 6.25
        elif frn == 20:
            try: extra, pos = _cat62__decode_390_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn == 21:                 # target dimensions FX
            if pos >= len(data): return track, len(data)
            b = data[pos]; pos += 1; track["target_length_m"] = b >> 1
            extent = 0
            while b & 1:
                if pos >= len(data): return track, len(data)
                b = data[pos]; pos += 1; extent += 1
                if extent == 1: track["target_orientation_deg"] = round((b >> 1) * 360 / 128, 2)
                elif extent == 2: track["target_width_m"] = b >> 1
        elif frn == 22:
            if pos >= len(data): return track, len(data)
            track["fleet_id"] = data[pos]; pos += 1
        elif frn == 23:                 # I062/110
            try: flags, pos = _cat62__presence(data, pos, 1)
            except ValueError: return track, len(data)
            for index, size in enumerate((1, 4, 6, 2, 2, 1, 1)):
                if flags[index]:
                    if pos + size > len(data): return track, len(data)
                    if index == 4: track["mode1"] = "{:02o}".format(_cat62__u16(data[pos:pos + 2]) & 0x3F)
                    pos += size
        elif frn == 24:
            if pos + 2 > len(data): return track, len(data)
            track["mode2"] = "{:04o}".format(_cat62__u16(data[pos:pos + 2]) & 0x0FFF); pos += 2
        elif frn == 25:                 # extendible 3-octet composed track numbers
            while True:
                if pos + 3 > len(data): return track, len(data)
                b = data[pos + 2]; pos += 3
                if not b & 1: break
        elif frn == 26:
            try: extra, pos = _cat62__decode_500_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn == 27:
            try: extra, pos = _cat62__decode_340_ed121(data, pos)
            except ValueError: return track, len(data)
            track.update(extra)
        elif frn in (33, 34):
            pos = _cat62__skip_len_field(data, pos)
        elif frn >= 28:
            continue
        else:
            break
    return track, pos


def _cat62__make_cat062_handler(pub, topic: str = _cat62_TOPIC_062):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat62_decode_cat62_record(data, pos)
            if pos <= previous:
                break
            if len(track) > 2:
                _cat62__pub(pub, track, "cat62", verbose, topic)
                publish_native(pub, native_topic(semantic_topic(
                                   topic.format(source=_asterix_source(track)), track)),
                               asterix_data_block(62, data[previous:pos]),
                               "asterix", profile="cat062")
    return _h

def _cat62__process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)

def _cat62__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]

def _cat62__run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat62__raw_frame_payload(bytes(sample.payload), category)
        except ValueError as exc:
            print("CAT-{} ignored invalid raw Zenoh frame: {}".format(category, exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-{} raw Zenoh input: {}".format(category, input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()

def _cat62__run_cat62(host: str, port: int, udp: bool, handler, verbose: bool):
    """CAT-062: outbound TCP connect with auto-reconnect, or inbound UDP."""
    if udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-62 UDP on 0.0.0.0:{}".format(port), flush=True)
        _cat62__process_stream(_cat62_iter_frames_udp(sock), {_cat62_CAT_062: handler}, verbose)
        return
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            print("CAT-62 TCP connected to {}:{}".format(host, port), flush=True)
            _cat62__process_stream(_cat62_iter_frames_tcp(sock), {_cat62_CAT_062: handler}, verbose)
        except (EOFError, ValueError, ConnectionRefusedError, OSError) as exc:
            print("CAT-62 error: {} — reconnecting in {}s".format(
                exc, _cat62_RECONNECT_DELAY_S), flush=True)
            if sock:
                try: sock.close()
                except Exception: pass
        time.sleep(_cat62_RECONNECT_DELAY_S)



def _cat62_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-062 Ed.1.21 -> Zenoh")
    parser.add_argument("--host", default=os.environ.get("CAT62_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT62_PORT", "50062") or 50062))
    parser.add_argument("--udp", action="store_true", default=os.environ.get("CAT62_UDP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT62_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT62_INPUT_TOPIC", _cat62_RAW_INPUT_TOPIC))
    parser.add_argument("--topic", default=_cat62_TOPIC_062)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.host and not args.udp: parser.error("--host is required unless --udp or --zenoh-raw is selected")
    while True:
        try:
            session = open_session()
            break
        except ZError as exc:
            print("CAT-62 Zenoh connect failed: {} — retry in {}s".format(exc, _cat62_ZENOH_RETRY_S), flush=True)
            time.sleep(_cat62_ZENOH_RETRY_S)
    handler = _cat62__make_cat062_handler(session, args.topic)
    try:
        if args.zenoh_raw: _cat62__run_zenoh_raw(session, args.input_topic, _cat62_CAT_062, handler, args.verbose)
        else: _cat62__run_cat62(args.host, args.port, args.udp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

decode_cat62_record = _cat62_decode_cat62_record


# ==========================================================================
# CAT-063 — Sensor Status Messages, Ed.1.7
#
# Status/health/calibration reports for the individual sensors (radars,
# Mode S stations, etc.) feeding a multi-sensor tracker — the same "system
# status" relationship to CAT-062 (fused system tracks) that CAT-034 has to
# CAT-048 and CAT-019 has to CAT-020.
# ==========================================================================

_cat63_TOPIC_ROOT = topic_root()

_cat63_TOPIC_SENSOR    = _cat63_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat63_RAW_INPUT_TOPIC = "{}/raw/asterix/cat63".format(_cat63_TOPIC_ROOT)
_cat63_CAT_063 = 63

_cat63__CON = {0: "operational", 1: "degraded", 2: "initialization", 3: "not_connected"}


def _cat63__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat63__netbird_ip() -> str:
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


def _cat63__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat63_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat63__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat63__recv_exact(sock, length - 3)
        record_in("cat63", len(data))
        yield cat, data


def _cat63_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat63", len(record))
            yield cat, record
            offset += length


def _cat63_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat63__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat63__decode_060(data: bytes, pos: int, msg: dict) -> int:
    """I063/060 Sensor Connection Status — 1-3 byte FX chain."""
    if pos + 1 > len(data):
        return pos
    b1 = data[pos]; pos += 1
    msg["connection_status"] = _cat63__CON.get((b1 >> 6) & 0x03, "undefined")
    msg["psr_nogo"] = bool(b1 & 0x20)
    msg["ssr_nogo"] = bool(b1 & 0x10)
    msg["mds_nogo"] = bool(b1 & 0x08)
    msg["ads_nogo"] = bool(b1 & 0x04)
    msg["mlt_nogo"] = bool(b1 & 0x02)
    if not (b1 & 0x01):
        return pos

    if pos + 1 > len(data):
        return pos
    b2 = data[pos]; pos += 1
    msg["operational_release_inhibited"] = bool(b2 & 0x80)
    msg["overload_processor"]  = bool(b2 & 0x40)
    msg["overload_transmission"] = bool(b2 & 0x20)
    msg["monitoring_disconnected"] = bool(b2 & 0x10)
    msg["time_source_invalid"] = bool(b2 & 0x08)
    msg["no_plot_warning"] = bool(b2 & 0x04)
    if not (b2 & 0x01):
        return pos

    if pos + 1 > len(data):
        return pos
    b3 = data[pos]; pos += 1
    if b3 & 0x80:                       # TTF EP
        msg["test_target_failure"] = bool(b3 & 0x40)
    if b3 & 0x20:                       # SPO EP
        msg["spoofing_suspected"] = bool(b3 & 0x10)
    # further FX bytes beyond octet 3 are not yet publicly documented; skip
    # any remaining chained bytes rather than misreading them as new fields.
    while b3 & 0x01:
        if pos + 1 > len(data):
            break
        b3 = data[pos]; pos += 1
    return pos


def _cat63_decode_cat063(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat63_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I063/010 SAC/SIC (data source)
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I063/015 Service Identification
            if pos + 1 > len(data): break
            msg["service_id"] = data[pos]; pos += 1
        elif frn == 2:                  # I063/030 Time of Message (1/128 s)
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I063/050 Sensor Identifier (SAC/SIC of the sensor)
            if pos + 2 > len(data): break
            msg["sensor_sac"] = data[pos]; msg["sensor_sic"] = data[pos + 1]; pos += 2
        elif frn == 4:                  # I063/060 Sensor Connection Status (FX)
            pos = _cat63__decode_060(data, pos, msg)
        elif frn == 5:                  # I063/070 Time Stamping Bias (signed, 1 ms)
            if pos + 2 > len(data): break
            msg["time_stamping_bias_ms"] = struct.unpack(">h", data[pos:pos + 2])[0]; pos += 2
        elif frn == 6:                  # I063/080 SSR/Mode S Range/Azimuth Gain and Bias
            if pos + 4 > len(data): break
            msg["ssr_range_gain"] = round(struct.unpack(">h", data[pos:pos + 2])[0] / 100000.0, 6)
            msg["ssr_range_bias_nm"] = round(struct.unpack(">h", data[pos + 2:pos + 4])[0] / 128.0, 4)
            pos += 4
        elif frn == 7:                  # I063/081 SSR/Mode S Azimuth Bias (deg)
            if pos + 2 > len(data): break
            msg["ssr_azimuth_bias_deg"] = round(struct.unpack(">h", data[pos:pos + 2])[0] * (360.0 / 65536.0), 4)
            pos += 2
        elif frn == 8:                  # I063/090 PSR Range/Azimuth Gain and Bias
            if pos + 4 > len(data): break
            msg["psr_range_gain"] = round(struct.unpack(">h", data[pos:pos + 2])[0] / 100000.0, 6)
            msg["psr_range_bias_nm"] = round(struct.unpack(">h", data[pos + 2:pos + 4])[0] / 128.0, 4)
            pos += 4
        elif frn == 9:                  # I063/091 PSR Azimuth Bias (deg)
            if pos + 2 > len(data): break
            msg["psr_azimuth_bias_deg"] = round(struct.unpack(">h", data[pos:pos + 2])[0] * (360.0 / 65536.0), 4)
            pos += 2
        elif frn == 10:                 # I063/092 PSR Elevation Bias (deg)
            if pos + 2 > len(data): break
            msg["psr_elevation_bias_deg"] = round(struct.unpack(">h", data[pos:pos + 2])[0] * (360.0 / 65536.0), 4)
            pos += 2
        elif frn in (11, 12):           # RE / SP
            pos = _cat63__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat63__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat63_CAT_063:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat63__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat63__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat63__raw_frame_payload(bytes(sample.payload), _cat63_CAT_063)
        except ValueError as exc:
            print("CAT-63 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-63 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat63__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat63__process_stream(_cat63_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-63 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-63 TCP disconnected: {}".format(addr), flush=True)


def _cat63__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat63__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-63 Ed.1.7 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-63 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat63__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-63 Ed.1.7 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat63__process_stream(_cat63_iter_frames_udp(sock), handler, verbose)


def _cat63__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat63_decode_cat063(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-63 Ed.1.7"
        topic = _cat63_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat63Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-63 {} status={} sensor={}/{}".format(
                topic, msg.get("connection_status"), msg.get("sensor_sac"), msg.get("sensor_sic")), flush=True)
    return _h


def _cat63_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-063 Ed.1.7 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT63_PORT", "50063") or 50063))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT63_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT63_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT63_INPUT_TOPIC", _cat63_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT63_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat63__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat63__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat63__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-065 — SDPS Service Status Reports, Ed.1.6
#
# Surveillance Data Processing System (the tracker upstream of CAT-062's
# system tracks) health/service status — the SDPS-side companion to CAT-062
# the same way CAT-019 is to CAT-020 or CAT-063 is to CAT-062's sensors.
# Single UAP, 14 FRNs, no position field. Full spec text read directly
# (docs/references/asterix-specs/cat065/cat-1.6.ast), no field kept raw.
# ==========================================================================

_cat65_TOPIC_ROOT = topic_root()

_cat65_TOPIC_SENSOR    = _cat65_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat65_RAW_INPUT_TOPIC = "{}/raw/asterix/cat65".format(_cat65_TOPIC_ROOT)
_cat65_CAT_065 = 65

_cat65__MSG_TYPES = {1: "sdps_status", 2: "end_of_batch", 3: "service_status_report"}

_cat65__NOGO = {0: "operational", 1: "degraded", 2: "not_connected", 3: "unknown"}

_cat65__PSS = {0: "not_applicable", 1: "sdps1_selected", 2: "sdps2_selected", 3: "sdps3_selected"}

_cat65__SERVICE_STATUS = {
    1: "degradation", 2: "degradation_ended", 3: "main_radar_out_of_service",
    4: "interrupted_by_operator", 5: "interrupted_due_to_contingency",
    6: "ready_for_restart_after_contingency", 7: "ended_by_operator",
    8: "failure_of_user_main_radar", 9: "restarted_by_operator",
    10: "main_radar_becoming_operational", 11: "main_radar_becoming_degraded",
    12: "continuity_interrupted_disconnection", 13: "continuity_restarted",
    14: "synchronised_on_backup_radar", 15: "synchronised_on_main_radar",
    16: "main_and_backup_radar_failed",
}


def _cat65__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat65__netbird_ip() -> str:
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


def _cat65__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat65_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat65__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat65__recv_exact(sock, length - 3)
        record_in("cat65", len(data))
        yield cat, data


def _cat65_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat65", len(record))
            yield cat, record
            offset += length


def _cat65_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat65__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat65_decode_cat065(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat65_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I065/010 SAC/SIC
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I065/000 Message Type
            if pos >= len(data): break
            msg["msg_type"] = _cat65__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I065/015 Service Identification
            if pos >= len(data): break
            msg["service_id"] = data[pos]; pos += 1
        elif frn == 3:                  # I065/030 Time of Message
            if pos + 3 > len(data): break
            msg["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 4:                  # I065/020 Batch Number
            if pos >= len(data): break
            msg["batch_number"] = data[pos]; pos += 1
        elif frn == 5:                  # I065/040 SDPS Configuration and Status
            if pos >= len(data): break
            b = data[pos]; pos += 1
            msg["sdps_status"] = _cat65__NOGO.get((b >> 6) & 0x03, "unknown")
            if b & 0x20: msg["overload"] = True
            if b & 0x10: msg["time_source_invalid"] = True
            msg["processing_system"] = _cat65__PSS.get((b >> 2) & 0x03, "unknown")
            msg["track_renumbering_toggle"] = bool(b & 0x02)
        elif frn == 6:                  # I065/050 Service Status Report
            if pos >= len(data): break
            msg["service_status_report"] = _cat65__SERVICE_STATUS.get(data[pos], "code_{}".format(data[pos])); pos += 1
        elif 7 <= frn <= 11:            # spare (no encoded item)
            continue
        elif frn in (12, 13):           # I065/RE, I065/SP
            pos = _cat65__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat65__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat65_CAT_065:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat65__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat65__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat65__raw_frame_payload(bytes(sample.payload), _cat65_CAT_065)
        except ValueError as exc:
            print("CAT-65 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-65 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat65__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat65__process_stream(_cat65_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-65 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-65 TCP disconnected: {}".format(addr), flush=True)


def _cat65__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat65__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-65 Ed.1.6 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-65 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat65__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-65 Ed.1.6 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat65__process_stream(_cat65_iter_frames_udp(sock), handler, verbose)


def _cat65__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat65_decode_cat065(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-65 Ed.1.6"
        topic = _cat65_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat65Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-65 {} status={}".format(topic, msg.get("sdps_status")), flush=True)
    return _h


def _cat65_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-065 Ed.1.6 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT65_PORT", "50065") or 50065))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT65_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT65_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT65_INPUT_TOPIC", _cat65_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT65_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat65__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat65__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat65__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-150 — MADAP Plan Server - Flight Data Message, Ed.3.0
#
# Maastricht UAC's legacy MADAP flight-data-processing system distributing
# flight plan / correlation / conflict-alert data to clients. Single UAP,
# 27 FRNs — no direct position field is ever transmitted in this edition:
# I150/151 (WGS-84 route point position) is defined in the spec's item
# catalogue but is genuinely absent from this edition's UAP list (confirmed
# by reading the raw uap block directly, not a summarization artifact — the
# UAP only goes up to I150/171, I150/151 is simply never referenced), so
# route points carry local Cartesian offsets (I150/150) instead. Like
# CAT-032, this category correlates flight plans to tracks; it does not
# report a surveillance position.
#
# Many items are transmitted as ASCII digit/character strings rather than
# packed binary (a MADAP convention, not an ASTERIX default) — e.g. I150/130
# Cleared Flight Level is 3 ASCII digits, not a binary integer. Decoded
# literally as the spec describes rather than reinterpreted.
#
# Full spec text read directly (docs/references/asterix-specs/cat150/cat-3.0.ast).
# ==========================================================================

_cat150_TOPIC_ROOT = topic_root()

_cat150_TOPIC_SENSOR    = _cat150_TOPIC_ROOT + "/air/{source}/madap/civ/flightplan"
_cat150_RAW_INPUT_TOPIC = "{}/raw/asterix/cat150".format(_cat150_TOPIC_ROOT)
_cat150_CAT_150 = 150

_cat150__MSG_TYPES = {
    1: "flight_plan_creation", 2: "flight_plan_modification", 3: "flight_plan_repetition",
    4: "manual_deletion", 5: "automatic_deletion", 6: "beyond_extraction_area",
    251: "short_term_conflict_alert", 252: "correlations", 253: "decorrelations",
    254: "start_of_background_loop", 255: "end_of_background_loop",
}

_cat150__ROUTE_POINT_TYPE = {
    1: "point", 2: "bearing_distance", 3: "latlon_short", 4: "latlon_long",
    5: "xy_coordinate", 6: "georeference", 14: "airport",
}


def _cat150__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat150__netbird_ip() -> str:
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


def _cat150__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat150_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat150__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat150__recv_exact(sock, length - 3)
        record_in("cat150", len(data))
        yield cat, data


def _cat150_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat150", len(record))
            yield cat, record
            offset += length


def _cat150_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat150__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat150__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat150__ascii(b: bytes) -> str: return b.decode("ascii", "replace").strip("\x00 ")


def _cat150_decode_cat150(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat150_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I150/010 Destination ID
            if pos + 2 > len(data): break
            msg["dest_centre"] = data[pos]; msg["dest_workstation"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I150/020 Source ID
            if pos + 2 > len(data): break
            msg["source_centre"] = data[pos]; msg["source_workstation"] = data[pos + 1]; pos += 2
        elif frn == 2:                  # I150/030 Message Type
            if pos >= len(data): break
            msg["msg_type"] = _cat150__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 3:                  # I150/040 Plan Reference Number
            if pos + 2 > len(data): break
            msg["plan_ref_number"] = _cat150__u16(data[pos:pos + 2]); pos += 2
        elif frn == 4:                  # I150/050 Callsign
            if pos + 7 > len(data): break
            s = _cat150__ascii(data[pos:pos + 7]); pos += 7
            if s: msg["callsign"] = s
        elif frn == 5:                  # I150/060 Present Mode 3A
            if pos + 4 > len(data): break
            s = _cat150__ascii(data[pos:pos + 4]); pos += 4
            if s: msg["present_squawk"] = s
        elif frn == 6:                  # I150/070 Next Mode 3A
            if pos + 4 > len(data): break
            s = _cat150__ascii(data[pos:pos + 4]); pos += 4
            if s: msg["next_squawk"] = s
        elif frn == 7:                  # I150/080 Departure Aerodrome
            if pos + 4 > len(data): break
            s = _cat150__ascii(data[pos:pos + 4]); pos += 4
            if s: msg["departure_airport"] = s
        elif frn == 8:                  # I150/090 Destination Aerodrome
            if pos + 4 > len(data): break
            s = _cat150__ascii(data[pos:pos + 4]); pos += 4
            if s: msg["destination_airport"] = s
        elif frn == 9:                  # I150/100 Type Flags
            if pos >= len(data): break
            b = data[pos]; pos += 1
            if b & 0x80: msg["general_air_traffic"] = True
            if b & 0x40: msg["operational_air_traffic"] = True
            if b & 0x04: msg["complete_flight_plan"] = True
            if b & 0x02: msg["short_flight_plan"] = True
        elif frn == 10:                 # I150/110 Status Flags
            if pos >= len(data): break
            b = data[pos]; pos += 1
            if b & 0x40: msg["in_hold"] = True
            if b & 0x20: msg["rvsm_equipped"] = True
            if b & 0x10: msg["rvsm_capable"] = True
            if b & 0x08: msg["rvsm_exempted"] = True
        elif frn == 11:                 # I150/120 Aircraft Type
            if pos + 7 > len(data): break
            noa = _cat150__ascii(data[pos:pos + 2])
            toa = _cat150__ascii(data[pos + 2:pos + 6])
            wt = _cat150__ascii(data[pos + 6:pos + 7]); pos += 7
            if noa: msg["number_of_aircraft"] = noa
            if toa: msg["aircraft_type"] = toa
            if wt: msg["wake_turbulence_cat"] = wt
        elif frn == 12:                 # I150/130 Cleared Flight Level
            if pos + 3 > len(data): break
            s = _cat150__ascii(data[pos:pos + 3]); pos += 3
            if s: msg["cleared_flight_level"] = s
        elif frn == 13:                 # I150/140 Route Points, Description (repetitive 1, 12B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            points = []
            for _ in range(rep):
                if pos + 12 > len(data): break
                t = data[pos]
                e = _cat150__ascii(data[pos + 1:pos + 12]); pos += 12
                points.append({"type": _cat150__ROUTE_POINT_TYPE.get(t, "type_{}".format(t)), "description": e})
            if points: msg["route_point_descriptions"] = points
        elif frn == 14:                 # I150/150 Route Points, Coordinates (repetitive 1, 4B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            points = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                points.append({
                    "x_nm": round(_cat150__s16(data[pos:pos + 2]) / 64.0, 3),
                    "y_nm": round(_cat150__s16(data[pos + 2:pos + 4]) / 64.0, 3),
                })
                pos += 4
            if points: msg["route_point_coordinates"] = points
        elif frn == 15:                 # I150/160 Route Points, Time (repetitive 1, 4B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            times = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                hh = _cat150__ascii(data[pos:pos + 2])
                mm = _cat150__ascii(data[pos + 2:pos + 4]); pos += 4
                times.append("{}:{}".format(hh, mm))
            if times: msg["route_point_times"] = times
        elif frn == 16:                 # I150/170 Route Points, Flight Level (repetitive 1, 3B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            levels = []
            for _ in range(rep):
                if pos + 3 > len(data): break
                levels.append(_cat150__ascii(data[pos:pos + 3])); pos += 3
            if levels: msg["route_point_flight_levels"] = levels
        elif frn == 17:                 # I150/180 Route Points, Speed (repetitive 1, 4B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            speeds = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                speeds.append(_cat150__ascii(data[pos:pos + 4])); pos += 4
            if speeds: msg["route_point_speeds"] = speeds
        elif frn == 18:                 # I150/190 Controller ID
            if pos + 2 > len(data): break
            s = _cat150__ascii(data[pos:pos + 2]); pos += 2
            if s: msg["controller_id"] = s
        elif frn == 19:                 # I150/200 Field 18 (repetitive 1, 1B/entry ASCII chars)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            if pos + rep > len(data): break
            s = _cat150__ascii(data[pos:pos + rep]); pos += rep
            if s: msg["field_18"] = s
        elif frn == 20:                 # I150/210 Correlated Track Number
            if pos + 2 > len(data): break
            msg["correlated_track_num"] = _cat150__u16(data[pos:pos + 2]); pos += 2
        elif frn == 21:                 # I150/220 Maximum Plan Count
            if pos + 2 > len(data): break
            msg["max_plan_count"] = _cat150__u16(data[pos:pos + 2]); pos += 2
        elif frn == 22:                 # I150/230 Number of Plans
            if pos + 2 > len(data): break
            msg["number_of_plans"] = _cat150__u16(data[pos:pos + 2]); pos += 2
        elif frn == 23:                 # I150/240 Newly Correlated Plans (repetitive 1, 4B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            pairs = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                pairs.append({"plan": _cat150__u16(data[pos:pos + 2]), "track": _cat150__u16(data[pos + 2:pos + 4])})
                pos += 4
            if pairs: msg["newly_correlated_plans"] = pairs
        elif frn == 24:                 # I150/250 Newly De-correlated Plans (repetitive 1, 2B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            plans = []
            for _ in range(rep):
                if pos + 2 > len(data): break
                plans.append(_cat150__u16(data[pos:pos + 2])); pos += 2
            if plans: msg["newly_decorrelated_plans"] = plans
        elif frn == 25:                 # I150/251 Tracks in Conflict (repetitive 1, 4B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            pairs = []
            for _ in range(rep):
                if pos + 4 > len(data): break
                pairs.append({"track1": _cat150__u16(data[pos:pos + 2]), "track2": _cat150__u16(data[pos + 2:pos + 4])})
                pos += 4
            if pairs: msg["tracks_in_conflict"] = pairs
        elif frn == 26:                 # I150/171 Route Points, Requested Flight Level (repetitive 1, 3B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            levels = []
            for _ in range(rep):
                if pos + 3 > len(data): break
                levels.append(_cat150__ascii(data[pos:pos + 3])); pos += 3
            if levels: msg["route_point_requested_flight_levels"] = levels
        else:
            break
    return msg if msg else None


def _cat150__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat150_CAT_150:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat150__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat150__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat150__raw_frame_payload(bytes(sample.payload), _cat150_CAT_150)
        except ValueError as exc:
            print("CAT-150 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-150 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat150__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat150__process_stream(_cat150_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-150 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-150 TCP disconnected: {}".format(addr), flush=True)


def _cat150__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat150__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-150 Ed.3.0 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-150 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat150__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-150 Ed.3.0 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat150__process_stream(_cat150_iter_frames_udp(sock), handler, verbose)


def _cat150__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat150_decode_cat150(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-150 Ed.3.0"
        msg.setdefault("sac", msg.get("source_centre", 0))
        msg.setdefault("sic", msg.get("source_workstation", 0))
        topic = _cat150_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat150Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-150 {} msg_type={}".format(topic, msg.get("msg_type")), flush=True)
    return _h


def _cat150_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-150 Ed.3.0 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT150_PORT", "50150") or 50150))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT150_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT150_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT150_INPUT_TOPIC", _cat150_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT150_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat150__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat150__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat150__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-205 — Radio Direction Finder Reports, Ed.1.0
#
# RDF network triangulating a radio transmitter's position (typically an
# aircraft's VHF radio) from bearing lines-of-position at one or more
# sensors. A genuine positioned track, unlike CAT-032/150 — I205/050 gives
# WGS-84 lat/lon directly when present (the transmitter's estimated
# position for message types 1/3, or the RDF sensor's own position for
# type 2), with I205/060's Cartesian offset (relative to a System Reference
# Point, per the spec's own cross-reference to CAT-025 item 600) as a
# fallback via --site-lat/--site-lon like other categories in this file.
#
# Full spec text read directly
# (docs/references/asterix-specs/cat205/cat-1.0.ast), no field kept raw.
# ==========================================================================

_cat205_TOPIC_ROOT = topic_root()

_cat205_TOPIC_AIR      = _cat205_TOPIC_ROOT + "/air/{source}/rdf/unknown/aircraft"
_cat205_RAW_INPUT_TOPIC = "{}/raw/asterix/cat205".format(_cat205_TOPIC_ROOT)
_cat205_CAT_205 = 205

_cat205__MSG_TYPES = {
    1: "system_position_report", 2: "system_bearing_report",
    3: "conflicting_transmission_position", 4: "detection_end_report",
    5: "sensor_data_report",
}


def _cat205__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat205__netbird_ip() -> str:
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


def _cat205__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat205_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat205__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat205__recv_exact(sock, length - 3)
        record_in("cat205", len(data))
        yield cat, data


def _cat205_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat205", len(record))
            yield cat, record
            offset += length


def _cat205_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat205__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat205__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat205__s16(b: bytes) -> int: return struct.unpack(">h", b)[0]

def _cat205__s24(b: bytes) -> int:
    v = int.from_bytes(b, "big")
    return v - (1 << 24) if v & (1 << 23) else v

def _cat205__s32(b: bytes) -> int: return struct.unpack(">i", b)[0]


def _cat205__cartesian_to_wgs84(origin_lat: float, origin_lon: float, x_m: float, y_m: float):
    """Local CAT-205 X=east/Y=north metres to WGS-84, relative to a System Reference Point."""
    lat = origin_lat + y_m / 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(origin_lat))
    lon = origin_lon + (x_m / lon_scale if lon_scale else 0.0)
    return lat, lon


def _cat205_decode_cat205_record(data: bytes, pos: int, site_lat=None, site_lon=None):
    """Decode exactly the public EUROCONTROL CAT-205 Edition 1.0 UAP."""
    fspec, pos = _cat205_parse_fspec(data, pos)
    track = {"_ts": time.time(), "_src": "ASTERIX CAT-205 Ed.1.0"}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I205/010 Data Source Identifier
            if pos + 2 > len(data): return track, len(data)
            track["sac"], track["sic"] = data[pos], data[pos + 1]; pos += 2
        elif frn == 1:                  # I205/015 Service Identification
            if pos >= len(data): return track, len(data)
            track["service_id"] = data[pos]; pos += 1
        elif frn == 2:                  # I205/000 Message Type
            if pos >= len(data): return track, len(data)
            track["msg_type"] = _cat205__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 3:                  # I205/030 Time of Day
            if pos + 3 > len(data): return track, len(data)
            track["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 4:                  # I205/040 Report Number
            if pos >= len(data): return track, len(data)
            track["report_number"] = data[pos]; pos += 1
        elif frn == 5:                  # I205/090 Radio Channel Name
            if pos + 7 > len(data): return track, len(data)
            s = data[pos:pos + 7].decode("ascii", "replace").strip("\x00 "); pos += 7
            if s: track["radio_channel"] = s
        elif frn == 6:                  # I205/050 Position WGS-84
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / 2**25
            track["lat_deg"] = round(_cat205__s32(data[pos:pos + 4]) * scale, 7)
            track["lon_deg"] = round(_cat205__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
        elif frn == 7:                  # I205/060 Position Cartesian
            if pos + 6 > len(data): return track, len(data)
            x_m = _cat205__s24(data[pos:pos + 3]) * 0.5
            y_m = _cat205__s24(data[pos + 3:pos + 6]) * 0.5; pos += 6
            track["cart_x_m"] = round(x_m, 2); track["cart_y_m"] = round(y_m, 2)
            if site_lat is not None and site_lon is not None:
                lat, lon = _cat205__cartesian_to_wgs84(site_lat, site_lon, x_m, y_m)
                track.setdefault("lat_deg", round(lat, 7)); track.setdefault("lon_deg", round(lon, 7))
        elif frn == 8:                  # I205/070 Local Bearing
            if pos + 2 > len(data): return track, len(data)
            track["local_bearing_deg"] = round(_cat205__u16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif frn == 9:                  # I205/080 System Bearing
            if pos + 2 > len(data): return track, len(data)
            track["system_bearing_deg"] = round(_cat205__u16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif frn == 10:                 # I205/100 Quality of Measurement
            if pos >= len(data): return track, len(data)
            track["measurement_quality_raw"] = data[pos]; pos += 1
        elif frn == 11:                 # I205/110 Estimated Uncertainty
            if pos >= len(data): return track, len(data)
            track["position_uncertainty_m"] = data[pos] * 100; pos += 1
        elif frn == 12:                 # I205/120 Contributing Sensors (repetitive 1, 1B/entry)
            if pos >= len(data): return track, len(data)
            rep = data[pos]; pos += 1
            sensors = []
            for _ in range(rep):
                if pos >= len(data): break
                sensors.append(data[pos]); pos += 1
            if sensors: track["contributing_sensors"] = sensors
        elif frn == 13:                 # I205/130 Conflicting Transmitter Position WGS-84
            if pos + 8 > len(data): return track, len(data)
            scale = 180.0 / 2**25
            track["conflicting_lat_deg"] = round(_cat205__s32(data[pos:pos + 4]) * scale, 7)
            track["conflicting_lon_deg"] = round(_cat205__s32(data[pos + 4:pos + 8]) * scale, 7); pos += 8
        elif frn == 14:                 # I205/140 Conflicting Transmitter Position Cartesian
            if pos + 6 > len(data): return track, len(data)
            track["conflicting_cart_x_m"] = round(_cat205__s24(data[pos:pos + 3]) * 0.5, 2)
            track["conflicting_cart_y_m"] = round(_cat205__s24(data[pos + 3:pos + 6]) * 0.5, 2); pos += 6
        elif frn == 15:                 # I205/150 Conflicting Transmitter Estimated Uncertainty
            if pos >= len(data): return track, len(data)
            track["conflicting_position_uncertainty_m"] = data[pos] * 100; pos += 1
        elif frn == 16:                 # I205/160 Track Number
            if pos + 2 > len(data): return track, len(data)
            track["track_num"] = _cat205__u16(data[pos:pos + 2]); pos += 2
        elif frn == 17:                 # I205/170 Sensor Identification
            if pos >= len(data): return track, len(data)
            track["sensor_id"] = data[pos]; pos += 1
        elif frn == 18:                 # I205/180 Signal Level
            if pos + 2 > len(data): return track, len(data)
            track["signal_level_dbuv"] = round(_cat205__s16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif frn == 19:                 # I205/190 Signal Quality
            if pos >= len(data): return track, len(data)
            track["signal_quality"] = data[pos]; pos += 1
        elif frn == 20:                 # I205/200 Signal Elevation
            if pos + 2 > len(data): return track, len(data)
            track["signal_elevation_deg"] = round(_cat205__s16(data[pos:pos + 2]) * 0.01, 2); pos += 2
        elif frn == 21:                 # I205/SP
            pos = _cat205__skip_len_field(data, pos)
        else:
            return track, len(data)
    return track, pos


def _cat205__make_handler(session, site, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        pos = 0
        while pos < len(data):
            previous = pos
            track, pos = _cat205_decode_cat205_record(data, pos, site[0], site[1])
            if pos <= previous:
                break
            if "lat_deg" not in track or "lon_deg" not in track:
                if verbose:
                    print("cat205 record without map position; configure CAT205_SITE_LAT/LON for cartesian-only reports", flush=True)
                continue
            topic = _cat205_TOPIC_AIR.format(source=_asterix_source(track))
            publish_dual(session, topic, track, AsterixCat205Track)
            publish_native(session, native_topic(semantic_topic(topic, track)),
                           asterix_data_block(205, data[previous:pos]),
                           "asterix", profile="cat205")
            if verbose:
                print("cat205 {} -> {}".format(track.get("track_num", "target"), topic), flush=True)
    return _h


def _cat205__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat205_CAT_205:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat205__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat205__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat205__raw_frame_payload(bytes(sample.payload), _cat205_CAT_205)
        except ValueError as exc:
            print("CAT-205 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-205 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat205__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat205__process_stream(_cat205_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-205 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-205 TCP disconnected: {}".format(addr), flush=True)


def _cat205__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat205__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-205 Ed.1.0 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-205 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat205__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-205 Ed.1.0 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat205__process_stream(_cat205_iter_frames_udp(sock), handler, verbose)


def _cat205_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-205 Ed.1.0 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT205_PORT", "50205") or 50205))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT205_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT205_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT205_INPUT_TOPIC", _cat205_RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_cat205__env_float("CAT205_SITE_LAT"))
    parser.add_argument("--site-lon", type=float, default=_cat205__env_float("CAT205_SITE_LON"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT205_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = open_session()
    handler = _cat205__make_handler(session, site, args.verbose)
    try:
        print("Zenoh CAT-205 topics:", _cat205_TOPIC_AIR, flush=True)
        if args.zenoh_raw: _cat205__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat205__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-240 — Radar Video Transmission, Ed.1.3
#
# Raw radar video (amplitude sample) transmission per radial: no position,
# no track — this is the actual signal-level video data a radar produces
# before any plot extraction, not a target report. The structural envelope
# (message type, radial header, bit-resolution/compression indicator,
# octet/cell counters, time of day) is decoded fully; the video cell block
# itself (I240/050/051/052) has no further ASTERIX-defined internal
# structure beyond what I240/048's resolution field already states (1-32
# bits/cell, proprietary per-vendor signal encoding) so it is preserved as
# a raw hex blob rather than guessed apart. Per the spec, a single message
# can carry up to ~64KB of video data (I240/052) — this is a genuinely
# high-volume category; anyone consuming its JSON/native views should be
# aware messages are much larger than typical ASTERIX records.
#
# Full spec text read directly (docs/references/asterix-specs/cat240/cat-1.3.ast).
# ==========================================================================

_cat240_TOPIC_ROOT = topic_root()

_cat240_TOPIC_SENSOR    = _cat240_TOPIC_ROOT + "/land/{source}/radar/neutral/video"
_cat240_RAW_INPUT_TOPIC = "{}/raw/asterix/cat240".format(_cat240_TOPIC_ROOT)
_cat240_CAT_240 = 240

_cat240__MSG_TYPES = {1: "video_summary", 2: "video_message"}

_cat240__RESOLUTION = {
    1: "monobit_1bit", 2: "low_2bit", 3: "medium_4bit",
    4: "high_8bit", 5: "very_high_16bit", 6: "ultra_high_32bit",
}


def _cat240__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat240__netbird_ip() -> str:
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


def _cat240__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat240_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat240__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat240__recv_exact(sock, length - 3)
        record_in("cat240", len(data))
        yield cat, data


def _cat240_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat240", len(record))
            yield cat, record
            offset += length


def _cat240_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat240__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat240__u16(b: bytes) -> int: return struct.unpack(">H", b)[0]

def _cat240__u32(b: bytes) -> int: return struct.unpack(">I", b)[0]


def _cat240_decode_cat240(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat240_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I240/010 Data Source Identifier
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I240/000 Message Type
            if pos >= len(data): break
            msg["msg_type"] = _cat240__MSG_TYPES.get(data[pos], "type_{}".format(data[pos])); pos += 1
        elif frn == 2:                  # I240/020 Video Record Header
            if pos + 4 > len(data): break
            msg["sequence_number"] = _cat240__u32(data[pos:pos + 4]); pos += 4
        elif frn == 3:                  # I240/030 Video Summary (repetitive 1, ASCII chars)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            if pos + rep > len(data): break
            s = data[pos:pos + rep].decode("ascii", "replace").strip("\x00 "); pos += rep
            if s: msg["video_summary"] = s
        elif frn == 4:                  # I240/040 Video Header Nano
            if pos + 12 > len(data): break
            msg["start_azimuth_deg"] = round(_cat240__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 3)
            msg["end_azimuth_deg"] = round(_cat240__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0, 3)
            msg["start_range_cells"] = _cat240__u32(data[pos + 4:pos + 8])
            msg["cell_duration_ns"] = _cat240__u32(data[pos + 8:pos + 12]); pos += 12
        elif frn == 5:                  # I240/041 Video Header Femto
            if pos + 12 > len(data): break
            msg["start_azimuth_deg"] = round(_cat240__u16(data[pos:pos + 2]) * 360.0 / 65536.0, 3)
            msg["end_azimuth_deg"] = round(_cat240__u16(data[pos + 2:pos + 4]) * 360.0 / 65536.0, 3)
            msg["start_range_cells"] = _cat240__u32(data[pos + 4:pos + 8])
            msg["cell_duration_fs"] = _cat240__u32(data[pos + 8:pos + 12]); pos += 12
        elif frn == 6:                  # I240/048 Video Cells Resolution & Compression Indicator
            if pos + 2 > len(data): break
            b1 = data[pos]; res = data[pos + 1]; pos += 2
            msg["compression_applied"] = bool(b1 & 0x80)
            msg["bit_resolution"] = _cat240__RESOLUTION.get(res, "resolution_{}".format(res))
        elif frn == 7:                  # I240/049 Video Octets & Video Cells Counters
            if pos + 5 > len(data): break
            msg["valid_octets"] = _cat240__u16(data[pos:pos + 2])
            msg["valid_cells"] = int.from_bytes(data[pos + 2:pos + 5], "big"); pos += 5
        elif frn == 8:                  # I240/050 Video Block Low Data Volume (repetitive 1, 4B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            if pos + rep * 4 > len(data): break
            msg["video_block_hex"] = data[pos:pos + rep * 4].hex(); pos += rep * 4
            msg["video_block_cells"] = rep
        elif frn == 9:                  # I240/051 Video Block Medium Data Volume (repetitive 1, 64B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            if pos + rep * 64 > len(data): break
            msg["video_block_hex"] = data[pos:pos + rep * 64].hex(); pos += rep * 64
            msg["video_block_cells"] = rep
        elif frn == 10:                 # I240/052 Video Block High Data Volume (repetitive 1, 256B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            if pos + rep * 256 > len(data): break
            msg["video_block_hex"] = data[pos:pos + rep * 256].hex(); pos += rep * 256
            msg["video_block_cells"] = rep
        elif frn == 11:                 # I240/140 Time of Day
            if pos + 3 > len(data): break
            msg["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn in (12, 13):           # I240/RE, I240/SP
            pos = _cat240__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat240__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat240_CAT_240:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat240__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat240__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat240__raw_frame_payload(bytes(sample.payload), _cat240_CAT_240)
        except ValueError as exc:
            print("CAT-240 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-240 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat240__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat240__process_stream(_cat240_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-240 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-240 TCP disconnected: {}".format(addr), flush=True)


def _cat240__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat240__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-240 Ed.1.3 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-240 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat240__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-240 Ed.1.3 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat240__process_stream(_cat240_iter_frames_udp(sock), handler, verbose)


def _cat240__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat240_decode_cat240(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-240 Ed.1.3"
        topic = _cat240_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat240Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-240 {} msg_type={}".format(topic, msg.get("msg_type")), flush=True)
    return _h


def _cat240_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-240 Ed.1.3 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT240_PORT", "50240") or 50240))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT240_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT240_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT240_INPUT_TOPIC", _cat240_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT240_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat240__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat240__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat240__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()


# ==========================================================================
# CAT-247 — Version Number Exchange, Ed.1.3
#
# The last category in this catalogue sweep: a source reports which
# edition of each ASTERIX category it is transmitting (I247/550), so a
# receiver can pick the right decoder profile without out-of-band
# coordination. Single UAP, 7 FRNs, no position — status/metadata only.
# Full spec text read directly
# (docs/references/asterix-specs/cat247/cat-1.3.ast), no field kept raw.
# ==========================================================================

_cat247_TOPIC_ROOT = topic_root()

_cat247_TOPIC_SENSOR    = _cat247_TOPIC_ROOT + "/land/{source}/radar/neutral/sensor"
_cat247_RAW_INPUT_TOPIC = "{}/raw/asterix/cat247".format(_cat247_TOPIC_ROOT)
_cat247_CAT_247 = 247


def _cat247__env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0


def _cat247__netbird_ip() -> str:
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


def _cat247__recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def _cat247_iter_frames_tcp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = _cat247__recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = _cat247__recv_exact(sock, length - 3)
        record_in("cat247", len(data))
        yield cat, data


def _cat247_iter_frames_udp(sock: socket.socket):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in("cat247", len(record))
            yield cat, record
            offset += length


def _cat247_parse_fspec(data: bytes, pos: int):
    bits = []
    while pos < len(data):
        byte = data[pos]; pos += 1
        for shift in range(7, 0, -1):
            bits.append(bool(byte & (1 << shift)))
        if not (byte & 0x01):
            break
    return bits, pos


def _cat247__skip_len_field(data: bytes, pos: int) -> int:
    if pos >= len(data):
        return len(data)
    length = data[pos]
    return pos + length if length >= 1 and pos + length <= len(data) else len(data)


def _cat247_decode_cat247(data: bytes) -> dict | None:
    if len(data) < 1:
        return None
    fspec, pos = _cat247_parse_fspec(data, 0)
    msg = {}

    for frn, present in enumerate(fspec):
        if not present:
            continue
        if frn == 0:                    # I247/010 Data Source Identifier
            if pos + 2 > len(data): break
            msg["sac"] = data[pos]; msg["sic"] = data[pos + 1]; pos += 2
        elif frn == 1:                  # I247/015 Service Identification
            if pos >= len(data): break
            msg["service_id"] = data[pos]; pos += 1
        elif frn == 2:                  # I247/140 Time of Day
            if pos + 3 > len(data): break
            msg["tod_s"] = int.from_bytes(data[pos:pos + 3], "big") / 128.0; pos += 3
        elif frn == 3:                  # I247/550 Category Version Number Report (repetitive 1, 3B/entry)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            versions = []
            for _ in range(rep):
                if pos + 3 > len(data): break
                versions.append({
                    "category": data[pos],
                    "main_version": data[pos + 1],
                    "sub_version": data[pos + 2],
                })
                pos += 3
            if versions: msg["category_versions"] = versions
        elif frn == 4:                  # spare (no encoded item)
            continue
        elif frn in (5, 6):             # I247/SP, I247/RE
            pos = _cat247__skip_len_field(data, pos)
        else:
            break
    return msg if msg else None


def _cat247__process_stream(frame_iter, handler, verbose: bool):
    for cat, data in frame_iter:
        if cat == _cat247_CAT_247:
            handler(data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def _cat247__raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def _cat247__run_zenoh_raw(session, input_topic: str, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = _cat247__raw_frame_payload(bytes(sample.payload), _cat247_CAT_247)
        except ValueError as exc:
            print("CAT-247 ignored invalid raw Zenoh frame: {}".format(exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-247 raw Zenoh input: {}".format(input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def _cat247__process_tcp_conn(conn, addr, handler, verbose):
    try:
        _cat247__process_stream(_cat247_iter_frames_tcp(conn), handler, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("CAT-247 TCP protocol error from {}: {}".format(addr, exc), flush=True)
    finally:
        conn.close()
        print("CAT-247 TCP disconnected: {}".format(addr), flush=True)


def _cat247__run_inbound(port: int, use_tcp: bool, handler, verbose: bool):
    ip = _cat247__netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("CAT-247 Ed.1.3 TCP server on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("CAT-247 TCP connected: {}".format(addr), flush=True)
            threading.Thread(target=_cat247__process_tcp_conn, args=(conn, addr, handler, verbose), daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("CAT-247 Ed.1.3 UDP on 0.0.0.0:{}  (send to {}:{})".format(port, ip, port), flush=True)
        _cat247__process_stream(_cat247_iter_frames_udp(sock), handler, verbose)


def _cat247__make_handler(session, verbose_default: bool):
    def _h(data: bytes, verbose: bool):
        msg = _cat247_decode_cat247(data)
        if msg is None:
            return
        msg["_ts"] = time.time()
        msg["_src"] = "ASTERIX CAT-247 Ed.1.3"
        topic = _cat247_TOPIC_SENSOR.format(source=_asterix_source(msg))
        publish_dual(session, topic, msg, AsterixCat247Status, wrapper_field="sensor")
        if verbose:
            print("PUB CAT-247 {} versions={}".format(topic, len(msg.get("category_versions", []))), flush=True)
    return _h


def _cat247_main():
    parser = argparse.ArgumentParser(description="ASTERIX CAT-247 Ed.1.3 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT247_PORT", "50247") or 50247))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT247_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT247_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT247_INPUT_TOPIC", _cat247_RAW_INPUT_TOPIC))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port:
        parser.error("--port or CAT247_PORT is required unless --zenoh-raw is selected")
    session = open_session()
    handler = _cat247__make_handler(session, args.verbose)
    try:
        if args.zenoh_raw: _cat247__run_zenoh_raw(session, args.input_topic, handler, args.verbose)
        else: _cat247__run_inbound(args.port, args.tcp, handler, args.verbose)
    except KeyboardInterrupt: pass
    finally: session.close()

# ============================================================================
# Unified launcher and compatibility helpers
# ============================================================================

def _category_uses_raw(wanted: int) -> bool:
    if not (
        os.environ.get("UDP_INGRESS_PORT", "").strip()
        or os.environ.get("ASTERIX_PORT", "").strip()
        or os.environ.get("ASTERIX_ZENOH_UPSTREAM_ENDPOINT", "").strip()
    ):
        return False
    categories = [item.strip() for item in os.environ.get("ASTERIX_CATEGORIES", "34,48").split(",")]
    return str(wanted) in categories


def _bundle_main() -> None:
    children: list[tuple[str, str, list[str]]] = []
    script = "protocols/vendors/asterix/cat.py"
    if os.environ.get("ASTERIX_ZENOH_UPSTREAM_ENDPOINT", "").strip():
        children.append((
            "asterix-bridge",
            "bridges/asterix_bridge.py",
            [],
        ))
    if (
        os.environ.get("UDP_INGRESS_PORT", "").strip()
        or os.environ.get("ASTERIX_PORT", "").strip()
    ):
        children.append(("udp-ingress", "bridges/udp_ingress_bridge.py", []))

    for category in (1, 2, 4, 7, 8, 9, 10, 11, 15, 16, 17, 18, 19, 20, 21, 23, 25, 32, 34, 48, 63, 65, 150, 205, 240, 247):
        port = os.environ.get(f"CAT{category}_PORT", "").strip()
        tcp = os.environ.get(f"CAT{category}_TCP", "") == "1"
        args = ["--category", str(category)]
        if _category_uses_raw(category):
            children.append((
                f"asterix-cat{category}-raw",
                script,
                args + ["--zenoh-raw"],
            ))
        if port:
            direct_args = args + ["--port", port]
            if tcp:
                direct_args.append("--tcp")
            children.append((f"asterix-cat{category}", script, direct_args))

    args = ["--category", "62"]
    if _category_uses_raw(62):
        children.append(("asterix-cat62-raw", script, args + ["--zenoh-raw"]))
    if os.environ.get("CAT62_UDP", "") == "1":
        children.append(("asterix-cat62", script, args + ["--udp", "--port", os.environ.get("CAT62_PORT", "50062")]))
    elif os.environ.get("CAT62_HOST", "").strip() or os.environ.get("RADAR_HOST", "").strip():
        children.append((
            "asterix-cat62",
            script,
            args + [
                "--host",
                os.environ.get("CAT62_HOST", "").strip() or os.environ.get("RADAR_HOST", "").strip(),
                "--port",
                os.environ.get("CAT62_PORT", "").strip() or os.environ.get("RADAR_PORT", "").strip() or "50062",
            ],
        ))

    run_bundle("asterix", children)


def _pop_category_argument() -> int | None:
    for index, argument in enumerate(sys.argv[1:], 1):
        if argument == "--category":
            if index + 1 >= len(sys.argv):
                raise SystemExit("--category requires one of: 1, 2, 4, 7, 8, 9, 10, 11, 15, 16, 17, 18, 19, 20, 21, 23, 25, 32, 34, 48, 62, 63, 65, 150, 205, 240, 247")
            value = sys.argv[index + 1]
            del sys.argv[index:index + 2]
            break
        if argument.startswith("--category="):
            value = argument.split("=", 1)[1]
            del sys.argv[index]
            break
    else:
        return None
    try:
        category = int(value)
    except ValueError as exc:
        raise SystemExit("invalid ASTERIX category: {}".format(value)) from exc
    if category not in _CATEGORY_MAINS:
        raise SystemExit("unsupported ASTERIX category: {}".format(category))
    return category


_CATEGORY_MAINS = {
    1: _cat1_main,
    2: _cat2_main,
    4: _cat4_main,
    7: _cat7_main,
    8: _cat8_main,
    9: _cat9_main,
    10: _cat10_main,
    11: _cat11_main,
    15: _cat15_main,
    16: _cat16_main,
    17: _cat17_main,
    18: _cat18_main,
    19: _cat19_main,
    20: _cat20_main,
    21: _cat21_main,
    23: _cat23_main,
    25: _cat25_main,
    32: _cat32_main,
    34: _cat34_main,
    48: _cat48_main,
    62: _cat62_main,
    63: _cat63_main,
    65: _cat65_main,
    150: _cat150_main,
    205: _cat205_main,
    240: _cat240_main,
    247: _cat247_main,
}


def _raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    """Validate one complete ASTERIX frame and return its record payload."""
    if len(frame) < 3:
        raise ValueError("ASTERIX frame shorter than header")
    declared = int.from_bytes(frame[1:3], "big")
    if frame[0] != expected_category:
        raise ValueError("unexpected ASTERIX category {}".format(frame[0]))
    if declared != len(frame):
        raise ValueError("ASTERIX frame length mismatch")
    return frame[3:]


def main() -> None:
    category = _pop_category_argument()
    if category is None:
        _bundle_main()
    else:
        _CATEGORY_MAINS[category]()


if __name__ == "__main__":
    main()
