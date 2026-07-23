#!/usr/bin/env python3
"""GeoJSON/JSON mission routes and corridors on Zenoh -> route records."""

from __future__ import annotations

import argparse
import json
import os
import time

import zenoh

from protocols.random.mission_route_pb2 import MissionRoute
from protocols.protobuf_codec import publish_dual
from translation_common import TOPIC_ROOT, make_config, payload_json


INPUT_TOPIC = os.environ.get("MISSION_ROUTE_INPUT_TOPIC") or TOPIC_ROOT + "/raw/routes/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/air/mission/c2/unknown/aircraft"


def _geometry(value: dict) -> dict | None:
    if value.get("type") == "Feature":
        return value.get("geometry") if isinstance(value.get("geometry"), dict) else None
    return value if value.get("type") in {"LineString", "MultiLineString", "Polygon"} else None


def normalize(value: dict, now: float | None = None) -> dict | None:
    if not isinstance(value, dict):
        return None
    geometry = _geometry(value)
    properties = value.get("properties") if isinstance(value.get("properties"), dict) else value
    if not geometry or not isinstance(properties, dict):
        return None
    if geometry.get("type") not in {"LineString", "MultiLineString", "Polygon"}:
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return None
    now = time.time() if now is None else float(now)
    uid = str(value.get("id") or properties.get("id") or properties.get("route_id") or "route-" + str(int(now)))
    record = {"_ts": now, "_src": "mission_route", "uid": "ROUTE-" + uid[:140],
              "source_kind": "mission_route", "geometry": geometry,
              "route_type": str(properties.get("route_type") or properties.get("type") or "route")[:64],
              "route_properties": {str(k): v for k, v in list(properties.items())[:64]
                                   if isinstance(v, (str, int, float, bool))}}
    for target, keys in {"callsign": ("callsign", "name", "uas_id"),
                         "lower_altitude_m": ("lower_altitude_m", "min_alt_m"),
                         "upper_altitude_m": ("upper_altitude_m", "max_alt_m"),
                         "valid_from": ("valid_from", "start_time"),
                         "valid_until": ("valid_until", "end_time")}.items():
        for key in keys:
            item = properties.get(key)
            if isinstance(item, (str, int, float)):
                record[target] = item
                break
    # Use the first valid coordinate as a marker anchor for current output
    # layers; geometry-aware output will render the complete route.
    def first_point(value):
        if isinstance(value, list) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
            return float(value[1]), float(value[0])
        if isinstance(value, list):
            for item in value:
                found = first_point(item)
                if found:
                    return found
        return None
    point = first_point(coordinates)
    if not point:
        return None
    record.update(lat_deg=point[0], lon_deg=point[1])
    flat_coordinates: list[float] = []
    for item in coordinates:
        if isinstance(item, list):
            stack = [item]
            while stack:
                value = stack.pop(0)
                if isinstance(value, list) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
                    flat_coordinates.extend([float(value[0]), float(value[1])])
                elif isinstance(value, list):
                    stack[:0] = list(value)
    if flat_coordinates:
        record["coordinates"] = flat_coordinates
    record["properties_json"] = json.dumps(record["route_properties"], separators=(",", ":"))
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
                    publish_dual(session, OUTPUT_TOPIC, record, MissionRoute, zenoh)
        except Exception as exc:
            print("mission route decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("Mission-route translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="Mission routes on Zenoh -> EFDI").parse_args()
    run()
