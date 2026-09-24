#!/usr/bin/env python3
"""Backbone camera detection feeds -> static sensor-site markers.

Some trial-fabric participants publish camera-based detections under
"<org>/<camera-name>/detections_<site>/v1" (confirmed live: "hawk01",
sites "heathrow" and "berlin"). The payload carries per-frame detection
counts and an embedded JPEG keyframe, but no coordinates at all — cameras
are fixed, so unlike every other normalizer here (which reads a position
out of the payload), this one reads it out of a small known-sites table
keyed by the site name already encoded in the topic itself.

Deliberately narrow: only sites in _KNOWN_SITES are placed. A camera name
this pod hasn't seen before publishes nothing rather than guessing a
location — add it to the table once its real site is confirmed, the same
as any other vendor-specific mapping in this codebase.

Reuses tak_layer.py's existing sensor-alert coloring (green/yellow/red by
last_detection_ts age, see layers/tak_layer.py's _sensor_alert_cot_type)
instead of adding a new remarks branch there: this file only sets
last_detection_ts when the frame actually reports a detection, exactly the
same signal dronuradaras_bridge.py already produces for the same color
logic. No tak_layer.py changes needed to show this.
"""

from __future__ import annotations

import json
import re
import time

from gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/backbone/neutral/sensor/tracks/v1"

# (lat, lon) — approximate site coordinates, not the camera's exact mount
# point (not published, and not needed at this zoom level).
_KNOWN_SITES = {
    "heathrow": (51.4700, -0.4543),   # London Heathrow Airport
    "berlin": (52.3667, 13.5033),     # Berlin Brandenburg Airport (BER)
}

_TOPIC_RE = re.compile(r"/([^/]+)/detections_([a-z0-9_-]+)/v\d+$", re.IGNORECASE)


def normalize(payload: dict, key: str) -> dict | None:
    match = _TOPIC_RE.search(key)
    if not match:
        return None
    camera, site = match.group(1), match.group(2).lower()
    coords = _KNOWN_SITES.get(site)
    if coords is None:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    num_detections = data.get("num_detections")
    lat, lon = coords

    record = {
        "_ts": time.time(),
        "_src": "backbone:camera:" + camera,
        "sensor_type": "camera",
        "sensor_id": "BACKBONE-{}-{}".format(camera, site),
        "sensor_name": "{} ({})".format(camera, site),
        "lat_deg": lat,
        "lon_deg": lon,
        "is_online": True,
    }
    if isinstance(num_detections, (int, float)) and num_detections > 0:
        record["last_detection_ts"] = time.time()
        record["detection_count"] = int(num_detections)
    return record


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("camera_sites: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        try:
            payload = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        try:
            record = normalize(payload, str(sample.key_expr))
            if record:
                session.put(OUTPUT_TOPIC, json.dumps(record).encode())
        except Exception as exc:
            print("camera_sites decode error:", exc, flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("camera_sites: {} -> {} (known sites: {})".format(
        INPUT_TOPIC, OUTPUT_TOPIC, ", ".join(sorted(_KNOWN_SITES))), flush=True)
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
