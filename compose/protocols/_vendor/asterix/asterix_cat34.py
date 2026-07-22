#!/usr/bin/env python3

"""EUROCONTROL ASTERIX CAT-034 Edition 1.29 radar service protocol."""



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
from protocols.asterix_cat34_pb2 import AsterixCat34Status
from protocols.protobuf_codec import publish_dual


ORG       = os.environ.get("PARTNER_NAMESPACE", "")

TOPIC_ROOT = topic_root()

HERE      = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)

_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

TOPIC_SENSOR = "{}/land/asterix/cat34/neutral/radar/status/v1".format(TOPIC_ROOT)
RAW_INPUT_TOPIC = "{}/raw/asterix/cat34".format(TOPIC_ROOT)

CAT_034 = 0x22

_MSG_TYPES_034 = {1: "north_marker", 2: "sector_crossing",
                  3: "geo_filter",   4: "jamming_strobe"}

_COUNT_LABELS = {
    0: "no_detection", 1: "psr",        2: "ssr",         3: "psr_ssr",
    4: "all",          5: "no_det_psr", 6: "no_det_ssr",  7: "mode5",
    11: "mil_id",
}

def _env_float(key: str) -> float:
    v = os.environ.get(key, "").strip()
    return float(v) if v else 0.0

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

def _s24(b: bytes) -> int:
    """Sign-extend a 3-byte big-endian integer."""
    raw = (b[0] << 16) | (b[1] << 8) | b[2]
    return raw - (1 << 24) if raw >= (1 << 23) else raw

def _decode_i034_050(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/050 System Configuration and Status — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    sf = data[pos]; pos += 1
    if sf & 0x80:
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        out["sys_nogo"]        = bool(com & 0x80)   # NOGO: system not operational
        out["sys_ovl_rdp"]     = bool(com & 0x40)   # OVL RDP: overload
        out["sys_ovl_xmt"]     = bool(com & 0x20)   # OVL XMT: comms overload
        if com & 0x10: out["sys_msc_connected"] = True  # MSC: monitoring connected
        out["sys_tsv_invalid"] = bool(com & 0x08)   # TSV: time source invalid
    for key, mask in (("psr_status", 0x20), ("ssr_status", 0x10), ("mds_status", 0x08)):
        if sf & mask:
            if pos >= len(data): return out, pos
            b = data[pos]; pos += 1
            an = (b & 0x60) >> 5
            out[key] = ("not_operational", "operational", "degraded", "test")[an]
            if b & 0x10: out[key.replace("status", "overload")] = True
    while sf & 0x01:
        if pos >= len(data): break
        sf = data[pos]; pos += 1
        for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
            if (sf & mask) and pos < len(data): pos += 1
    return out, pos

def _decode_i034_060(data: bytes, pos: int) -> tuple[dict, int]:
    """I034/060 System Processing Mode — compound."""
    out = {}
    if pos >= len(data):
        return out, pos
    sf = data[pos]; pos += 1
    if sf & 0x80:               # COM sub-field
        if pos >= len(data): return out, pos
        com = data[pos]; pos += 1
        red = (com & 0xE0) >> 5
        if red: out["reduction_level"] = red
    if sf & 0x20:               # PSR sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        out["psr_polarization"]    = "circular" if (b & 0x80) else "linear"
        if b & 0x40: out["psr_coverage_reduced"] = True  # REDRAD
        if b & 0x20: out["psr_stc_active"]       = True
    if sf & 0x10:               # SSR sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        chab = (b >> 6) & 0x03
        if chab: out["ssr_channel"] = ("", "A", "B", "A+B")[chab]
        if b & 0x20: out["ssr_overload"] = True
    if sf & 0x08:               # MDS sub-field
        if pos >= len(data): return out, pos
        b = data[pos]; pos += 1
        if b & 0x80: out["mds_overload"] = True
    while sf & 0x01:
        if pos >= len(data): break
        sf = data[pos]; pos += 1
        for mask in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
            if (sf & mask) and pos < len(data): pos += 1
    return out, pos

def decode_cat034(data: bytes) -> dict | None:
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
        elif frn == 2:                  # I034/030  Time of Day (1/128 s)
            if pos + 3 > len(data): break
            raw = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2]
            msg["tod_s"] = raw / 128.0; pos += 3
        elif frn == 3:                  # I034/020  Sector Number (360/256 °)
            if pos + 1 > len(data): break
            msg["sector_deg"] = round(data[pos] * 360.0 / 256.0, 2); pos += 1
        elif frn == 4:                  # I034/041  Antenna Rotation Period (1/128 s)
            if pos + 2 > len(data): break
            msg["rotation_s"] = round(_u16(data[pos:pos+2]) / 128.0, 2); pos += 2
        elif frn == 5:                  # I034/050  System Configuration (compound)
            extra, pos = _decode_i034_050(data, pos)
            msg.update(extra)
        elif frn == 6:                  # I034/060  System Processing Mode (compound)
            extra, pos = _decode_i034_060(data, pos)
            msg.update(extra)
        elif frn == 7:                  # I034/070  Message Count Values (REP × 2 bytes)
            if pos >= len(data): break
            rep = data[pos]; pos += 1
            counts = {}
            for _ in range(rep):
                if pos + 2 > len(data): break
                word = _u16(data[pos:pos+2]); pos += 2
                counts[_COUNT_LABELS.get((word >> 11) & 0x1F,
                                          "type{}".format((word >> 11) & 0x1F))] = word & 0x7FF
            if counts: msg["msg_counts"] = counts
        elif frn == 8:                  # I034/100  Generic Polar Window (8 bytes)
            if pos + 8 > len(data): break
            msg["coverage_rho_start_nm"] = round(_u16(data[pos:pos+2]) / 128.0, 2)
            msg["coverage_rho_end_nm"]   = round(_u16(data[pos+2:pos+4]) / 128.0, 2)
            msg["coverage_az_start_deg"] = round(_u16(data[pos+4:pos+6]) * 360.0 / 65536.0, 2)
            msg["coverage_az_end_deg"]   = round(_u16(data[pos+6:pos+8]) * 360.0 / 65536.0, 2)
            pos += 8
        elif frn == 9:                  # I034/110  Data Filter (1 byte)
            if pos + 1 > len(data): break
            _FILT034 = {0: "invalid", 1: "no_filter", 2: "psr", 3: "ssr",
                        4: "psr_ssr", 5: "all", 6: "no_det_psr", 7: "no_det_ssr"}
            msg["data_filter"] = _FILT034.get(data[pos], "type_{}".format(data[pos]))
            pos += 1
        elif frn == 10:                 # I034/120  3D-Position of Data Source (compound)
            if pos >= len(data): break
            sf = data[pos]; pos += 1
            if sf & 0x80:               # subfield 1: height MSL (int16, LSB = 1 m)
                if pos + 2 > len(data): break
                msg["site_alt_m"] = _s16(data[pos:pos+2]); pos += 2
            if sf & 0x40:               # subfield 2: WGS-84 lat+lon (int24 each, LSB = 180/2²³°)
                if pos + 6 > len(data): break
                msg["site_lat"] = round(_s24(data[pos:pos+3]) * (180.0 / 2**23), 7); pos += 3
                msg["site_lon"] = round(_s24(data[pos:pos+3]) * (180.0 / 2**23), 7); pos += 3
            while sf & 0x01:            # FX: skip unknown extension subfields
                if pos >= len(data): break
                sf = data[pos]; pos += 1
                for m in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02):
                    if (sf & m) and pos + 2 <= len(data): pos += 2
        elif frn == 11:                 # I034/090  Collimation Error (4 bytes)
            if pos + 4 > len(data): break
            msg["collimation_rng_nm"] = round(_s16(data[pos:pos+2]) / 128.0, 4);   pos += 2
            msg["collimation_az_deg"] = round(_s16(data[pos:pos+2]) * 360.0 / 65536.0, 4); pos += 2
        else:
            break
    return msg if msg else None

def _make_cat034_handler(pub_sensor, site, radar_name):
    # site = [lat, lon] — mutable so CAT-34 I034/120 can self-configure the radar position
    _first_seen:   dict[str, float] = {}
    _sweep:        dict[str, dict]  = {}   # key → {north_ts, rotation_s, status}
    _sweep_lock    = threading.Lock()
    _sweep_active: set[str]         = set()
    _keepalive:    dict[str, dict]  = {}   # key → last full status
    _keepalive_active: set[str]     = set()
    _ka_lock       = threading.Lock()
    _pos_hist:     dict[str, tuple] = {}   # key → (ts, lat, lon) for speed/course
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
                TOPIC_SENSOR,
                payload,
                AsterixCat34Status,
                zenoh,
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
                TOPIC_SENSOR,
                payload,
                AsterixCat34Status,
                zenoh,
                wrapper_field="sensor",
            )

    def _h(data: bytes, verbose: bool):
        msg = decode_cat034(data)
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

        # Self-configure: update radar site position from I034/120 when the radar transmits it
        if msg.get("site_lat") is not None:
            site[0] = msg["site_lat"]
            site[1] = msg["site_lon"]

        if not (pub_sensor and site[0] is not None and site[1] is not None):
            return

        sac = msg.get("sac", 0); sic = msg.get("sic", 0)
        key = "{}-{}".format(sac, sic)
        now = time.time()

        if mtype == "north_marker":
            _first_seen.setdefault(key, now)

            # Compute speed and course from successive position reports (mobile platform support)
            speed_ms = heading_deg = None
            prev = _pos_hist.get(key)
            if prev and site[0] is not None and site[1] is not None:
                dt = now - prev[0]
                if 0 < dt < 3600:
                    dlat = (site[0] - prev[1]) * 111320.0
                    dlon = (site[1] - prev[2]) * 111320.0 * math.cos(math.radians(site[0]))
                    dist_m = math.hypot(dlat, dlon)
                    speed_ms = round(dist_m / dt, 2)
                    if dist_m > 1.0:
                        heading_deg = round((math.degrees(math.atan2(dlon, dlat)) + 360) % 360, 1)
            _pos_hist[key] = (now, site[0], site[1])

            status = {
                "_src":        "ASTERIX CAT-34",
                "_ts":         now,
                "sensor_type": "radar",
                "sensor_id":   "CAT34-{}-{}".format(sac, sic),
                "sensor_name": radar_name or "RADAR SAC{}/SIC{}".format(sac, sic),
                "lat_deg":     site[0],
                "lon_deg":     site[1],
                "online_since": _first_seen[key],
            }
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
                TOPIC_SENSOR,
                status,
                AsterixCat34Status,
                zenoh,
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
    parser = argparse.ArgumentParser(description="ASTERIX CAT-034 Ed.1.29 -> Zenoh")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAT34_PORT", "50034") or 50034))
    parser.add_argument("--tcp", action="store_true", default=os.environ.get("CAT34_TCP") == "1")
    parser.add_argument("--zenoh-raw", action="store_true", default=os.environ.get("CAT34_ZENOH_RAW") == "1")
    parser.add_argument("--input-topic", default=os.environ.get("CAT34_INPUT_TOPIC", RAW_INPUT_TOPIC))
    parser.add_argument("--site-lat", type=float, default=_env_float("CAT34_RADAR_LAT"))
    parser.add_argument("--site-lon", type=float, default=_env_float("CAT34_RADAR_LON"))
    parser.add_argument("--site-name", default=os.environ.get("CAT34_RADAR_NAME", ""))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.zenoh_raw and not args.port: parser.error("--port or CAT34_PORT is required unless --zenoh-raw is selected")
    site = [args.site_lat or None, args.site_lon or None]
    session = zenoh.open(make_config()); publisher = session.declare_publisher(TOPIC_SENSOR)
    handler = _make_cat034_handler(publisher, site, args.site_name or None)
    try:
        if args.zenoh_raw: _run_zenoh_raw(session, args.input_topic, CAT_034, handler, args.verbose)
        else: _run_inbound(args.port, args.tcp, "CAT-34 Ed.1.29", {CAT_034: handler}, args.verbose)
    except KeyboardInterrupt: pass
    finally: publisher.undeclare(); session.close()



if __name__ == "__main__":

    main()
