#!/usr/bin/env python3
"""Embedded GeoJSON Point -> normalized tracks.

Same raw topic as protocols/random/generic_json.py
(TOPIC_ROOT/raw/backbone/<key>), a different recognition strategy: some
participants bury a standard GeoJSON Point Feature — {"type": "Feature",
"geometry": {"type": "Point", "coordinates": [lon, lat, ...]}} — arbitrarily
deep inside their own proprietary wrapper object rather than publishing
flat lat/lon fields. generic_json.py's flat/one-level-nested lookup never
finds that; this file walks the whole payload looking for the GeoJSON shape
specifically, regardless of what it's nested under.

Bounded recursion (depth and node count) — payload shape is untrusted trial-
fabric input, not sized or structured by us.
"""

from __future__ import annotations

import json
import time

from gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

_MAX_DEPTH = 8
_MAX_NODES = 2000


def _find_point_feature(node, depth: int, budget: list[int]):
    """Return (lon, lat) from the first GeoJSON Point Feature found, or None."""
    if depth > _MAX_DEPTH or budget[0] <= 0:
        return None
    budget[0] -= 1

    if isinstance(node, dict):
        geometry = node.get("geometry")
        if (
            node.get("type") == "Feature"
            and isinstance(geometry, dict)
            and geometry.get("type") == "Point"
        ):
            coords = geometry.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                try:
                    lon, lat = float(coords[0]), float(coords[1])
                except (TypeError, ValueError):
                    lon = lat = None
                if lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lon, lat
        for value in node.values():
            found = _find_point_feature(value, depth + 1, budget)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_point_feature(item, depth + 1, budget)
            if found is not None:
                return found
    return None


def normalize(payload, origin: str) -> dict | None:
    found = _find_point_feature(payload, 0, [_MAX_NODES])
    if found is None:
        return None
    lon, lat = found
    raw_id = payload.get("_id") if isinstance(payload, dict) else None
    return {
        "_ts": time.time(),
        "_src": "backbone:geojson:" + origin,
        "uid": "BACKBONE-" + "".join(
            ch if ch.isalnum() or ch in "._:-" else "_" for ch in str(raw_id or origin))[:120],
        "lat_deg": round(lat, 6),
        "lon_deg": round(lon, 6),
        "callsign": str(raw_id or origin)[:120],
    }


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("geojson translator: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    prefix = INPUT_TOPIC[:-len("**")]
    output_topic = TOPIC_ROOT + "/land/backbone/unknown/unit/tracks/v1"

    def on_sample(sample) -> None:
        try:
            payload = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        try:
            key = str(sample.key_expr)
            origin = key[len(prefix):].split("/", 1)[0] if key.startswith(prefix) else key
            record = normalize(payload, origin)
            if record:
                session.put(output_topic, json.dumps(record).encode())
        except Exception as exc:
            print("geojson translator decode error:", exc, flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("geojson translator: {} -> {}".format(INPUT_TOPIC, output_topic), flush=True)
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
