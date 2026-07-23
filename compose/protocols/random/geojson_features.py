#!/usr/bin/env python3
"""GeoJSON / OGC API Features on Zenoh -> normalized map features."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time

import zenoh

from namespace_prefix import prefix
from protocols.random.geojson_features_pb2 import GeoFeature
from protocols.protobuf_codec import publish_dual
from translation_common import TOPIC_ROOT, make_config, payload_json


INPUT_TOPIC = os.environ.get("GEOJSON_INPUT_TOPIC") or TOPIC_ROOT + "/raw/geojson/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/ogc/neutral/zone/features/v1"


def _points(geometry: dict) -> list[tuple[float, float]]:
    coordinates = geometry.get("coordinates")
    result: list[tuple[float, float]] = []

    def walk(value) -> None:
        if isinstance(value, (list, tuple)) and len(value) >= 2 \
                and all(isinstance(item, (int, float)) for item in value[:2]):
            lon, lat = float(value[0]), float(value[1])
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                result.append((lat, lon))
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
    walk(coordinates)
    return result


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def _features(payload) -> list[dict]:
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        return [item for item in payload.get("features", []) if isinstance(item, dict)]
    if isinstance(payload, dict) and payload.get("type") == "Feature":
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def normalize(feature: dict, now: float | None = None) -> dict | None:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in {
            "Point", "LineString", "Polygon", "MultiPoint", "MultiLineString",
            "MultiPolygon"}:
        return None
    points = _points(geometry)
    center = _centroid(points)
    if not center:
        return None
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    raw_id = feature.get("id") or properties.get("id")
    if raw_id is None:
        raw_id = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()[:24]
    now = time.time() if now is None else float(now)
    record = {
        "_ts": now,
        "_src": "ogc_features",
        "uid": "GEO-" + "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in str(raw_id))[:120],
        "lat_deg": round(center[0], 7),
        "lon_deg": round(center[1], 7),
        "source_kind": "geojson_feature",
        "geometry": geometry,
        "geometry_type": geometry.get("type"),
        "feature_properties": {str(k): v for k, v in list(properties.items())[:64]
                                if isinstance(v, (str, int, float, bool))},
    }
    flat_coordinates: list[float] = []
    for lat, lon in points:
        flat_coordinates.extend([lon, lat])
    if flat_coordinates:
        record["coordinates"] = flat_coordinates
    record["properties_json"] = json.dumps(record["feature_properties"], separators=(",", ":"))
    for target, key in (("name", "name"), ("zone_id", "zone_id"), ("description", "description"),
                        ("valid_from", "valid_from"), ("valid_until", "valid_until")):
        if key in properties and isinstance(properties[key], (str, int, float)):
            record[target] = properties[key]
    return record


def run() -> None:
    session = zenoh.open(make_config())

    def on_sample(sample) -> None:
        try:
            payload = payload_json(sample)
            for feature in _features(payload):
                record = normalize(feature)
                if record:
                    publish_dual(session, OUTPUT_TOPIC, record, GeoFeature, zenoh)
        except Exception as exc:
            print("GeoJSON decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("GeoJSON translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="GeoJSON/OGC Features on Zenoh -> EFDI").parse_args()
    run()
