#!/usr/bin/env python3
"""OGC SensorThings API Observations on Zenoh -> normalized sensor records.

bridges/sensorthings_bridge.py polls a SensorThings v1.1 service and lands each
Observation under ``.../raw/sensorthings/**``. This translator resolves the
position and publishes a marker the CoT and NVG layers already understand.

SensorThings splits an observation's position across two places. The preferred
source is the Observation's own ``FeatureOfInterest`` (where the measurement was
taken); the fallback is the parent ``Thing``'s ``Locations`` (where the sensor
lives). An observation with neither is a number without a place on the map, so
it is skipped rather than guessed at.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

import zenoh

from protocols.protobuf_codec import publish_dual
from protocols.random.sensorthings_pb2 import SensorThingsObservation
from translation_common import TOPIC_ROOT, make_config, payload_json

INPUT_TOPIC = os.environ.get("SENSORTHINGS_INPUT_TOPIC") or TOPIC_ROOT + "/raw/sensorthings/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/sensorthings/iot/neutral/sensor"


def _point(geometry) -> tuple[float, float] | None:
    """Return (lat, lon) from a GeoJSON geometry, Point or first coordinate."""
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")

    def walk(value):
        if isinstance(value, (list, tuple)) and len(value) >= 2 \
                and all(isinstance(item, (int, float)) for item in value[:2]):
            lon, lat = float(value[0]), float(value[1])
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                return lat, lon
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(coordinates)


def _location(observation: dict) -> tuple[float, float] | None:
    feature = observation.get("FeatureOfInterest")
    if isinstance(feature, dict):
        found = _point(feature.get("feature") or feature.get("location"))
        if found:
            return found
    datastream = observation.get("Datastream")
    if isinstance(datastream, dict):
        thing = datastream.get("Thing")
        if isinstance(thing, dict):
            for entry in thing.get("Locations") or []:
                if isinstance(entry, dict):
                    found = _point(entry.get("location"))
                    if found:
                        return found
    return None


def _epoch(stamp: str, now: float) -> float:
    if not isinstance(stamp, str) or not stamp:
        return now
    # phenomenonTime may be an instant or an interval "start/end".
    text = stamp.split("/")[-1].replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return now
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def normalize(observation: dict, now: float | None = None) -> dict | None:
    if not isinstance(observation, dict):
        return None
    position = _location(observation)
    if not position:
        return None
    now = time.time() if now is None else float(now)
    lat, lon = position

    datastream = observation.get("Datastream")
    datastream = datastream if isinstance(datastream, dict) else {}
    raw_id = observation.get("@iot.id") or observation.get("iot.id") or observation.get("id")
    if raw_id is None:
        raw_id = "{}-{}".format(datastream.get("@iot.id", "ds"), observation.get("phenomenonTime", now))

    record = {
        "_ts": _epoch(observation.get("phenomenonTime", ""), now),
        "_src": "sensorthings",
        "uid": "STA-" + "".join(
            ch if ch.isalnum() or ch in "._:-" else "_" for ch in str(raw_id))[:120],
        "lat_deg": round(lat, 7),
        "lon_deg": round(lon, 7),
        "source_kind": "sensorthings_observation",
    }

    result = observation.get("result")
    if isinstance(result, bool):
        record["result_text"] = str(result)
    elif isinstance(result, (int, float)):
        record["result_value"] = float(result)
    elif isinstance(result, str):
        record["result_text"] = result[:200]

    if isinstance(observation.get("phenomenonTime"), str):
        record["phenomenon_time"] = observation["phenomenonTime"][:64]

    for target, value in (("datastream_name", datastream.get("name")),
                          ("observed_property", (datastream.get("ObservedProperty") or {}).get("name")
                           if isinstance(datastream.get("ObservedProperty"), dict) else None),
                          ("sensor_name", (datastream.get("Sensor") or {}).get("name")
                           if isinstance(datastream.get("Sensor"), dict) else None),
                          ("thing_name", (datastream.get("Thing") or {}).get("name")
                           if isinstance(datastream.get("Thing"), dict) else None)):
        if isinstance(value, str) and value:
            record[target] = value[:120]

    unit = datastream.get("unitOfMeasurement")
    if isinstance(unit, dict) and isinstance(unit.get("symbol"), str):
        record["unit_symbol"] = unit["symbol"][:32]

    return record


def run() -> None:
    session = zenoh.open(make_config())

    def on_sample(sample) -> None:
        try:
            record = normalize(payload_json(sample))
            if record:
                publish_dual(session, OUTPUT_TOPIC, record, SensorThingsObservation, zenoh)
        except Exception as exc:
            print("SensorThings decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("SensorThings translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="SensorThings Observations on Zenoh -> sensor records").parse_args()
    run()
