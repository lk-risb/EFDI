#!/usr/bin/env python3
"""STANAG 4609 raw KLV / MISB decode → Zenoh track records."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time

import zenoh

from protocols.protobuf_codec import dual_topic, wrapped_track_message
from protocols._vendor.stanag.stanag4609_common import (
    RAW_TOPIC,
    SOURCE,
    ST0601_LOCAL_SET_KEY,
    TRACK_TOPIC,
    decode_st0601,
    make_config,
    pack_track_payload,
    parse_local_set,
    split_klv_packet,
)
from protocols.stanag4609_pb2 import Stanag4609Track

_RECONNECT_S = float(os.environ.get("STANAG4609_RECONNECT_S", "10"))
_STREAM_ID = hashlib.sha1(SOURCE.encode("utf-8")).hexdigest()[:10]


def publish_packet(session: "zenoh.Session", packet_index: int, packet: bytes, verbose: bool) -> None:
    split = split_klv_packet(packet)
    if split is None:
        return
    key, value = split
    payload = {
        "_ts": time.time(),
        "_src": SOURCE,
        "stream_id": _STREAM_ID,
        "packet_index": packet_index,
        "klv_key": key.hex(),
        "klv_len": len(value),
        "klv_raw_b64": base64.b64encode(packet).decode("ascii"),
    }
    if key == ST0601_LOCAL_SET_KEY:
        tags = parse_local_set(value)
        decoded = decode_st0601(tags)
        payload.update(decoded)
        lat = decoded.get("sensor_lat_deg") or decoded.get("frame_center_lat_deg")
        lon = decoded.get("sensor_lon_deg") or decoded.get("frame_center_lon_deg")
        if lat is not None and lon is not None:
            payload["lat_deg"] = lat
            payload["lon_deg"] = lon
        if "platform_heading_deg" in decoded:
            payload["heading_deg"] = decoded["platform_heading_deg"]

    if "lat_deg" not in payload or "lon_deg" not in payload:
        return
    session.put(TRACK_TOPIC, json.dumps(payload).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    message = wrapped_track_message(Stanag4609Track, pack_track_payload(payload, packet))
    session.put(dual_topic(TRACK_TOPIC), message.SerializeToString(), encoding=zenoh.Encoding.APPLICATION_PROTOBUF)
    if verbose:
        print("STANAG4609 KLV PUB {} key={} len={}".format(
            TRACK_TOPIC, payload["klv_key"][:12], payload["klv_len"]
        ), flush=True)


def run(args):
    while True:
        try:
            session = zenoh.open(make_config())
            break
        except Exception as exc:
            print("STANAG4609 KLV Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    print("STANAG 4609 KLV bridge started", flush=True)
    print("  Input  : {}".format(RAW_TOPIC), flush=True)
    print("  Output : {}".format(TRACK_TOPIC), flush=True)

    def on_sample(sample) -> None:
        try:
            packet = bytes(sample.payload)
            publish_packet(session, 0, packet, args.verbose)
        except Exception as exc:
            print("STANAG4609 KLV decode error: {}".format(exc), flush=True)

    subscriber = session.declare_subscriber(RAW_TOPIC, on_sample)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="STANAG 4609 raw KLV → Zenoh tracks")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
