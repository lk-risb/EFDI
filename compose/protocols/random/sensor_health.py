#!/usr/bin/env python3
"""Common sensor-health JSON translator on Zenoh."""

from __future__ import annotations

import argparse
import os
import time

import zenoh

from protocols.protobuf_codec import publish_dual
from protocols.random.sensor_health_pb2 import SensorHealth
from translation_common import TOPIC_ROOT, make_config, payload_json


INPUT_TOPIC = os.environ.get("SENSOR_HEALTH_INPUT_TOPIC") or TOPIC_ROOT + "/raw/health/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/health/unknown/neutral/station"


def normalize(value: dict, now: float | None = None) -> dict | None:
    if not isinstance(value, dict):
        return None
    payload = value
    sensor_id = payload.get("sensor_id") or payload.get("sensor") or payload.get("node_id") or payload.get("id")
    if sensor_id is None:
        return None
    now = time.time() if now is None else float(now)
    status = str(value.get("status") or value.get("state") or "unknown").lower()[:64]
    record = {"_ts": now, "_src": "sensor_health", "uid": "HEALTH-{}".format(str(sensor_id)[:120]),
              "sensor_id": str(sensor_id)[:160], "health_status": status,
              "status": status,
              "source_kind": "sensor_health"}
    for output, keys in {
        "lat_deg": ("lat_deg", "latitude", "lat"),
        "lon_deg": ("lon_deg", "longitude", "lon"),
        "alt_m": ("alt_m", "altitude_m", "altitude"),
        "last_detection_ts": ("last_detection_ts", "last_detection_timestamp"),
    }.items():
        for key in keys:
            item = payload.get(key)
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                record[output] = float(item)
                break
    for output, keys in {"model": ("model", "model_name"), "firmware": ("firmware", "version"),
                         "health_detail": ("detail", "message", "reason")}.items():
        for key in keys:
            item = payload.get(key)
            if isinstance(item, (str, int, float)):
                record[output] = str(item)[:512]
                break
    if "last_detection_ts" in record:
        record["last_detection_timestamp"] = record["last_detection_ts"]
    if "health_detail" in record:
        record["detail"] = record["health_detail"]
    return record


def run() -> None:
    session = zenoh.open(make_config())

    def on_sample(sample) -> None:
        try:
            value = payload_json(sample)
            values = value if isinstance(value, list) else [value]
            for item in values:
                record = normalize(item)
                if record:
                    publish_dual(session, OUTPUT_TOPIC, record, SensorHealth, zenoh)
        except Exception as exc:
            print("sensor health decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("Sensor-health translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="Sensor health JSON on Zenoh -> EFDI").parse_args()
    run()
