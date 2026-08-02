#!/usr/bin/env python3
"""Generic MQTT sensor JSON on Zenoh -> normalized sensor records.

bridges/mqtt_bridge.py lands raw broker payloads under ``.../raw/mqtt/**``.
MQTT carries no schema of its own, so this translator accepts a deliberately
small, documented contract and ignores anything that does not match:

  required : an identifier  (id | uid | sensor_id | device_id | name)
             a latitude     (lat | latitude | lat_deg)
             a longitude    (lon | lng | longitude | lon_deg)
  optional : time (ts | time | timestamp, epoch seconds or ISO-8601)
             alt | altitude | alt_m, speed | speed_ms, heading | course,
             type | sensor_type, label | name

A payload missing a position is not a map object and is skipped — its exact
bytes remain on the raw topic for a vendor-specific normalizer to handle.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from gateway import TOPIC_ROOT, open_session, subscribe, payload_bytes
from protocols.proto.mqtt_json_pb2 import MqttSensorRecord

from protocols.track_views import publish_dual

INPUT_TOPIC = os.environ.get("MQTT_INPUT_TOPIC") or TOPIC_ROOT + "/raw/mqtt/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/mqtt/iot/unknown/sensor"

_ID_KEYS = ("id", "uid", "sensor_id", "device_id", "serial", "name")
_LAT_KEYS = ("lat", "latitude", "lat_deg")
_LON_KEYS = ("lon", "lng", "longitude", "lon_deg")
_TIME_KEYS = ("ts", "time", "timestamp", "observed_at")


def _first(payload: dict, keys) -> object:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _epoch(value, now: float) -> float:
    number = _number(value)
    if number is not None:
        # Milliseconds are as common as seconds on IoT brokers.
        return number / 1000.0 if number > 1e11 else number
    if isinstance(value, str):
        try:
            text = value.strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return now
    return now


def normalize(payload: dict, mqtt_topic: str = "", now: float | None = None) -> dict | None:
    """Map one vendor payload onto the fabric contract, or None if it cannot be."""
    if not isinstance(payload, dict):
        return None
    lat = _number(_first(payload, _LAT_KEYS))
    lon = _number(_first(payload, _LON_KEYS))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    raw_id = _first(payload, _ID_KEYS)
    if raw_id is None:
        return None
    now = time.time() if now is None else float(now)

    record = {
        "_ts": _epoch(_first(payload, _TIME_KEYS), now),
        "_src": "mqtt",
        "uid": "MQTT-" + "".join(
            ch if ch.isalnum() or ch in "._:-" else "_" for ch in str(raw_id))[:120],
        "lat_deg": round(lat, 7),
        "lon_deg": round(lon, 7),
        "source_kind": "mqtt_sensor",
    }
    if mqtt_topic:
        record["mqtt_topic"] = mqtt_topic

    for target, keys in (("geo_alt_m", ("alt", "altitude", "alt_m", "geo_alt_m")),
                         ("speed_ms", ("speed", "speed_ms")),
                         ("heading_deg", ("heading", "course", "heading_deg"))):
        value = _number(_first(payload, keys))
        if value is not None:
            record[target] = value

    for target, keys in (("sensor_type", ("type", "sensor_type")),
                         ("label", ("label", "name"))):
        value = _first(payload, keys)
        if isinstance(value, (str, int, float)):
            record[target] = str(value)[:120]

    extras = {str(k): v for k, v in list(payload.items())[:64]
              if isinstance(v, (str, int, float, bool))}
    record["properties_json"] = json.dumps(extras, separators=(",", ":"))
    return record


def run() -> None:
    session = open_session()
    prefix = TOPIC_ROOT + "/raw/mqtt/"

    def on_sample(sample) -> None:
        # A broker carries whatever a vendor publishes: Sparkplug B protobuf,
        # CBOR, plain text. Anything this translator cannot read as JSON simply
        # is not addressed to it — skip it silently instead of logging once per
        # message, and leave the bytes on the raw topic for another normalizer.
        try:
            payload = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        try:
            key = str(sample.key_expr)
            mqtt_topic = key[len(prefix):] if key.startswith(prefix) else ""
            record = normalize(payload, mqtt_topic)
            if record:
                publish_dual(session, OUTPUT_TOPIC, record, MqttSensorRecord)
        except Exception as exc:
            print("MQTT decode error:", exc, flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("MQTT translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="MQTT sensor JSON on Zenoh -> sensor records").parse_args()
    run()
