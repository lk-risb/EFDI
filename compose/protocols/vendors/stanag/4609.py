#!/usr/bin/env python3
"""STANAG 4609 SRT/KLV ingest → Zenoh.

Reads an SRT transport carrying MPEG-TS with KLV metadata, extracts the KLV
packet stream with ffmpeg, and publishes best-effort metadata snapshots to the
EFDI Zenoh fabric.

The bridge is intentionally conservative:
- it publishes the raw KLV packet bytes for downstream consumers that want the
  exact MISB payload
- it decodes a small, safe subset of common MISB ST 0601 fields when present
- it never attempts to transcode the video essence itself

Config (compose/.env):
  STANAG4609_SRT_URL=srt://host:port?mode=listener  # required
  STANAG4609_SOURCE=optional-stream-name            # optional display/source tag

Run:
  venv/bin/python3 protocols/vendors/stanag/4609.py
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from importlib import import_module
import json
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import zenoh

from namespace_prefix import topic_root
from protocols.protobuf_codec import dual_topic, native_topic, publish_native, wrapped_track_message
from zenoh_auth import apply_zenoh_auth

Stanag4609Track = import_module("protocols.vendors.stanag.4609_pb2").Stanag4609Track

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
_SRT_URL = os.environ.get("STANAG4609_SRT_URL", "").strip()
SOURCE = os.environ.get("STANAG4609_SOURCE", "stanag4609").strip() or "stanag4609"
_FFMPEG_BIN = os.environ.get("STANAG4609_FFMPEG_BIN", "ffmpeg")
_RECONNECT_S = float(os.environ.get("STANAG4609_RECONNECT_S", "10"))
READ_CHUNK = int(os.environ.get("STANAG4609_READ_CHUNK", "65536"))
MAX_KLV_BYTES = int(os.environ.get("STANAG4609_MAX_KLV_BYTES", "1048576"))
_STREAM_ID = hashlib.sha1(_SRT_URL.encode("utf-8")).hexdigest()[:10] if _SRT_URL else "stream"

KLV_PREFIX = b"\x06\x0E\x2B\x34"
ST0601_LOCAL_SET_KEY = bytes.fromhex("060e2b34020b01010e01030101000000")
RAW_TOPIC = "{}/raw/stanag4609/klv".format(TOPIC_ROOT)
TRACK_TOPIC = "{}/air/stanag4609/camera/unknown/uav".format(TOPIC_ROOT)

# Backward-compatible internal aliases used by the existing implementation.
_SOURCE = SOURCE
_READ_CHUNK = READ_CHUNK
_KLV_PREFIX = KLV_PREFIX
_ST0601_LOCAL_SET_KEY = ST0601_LOCAL_SET_KEY
_RAW_TOPIC = RAW_TOPIC
_TRACK_TOPIC = TRACK_TOPIC


def _safe_stream_label(url: str) -> str:
    """Return a useful endpoint label without credentials or SRT options."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "configured-host"
        port = ":{}".format(parsed.port) if parsed.port is not None else ""
        return "{}://{}{}".format(parsed.scheme or "srt", host, port)
    except ValueError:
        return "srt://configured-host"


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


def _decode_ber(data: bytes) -> tuple[int, int]:
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


def _decode_unsigned(raw: bytes, minimum: float, maximum: float) -> float:
    value = int.from_bytes(raw, "big", signed=False)
    scale = (1 << (8 * len(raw))) - 1
    return minimum + (value / scale) * (maximum - minimum) if scale else minimum


def _decode_signed(raw: bytes, minimum: float, maximum: float) -> float:
    value = int.from_bytes(raw, "big", signed=True)
    min_raw = -(1 << (8 * len(raw) - 1))
    max_raw = (1 << (8 * len(raw) - 1)) - 1
    if value == min_raw:
        raise ValueError("MISB reserved signed integer error value")
    return minimum + ((value + max_raw) / (2 * max_raw)) * (maximum - minimum)


def _looks_like_klv_key(key: bytes) -> bool:
    return len(key) == 16 and key.startswith(_KLV_PREFIX)


def _encode_ber(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative BER length")
    if value < 0x80:
        return bytes((value,))
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(raw),)) + raw


def _decode_ber_oid(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(10):
        if pos >= len(data):
            raise ValueError("truncated BER-OID tag")
        byte = data[pos]; pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise ValueError("BER-OID tag exceeds 64 bits")


def decode_ber(data: bytes) -> tuple[int, int]:
    return _decode_ber(data)


def decode_unsigned(raw: bytes, minimum: float, maximum: float) -> float:
    return _decode_unsigned(raw, minimum, maximum)


def decode_signed(raw: bytes, minimum: float, maximum: float) -> float:
    return _decode_signed(raw, minimum, maximum)


def looks_like_klv_key(key: bytes) -> bool:
    return _looks_like_klv_key(key)


def _parse_local_set(value: bytes) -> dict[int, bytes]:
    tags: dict[int, bytes] = {}
    pos = 0
    while pos < len(value):
        tag, pos = _decode_ber_oid(value, pos)
        tag_len, len_size = _decode_ber(value[pos:])
        pos += len_size
        if pos + tag_len > len(value):
            raise ValueError("truncated MISB local-set value")
        tags[tag] = value[pos:pos + tag_len]
        pos += tag_len
    return tags


def _decode_st0601(tags: dict[int, bytes]) -> dict[str, object]:
    out: dict[str, object] = {}

    def mapped_signed(raw: bytes | None, size: int, minimum: float, maximum: float):
        if raw is None or len(raw) != size:
            return None
        try:
            return _decode_signed(raw, minimum, maximum)
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
        out["platform_heading_deg"] = round(_decode_unsigned(heading, 0.0, 360.0), 3)

    altitude = tags.get(15)
    if altitude and len(altitude) == 2:
        out["sensor_alt_m"] = round(_decode_unsigned(altitude, -900.0, 19_000.0), 2)

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
        out["sensor_relative_azimuth_deg"] = round(_decode_unsigned(rel_az, 0.0, 360.0), 3)

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
        out["frame_center_alt_m"] = round(_decode_unsigned(frame_altitude, -900.0, 19_000.0), 2)

    raw_tags: dict[str, str] = {}
    for tag, raw in tags.items():
        if tag in {2, 5, 13, 14, 15, 18, 19, 23, 24, 25}:
            continue
        raw_tags[str(tag)] = raw.hex()
    if raw_tags:
        out["raw_tags_hex"] = raw_tags

    return out


def parse_local_set(value: bytes) -> dict[int, bytes]:
    return _parse_local_set(value)


def decode_st0601(tags: dict[int, bytes]) -> dict[str, object]:
    return _decode_st0601(tags)


def _parse_klv_packets(stream):
    buf = bytearray()
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            return
        buf.extend(chunk)
        while True:
            if len(buf) < 17:
                break
            if not _looks_like_klv_key(bytes(buf[:16])):
                idx = bytes(buf).find(_KLV_PREFIX, 1)
                if idx < 0:
                    # Preserve a possible prefix split across read() chunks.
                    keep = min(len(buf), len(_KLV_PREFIX) - 1)
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
                value_len, len_size = _decode_ber(bytes(buf[16:]))
            except ValueError:
                if buf:
                    del buf[0]
                break
            if value_len > MAX_KLV_BYTES:
                del buf[0]
                continue
            total_len = 16 + len_size + value_len
            if len(buf) < total_len:
                break
            key = bytes(buf[:16])
            value = bytes(buf[16 + len_size:total_len])
            del buf[:total_len]
            yield key, value


def parse_klv_packets(stream):
    yield from _parse_klv_packets(stream)


def split_klv_packet(packet: bytes) -> tuple[bytes, bytes] | None:
    if len(packet) < 17:
        return None
    key = packet[:16]
    if not _looks_like_klv_key(key):
        return None
    value_len, len_size = _decode_ber(packet[16:])
    if value_len > MAX_KLV_BYTES:
        return None
    total_len = 16 + len_size + value_len
    if len(packet) != total_len:
        return None
    return key, packet[16 + len_size:total_len]


def pack_track_payload(payload: dict[str, object], raw_packet: bytes) -> dict[str, object]:
    proto_payload = dict(payload)
    proto_payload["klv_raw"] = raw_packet
    return proto_payload


def _ffmpeg_proc() -> subprocess.Popen[bytes]:
    if not _SRT_URL:
        raise SystemExit("Set STANAG4609_SRT_URL in .env")
    cmd = [
        _FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-i", _SRT_URL,
        "-map", "0:d:0?",
        "-c", "copy",
        "-f", "data",
        "pipe:1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _stderr_pump(proc: subprocess.Popen[bytes]) -> None:
    if proc.stderr is None:
        return
    for raw in iter(proc.stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            print("STANAG4609 ffmpeg: {}".format(line), flush=True)


def publish_packet(session: "zenoh.Session", packet_index: int, key: bytes, value: bytes, verbose: bool) -> None:
    raw_packet = key + _encode_ber(len(value)) + value
    payload = {
        "_ts": time.time(),
        "_src": _SOURCE,
        "stream_id": _STREAM_ID,
        "source_tag": _SOURCE,
        "packet_index": packet_index,
        "klv_key": key.hex(),
        "klv_len": len(value),
        "klv_raw_b64": base64.b64encode(raw_packet).decode("ascii"),
    }

    if _looks_like_klv_key(key) and key == _ST0601_LOCAL_SET_KEY:
        tags = _parse_local_set(value)
        decoded = _decode_st0601(tags)
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
    topic = _TRACK_TOPIC if "lat_deg" in payload and "lon_deg" in payload else _RAW_TOPIC
    session.put(topic, json.dumps(payload).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    if topic == _TRACK_TOPIC:
        proto_payload = dict(payload)
        proto_payload["klv_raw"] = raw_packet
        message = wrapped_track_message(Stanag4609Track, proto_payload)
        session.put(
            dual_topic(topic),
            message.SerializeToString(),
            encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
        )
    else:
        # KLV that carried no usable position still reaches protobuf consumers:
        # the original packet rides a RawEnvelope on the /native sibling, so no
        # sample is available as JSON only.
        publish_native(session, native_topic(topic), raw_packet, "stanag4609",
                       zenoh, profile="misb-st0601")
    if verbose:
        print("STANAG4609 PUB {} {} key={} len={}".format(
            topic.split("/")[-4],
            topic,
            payload["klv_key"][:12],
            payload["klv_len"],
        ), flush=True)


def run(args):
    while True:
        try:
            session = zenoh.open(make_config())
            break
        except Exception as exc:
            print("STANAG4609 Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    print("STANAG 4609 video metadata bridge started", flush=True)
    print("  SRT    : {}".format(_safe_stream_label(_SRT_URL)), flush=True)
    print("  Stream : {}".format(_STREAM_ID), flush=True)
    print("  Topic  : {}".format(_TRACK_TOPIC), flush=True)

    while True:
        proc = None
        stderr_thread = None
        try:
            proc = _ffmpeg_proc()
            assert proc.stdout is not None
            stderr_thread = threading.Thread(target=_stderr_pump, args=(proc,), daemon=True)
            stderr_thread.start()
            print("STANAG4609 ffmpeg connected", flush=True)
            packet_index = 0
            for key, value in _parse_klv_packets(proc.stdout):
                publish_packet(session, packet_index, key, value, args.verbose)
                packet_index += 1
            rc = proc.wait(timeout=5)
            raise RuntimeError("ffmpeg exited with code {}".format(rc))
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("STANAG4609 error: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass

    session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="STANAG 4609 SRT/KLV → Zenoh")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
