#!/usr/bin/env python3
"""Shared helpers for the split STANAG 4609 SRT and KLV bridges."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

import zenoh

from namespace_prefix import topic_root
from zenoh_auth import apply_zenoh_auth

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
SOURCE = os.environ.get("STANAG4609_SOURCE", "stanag4609").strip() or "stanag4609"
READ_CHUNK = int(os.environ.get("STANAG4609_READ_CHUNK", "65536"))
RAW_TOPIC = "{}/raw/stanag4609/klv".format(TOPIC_ROOT)
TRACK_TOPIC = "{}/air/stanag4609/unknown/uav/tracks/v1".format(TOPIC_ROOT)
ST0601_LOCAL_SET_KEY = bytes.fromhex("060e2b34020b01010e01030101000000")
KLV_PREFIX = b"\x06\x0E"


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([ENDPOINT]))
    apply_zenoh_auth(conf)
    if ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


def decode_ber(data: bytes) -> tuple[int, int]:
    if not data:
        raise ValueError("missing BER length")
    first = data[0]
    if first < 0x80:
        return first, 1
    count = first & 0x7F
    if count == 0:
        raise ValueError("indefinite BER lengths are not supported")
    if len(data) < 1 + count:
        raise ValueError("short BER length")
    return int.from_bytes(data[1:1 + count], "big"), 1 + count


def decode_unsigned(raw: bytes, minimum: float, maximum: float) -> float:
    value = int.from_bytes(raw, "big", signed=False)
    scale = (1 << (8 * len(raw))) - 1
    return minimum + (value / scale) * (maximum - minimum) if scale else minimum


def decode_signed(raw: bytes, minimum: float, maximum: float) -> float:
    value = int.from_bytes(raw, "big", signed=True)
    min_raw = -(1 << (8 * len(raw) - 1))
    max_raw = (1 << (8 * len(raw) - 1)) - 1
    if max_raw == min_raw:
        return minimum
    return minimum + ((value - min_raw) / (max_raw - min_raw)) * (maximum - minimum)


def looks_like_klv_key(key: bytes) -> bool:
    return len(key) == 16 and key.startswith(KLV_PREFIX)


def split_klv_packet(packet: bytes) -> tuple[bytes, bytes] | None:
    if len(packet) < 17:
        return None
    key = packet[:16]
    if not looks_like_klv_key(key):
        return None
    value_len, len_size = decode_ber(packet[16:])
    total_len = 16 + len_size + value_len
    if len(packet) < total_len:
        return None
    return key, packet[16 + len_size:total_len]


def parse_klv_packets(stream):
    buf = bytearray()
    while True:
        chunk = stream.read(READ_CHUNK)
        if not chunk:
            return
        buf.extend(chunk)
        while True:
            if len(buf) < 17:
                break
            if not looks_like_klv_key(bytes(buf[:16])):
                idx = bytes(buf).find(KLV_PREFIX, 1)
                if idx < 0:
                    del buf[:-1]
                    break
                del buf[:idx]
                continue
            try:
                value_len, len_size = decode_ber(bytes(buf[16:]))
            except ValueError:
                if buf:
                    del buf[0]
                break
            total_len = 16 + len_size + value_len
            if len(buf) < total_len:
                break
            key = bytes(buf[:16])
            value = bytes(buf[16 + len_size:total_len])
            del buf[:total_len]
            yield key, value


def decode_st0601(tags: dict[int, bytes]) -> dict[str, object]:
    out: dict[str, object] = {}

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
        out["platform_heading_deg"] = round(decode_unsigned(heading, 0.0, 360.0), 3)

    lat = tags.get(13)
    if lat and len(lat) == 4:
        out["sensor_lat_deg"] = round(decode_signed(lat, -90.0, 90.0), 6)

    lon = tags.get(14)
    if lon and len(lon) == 4:
        out["sensor_lon_deg"] = round(decode_signed(lon, -180.0, 180.0), 6)

    rel_az = tags.get(18)
    if rel_az and len(rel_az) == 2:
        out["sensor_relative_azimuth_deg"] = round(decode_signed(rel_az, -180.0, 180.0), 3)

    rel_el = tags.get(19)
    if rel_el and len(rel_el) == 2:
        out["sensor_relative_elevation_deg"] = round(decode_signed(rel_el, -90.0, 90.0), 3)

    frame_lat = tags.get(23)
    if frame_lat and len(frame_lat) == 4:
        out["frame_center_lat_deg"] = round(decode_signed(frame_lat, -90.0, 90.0), 6)

    frame_lon = tags.get(24)
    if frame_lon and len(frame_lon) == 4:
        out["frame_center_lon_deg"] = round(decode_signed(frame_lon, -180.0, 180.0), 6)

    raw_tags: dict[str, str] = {}
    for tag, raw in tags.items():
        if tag in {2, 5, 13, 14, 18, 19, 23, 24}:
            continue
        raw_tags[str(tag)] = raw.hex()
    if raw_tags:
        out["raw_tags_hex"] = raw_tags

    return out


def parse_local_set(value: bytes) -> dict[int, bytes]:
    tags: dict[int, bytes] = {}
    pos = 0
    while pos < len(value):
        tag = value[pos]
        pos += 1
        tag_len, len_size = decode_ber(value[pos:])
        pos += len_size
        if pos + tag_len > len(value):
            break
        tags[tag] = value[pos:pos + tag_len]
        pos += tag_len
    return tags


def pack_track_payload(payload: dict[str, object], raw_packet: bytes) -> dict[str, object]:
    proto_payload = dict(payload)
    proto_payload["klv_raw"] = raw_packet
    return proto_payload
