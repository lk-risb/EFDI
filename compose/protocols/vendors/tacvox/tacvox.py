#!/usr/bin/env python3
"""tacvox's ingest batch on the EFDI Backbone trial fabric -> EFDI tracks.

tacvox is a tactical voice-net platform (live cross-language radio
translation, per-person health/readiness, TAK bridging) that publishes one
big batch document per cycle at "<slot>/tacvox/ingest/v1". The wire format is
plain JSON (schema uploaded to the portal for documentation/compatibility
checking only, not encoding) — but it's a batch envelope (IngestBatch) with
several nested arrays, not the single flat/one-level-nested record
protocols/random/generic_json.py's guesser expects, so it needs its own
normalizer. Schema reference: portal Schema viewer, topic
"<slot>/tacvox/ingest/v1", schema v1, SHA-256
95d9d533171ad140df2ed869eeeb196d319289f86bc7cbd5500b0869cd9b8cd4.

Two of the batch's arrays carry a position and are decoded here:

  batch.tak[]  — already CoT-shaped (callsign/type/state/lat/lon), this
    vendor's own bridge to/from TAK riding inside their ingest feed.

  batch.entries[]  — voice transmissions, each stamped with the speaker's own
    lat/lon/heading/motion at time of transmission. A live personnel/unit
    position feed piggybacking on radio traffic.

Not decoded (out of scope for this pass, no immediate map need):
  batch.health[] / health_history[] — per-person vitals/readiness. Also
    carries lat/lon; a real future use (e.g. flagging RED-band casualties)
    would justify adding this, but isn't built here yet.
  batch.events[] / sessions[] / devices[] / weather[] — net administration,
    not positional.
"""

from __future__ import annotations

import json
import time

from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

_INGEST_SUFFIX = "/tacvox/ingest/v1"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"


def _has_position(item: dict) -> bool:
    lat, lon = item.get("lat"), item.get("lon")
    return isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and not (lat == 0 and lon == 0)


def tak_records(batch: dict) -> list[dict]:
    out = []
    for item in batch.get("tak", []) or []:
        if not isinstance(item, dict) or not _has_position(item):
            continue
        out.append({
            "_ts": time.time(),
            "_src": "backbone:tacvox:tak",
            "uid": "tacvox-tak-{}".format(item.get("id", item.get("entryId", "?"))),
            "lat_deg": item["lat"],
            "lon_deg": item["lon"],
            "label": item.get("callsign") or item.get("summary") or "tacvox TAK",
            "cot_type": item.get("type"),
            "cot_state": item.get("state"),
        })
    return out


def entry_records(batch: dict) -> list[dict]:
    out = []
    for item in batch.get("entries", []) or []:
        if not isinstance(item, dict) or not _has_position(item):
            continue
        record = {
            "_ts": time.time(),
            "_src": "backbone:tacvox:entry",
            "uid": "tacvox-{}".format(item.get("from") or item.get("session") or item.get("id", "?")),
            "lat_deg": item["lat"],
            "lon_deg": item["lon"],
            "label": item.get("from") or "tacvox radio",
        }
        if isinstance(item.get("altM"), (int, float)):
            record["alt_m"] = item["altM"]
        if isinstance(item.get("headingDeg"), (int, float)):
            record["heading_deg"] = item["headingDeg"]
        if item.get("motion"):
            record["motion"] = item["motion"]
        out.append(record)
    return out


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("tacvox: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        if not key.endswith(_INGEST_SUFFIX):
            return
        try:
            batch = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(batch, dict):
            return
        try:
            for record in tak_records(batch) + entry_records(batch):
                topic = TOPIC_ROOT + "/land/backbone/unknown/unit/tracks/v1"
                session.put(topic, json.dumps(record).encode())
        except Exception as exc:
            print("tacvox decode error: {}".format(exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("tacvox: {} -> tracks (tak[], entries[])".format(INPUT_TOPIC), flush=True)
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
