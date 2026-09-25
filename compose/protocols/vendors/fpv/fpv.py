#!/usr/bin/env python3
"""FPV drone CRSF telemetry on the EFDI Backbone trial fabric -> EFDI tracks.

Topic "<slot>/fpv/telemetry/v1" (protobuf:TelemetryMessage) — one message at
a time via a oneof (gps/battery/link/attitude/flight_mode/raw). Only the gps
variant carries a position; every other variant is link/vehicle telemetry
with nothing to place on a map, so it's silently dropped here.
"""

from __future__ import annotations

import json
import time

from google.protobuf.message import DecodeError
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe
from schemas.vendors.fpv.crsf_telemetry_pb2 import TelemetryMessage

_TOPIC_SUFFIX = "/fpv/telemetry/v1"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

_KMH_TO_MPS = 1 / 3.6


def gps_record(payload: bytes) -> dict | None:
    msg = TelemetryMessage()
    msg.ParseFromString(payload)
    if msg.WhichOneof("payload") != "gps":
        return None
    gps = msg.gps
    if gps.latitude_deg == 0 and gps.longitude_deg == 0:
        return None
    record = {
        "_ts": time.time(),
        "_src": "backbone:fpv:{}".format(msg.source_id or "unknown"),
        "uid": msg.source_id or "fpv-drone",
        "lat_deg": gps.latitude_deg,
        "lon_deg": gps.longitude_deg,
        "alt_m": gps.altitude_m,
        "label": msg.source_id or "FPV drone",
        "affiliation": "unknown",
    }
    if gps.groundspeed_kmh:
        record["speed_mps"] = gps.groundspeed_kmh * _KMH_TO_MPS
    if gps.heading_deg:
        record["heading_deg"] = gps.heading_deg
    if gps.satellites:
        record["gps_satellites"] = gps.satellites
    return record


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("fpv: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        if not key.endswith(_TOPIC_SUFFIX):
            return
        try:
            record = gps_record(payload_bytes(sample))
            if record:
                topic = "{}/air/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, record.pop("affiliation"))
                session.put(topic, json.dumps(record).encode())
        except DecodeError as exc:
            print("fpv decode error on {}: {}".format(key, exc), flush=True)
        except Exception as exc:
            print("fpv error on {}: {}".format(key, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("fpv: {} -> tracks (gps variant only)".format(INPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    run()
