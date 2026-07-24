#!/usr/bin/env python3
"""STANAG 4609 KLV decoder — Zenoh raw → canonical tracks.

Subscribes to the raw MISB KLV packets that bridges/4609_bridge.py ingests from
the SRT/MPEG-TS transport, decodes a small, safe subset of common MISB ST 0601
fields, and publishes positioned frames as canonical tracks (SAPIENT / JSON /
protobuf views). SRT/ffmpeg ingest is the bridge's job; this protocol never
touches the transport and never transcodes the video essence itself.

The decoder is intentionally conservative:
- a positioned frame (sensor or frame-centre lat/lon present) becomes a track;
  its exact KLV bytes ride the /raw sibling of the object key
- non-positioned KLV carries no canonical track: its bytes already live on the
  fabric via the ingress bridge, so the decoder stays silent

Config (compose/.env):
  STANAG4609_SOURCE=optional-stream-name  # ingress source tag (contact identity)

Run:
  venv/bin/python3 protocols/vendors/stanag/4609.py --zenoh-raw
"""

from __future__ import annotations

import argparse
import base64
from importlib import import_module
import json
import os
import queue
import time
from datetime import datetime, timezone

import zenoh

from namespace_prefix import topic_root
from protocols.protobuf_codec import native_topic, publish_native, publish_dual, semantic_topic
from zenoh_auth import apply_zenoh_auth

Stanag4609Track = import_module("protocols.vendors.stanag.4609_pb2").Stanag4609Track

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
SOURCE = os.environ.get("STANAG4609_SOURCE", "stanag_4609").strip() or "stanag_4609"
_RECONNECT_S = float(os.environ.get("STANAG4609_RECONNECT_S", "10"))
READ_CHUNK = int(os.environ.get("STANAG4609_READ_CHUNK", "65536"))
MAX_KLV_BYTES = int(os.environ.get("STANAG4609_MAX_KLV_BYTES", "1048576"))

KLV_PREFIX = b"\x06\x0E\x2B\x34"
ST0601_LOCAL_SET_KEY = bytes.fromhex("060e2b34020b01010e01030101000000")
TRACK_TOPIC = "{}/air/stanag_4609/camera/unknown/uav".format(TOPIC_ROOT)

# Backward-compatible internal aliases used by the existing implementation.
_READ_CHUNK = READ_CHUNK
_KLV_PREFIX = KLV_PREFIX
_ST0601_LOCAL_SET_KEY = ST0601_LOCAL_SET_KEY
_TRACK_TOPIC = TRACK_TOPIC


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


def publish_packet(session, packet_index, key, value, verbose, stream_id, source):
    """Decode one raw KLV packet; publish a track only if it carries a position.

    Non-positioned KLV produces no canonical output — its exact bytes already
    live on the fabric (the ingress bridge put them there), so re-publishing here
    would only echo into this decoder's own raw subscription.
    """
    raw_packet = key + _encode_ber(len(value)) + value
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

    if "lat_deg" not in payload or "lon_deg" not in payload:
        return

    # A positioned frame is a track, so it leaves on the object key in every
    # view — SAPIENT, JSON and the per-protocol protobuf — exactly like the
    # other decoders. The stream is the object: its identity is the ingress
    # source tag, so successive frames update one contact instead of spawning
    # a new key each frame.
    payload["uid"] = stream_id
    obj_key = semantic_topic(_TRACK_TOPIC, payload)
    publish_dual(session, _TRACK_TOPIC, payload, Stanag4609Track, zenoh,
                 wrapper_field="track")
    # The exact KLV packet rides the /raw sibling of that same object key —
    # it is not embedded in the protobuf, the RawEnvelope is its home.
    publish_native(session, native_topic(obj_key), raw_packet, "stanag_4609",
                   zenoh, profile="misb-st0601")
    if verbose:
        print("STANAG4609 TRACK {} uid={} key={} len={}".format(
            _TRACK_TOPIC, stream_id, payload["klv_key"][:12], payload["klv_len"]), flush=True)


def run(args):
    while True:
        try:
            session = zenoh.open(make_config())
            break
        except Exception as exc:
            print("STANAG4609 Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    topic = args.raw_topic or "{}/raw/stanag_4609/**".format(TOPIC_ROOT)
    counter = {"i": 0}

    def on_sample(sample):
        data = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
        parsed = split_klv_packet(data)
        if parsed is None:
            return
        key, value = parsed
        # Contact identity is the ingress source segment of the raw topic
        # (…/raw/stanag_4609/<source>); fall back to the configured SOURCE.
        source = str(sample.key_expr).rstrip("/").rsplit("/", 1)[-1] or SOURCE
        publish_packet(session, counter["i"], key, value, args.verbose, source, source)
        counter["i"] += 1

    subscriber = session.declare_subscriber(topic, on_sample)
    print("STANAG 4609 KLV decoder started", flush=True)
    print("  Raw   : {}".format(topic), flush=True)
    print("  Track : {}".format(_TRACK_TOPIC), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="STANAG 4609 KLV decoder — Zenoh raw → tracks")
    parser.add_argument("--zenoh-raw", action="store_true",
                        help="decode KLV from …/raw/stanag_4609/** (default and only mode)")
    parser.add_argument("--raw-topic", default=os.environ.get("STANAG4609_RAW_TOPIC", ""),
                        help="override the raw KLV subscription key expression")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
