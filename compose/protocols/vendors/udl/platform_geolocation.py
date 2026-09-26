#!/usr/bin/env python3
"""UDL-style platform geolocation feed on the EFDI Backbone trial fabric ->
EFDI tracks.

No named vendor in the topic (just a hash slot id + "platform-geolocation/v0")
and no portal Schema viewer reference found for it (unlike tacvox.py's own,
cited by SHA-256) — this decoder is reverse-engineered purely from live
traffic captured on the real gateway on 2026-09-26, not a documented spec.
Re-verify field meanings against a real portal/vendor reference if one
surfaces; treat unit conversions below as best-effort, not certain.

Identified as UDL (Unified Data Library — a real US DoD data-sharing
convention, not a guessed name) by the payload's own "udl": true flag.
Shape, live-captured:

  {"metadata": {"message_id_origin_system": <uuid>,
                "time_message_origin_system": <unix seconds>,
                "time_received": <unix seconds>,
                "data_mode": "SIMULATED" | (other values never observed),
                "udl": true},
   "platform": {"platform_id": <str>, "platform_type": <str, e.g.
                "civilian-aircraft">},
   "geolocation": {"latitude": <deg>, "longitude": <deg>, "altitude": <?>,
                   "speed": <?>, "heading": <deg>}}

altitude/speed units are UNCONFIRMED — the one live sample captured had
altitude=10000.0 (plausible meters) and speed=0.01 (implausibly slow for
m/s on a listed "civilian-aircraft"; more likely km/s, mach, or a
placeholder in that sample specifically). Published through as-is rather
than guessed-and-converted; do not trust alt_m/speed_ms here as strongly as
a cited-spec decoder's.

Every live sample seen so far had data_mode="SIMULATED" — this feed may be
entirely synthetic test traffic, not real platform positions. _src below
carries data_mode through so nothing downstream mistakes it for confirmed
real data.
"""

from __future__ import annotations

import time

from protocols.vendors.random.gateway import TOPIC_ROOT, open_session, payload_json, publish_dual, subscribe
from protocols.vendors.random.proto.normalized_track_pb2 import NormalizedTrack

_TOPIC_SUFFIX = "/platform-geolocation/v0"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

# platform_type strings observed: "civilian-aircraft". Mapped to a plain
# entity slug for SAPIENT's classification (flex335.py's _classification_for
# substring-matches "aircraft"/"vessel"/"vehicle"/"person" — not "platform").
_DOMAIN_FOR_TYPE = {
    "aircraft": "air",
    "vessel": "sea",
    "ship": "sea",
    "vehicle": "land",
}


def _domain_for(platform_type: str) -> str:
    lowered = (platform_type or "").lower()
    for token, domain in _DOMAIN_FOR_TYPE.items():
        if token in lowered:
            return domain
    return "land"


def platform_geolocation_record(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    geo = payload.get("geolocation") or {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    if lat is None or lon is None:
        return None
    platform = payload.get("platform") or {}
    metadata = payload.get("metadata") or {}
    platform_type = platform.get("platform_type") or ""

    record = {
        "_ts": metadata.get("time_message_origin_system") or metadata.get("time_received") or time.time(),
        "_src": "backbone:udl:{}".format(metadata.get("data_mode", "unknown")).lower(),
        "uid": platform.get("platform_id") or "udl-{}".format(metadata.get("message_id_origin_system", "unknown")),
        "callsign": platform.get("platform_id") or "",
        "lat_deg": lat,
        "lon_deg": lon,
        "target_type": platform_type,
        "affiliation": "unknown",
    }
    if geo.get("altitude") is not None:
        record["alt_m"] = geo["altitude"]
    if geo.get("speed") is not None:
        record["speed_ms"] = geo["speed"]
    if geo.get("heading") is not None:
        record["heading_deg"] = geo["heading"]
    return record, _domain_for(platform_type)


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("udl platform_geolocation: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        if not key.endswith(_TOPIC_SUFFIX):
            return
        try:
            payload = payload_json(sample)
        except (ValueError, UnicodeDecodeError):
            return
        try:
            result = platform_geolocation_record(payload)
        except Exception as exc:  # noqa: BLE001 — malformed upstream sample must not kill this process
            print("udl platform_geolocation decode error on {}: {}".format(key, exc), flush=True)
            return
        if result is None:
            return
        record, domain = result
        affiliation = record.pop("affiliation")
        prefix = "{}/{}/backbone/{}/unit".format(TOPIC_ROOT, domain, affiliation)
        publish_dual(session, prefix, record, NormalizedTrack)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("udl platform_geolocation: {} -> tracks".format(INPUT_TOPIC), flush=True)
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
