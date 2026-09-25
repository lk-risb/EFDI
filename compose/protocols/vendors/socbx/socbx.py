#!/usr/bin/env python3
"""2T Security's "soc-bx" schema on the EFDI Backbone trial fabric ->
EFDI tracks + sensor alerts.

Unlike protocols/random/generic_json.py and geojson.py, this participant's
data comes across as real protobuf, not JSON — so it needs a real .proto
schema, not a flat-key guess. The schema was fetched from the fabric portal's
Schema viewer (this vendor uploaded one, unlike most other participants) and
is vendored at compose/schemas/vendors/socbx/socbx_unified.proto and
socbx_alerts.proto — third-party schemas live under compose/schemas/vendors/
<vendor>/, kept apart from both this decoder (compose/protocols/vendors/socbx/)
and EFDI's own /v2 envelope contracts in compose/protocols/proto/ — see those
files for the schema's own provenance/SHA-256.

Two topics decoded, both fed by bridges/backbone_bridge.py's raw relay
(TOPIC_ROOT/raw/backbone/<original key>, unchanged from the fabric):

  <slot>/soc-bx/unified/raw/v1  (protobuf:UnifiedSchema) — a map of tracked
    objects, each with an optional Location/Identity/Kinematics block. This
    is the one with map value: decoded straight into EFDI tracks.

  <slot>/soc-bx/alerts/v1  (protobuf:Alert) — SIEM alerts with a lat/lon of
    their own. Decoded into point sensor-alert markers, same shape as
    protocols/random/camera_sites.py's output (no coloring logic of its own
    here either — tak_layer.py's existing red/green-by-age handles it, this
    file only needs to set last_detection_ts when a fresh alert arrives).

Two other topics on this vendor's slot — soc-bx/unified/anomalies/v1 and
soc-bx/unified/stats/v1 — also have uploaded schemas, but neither carries a
location field (Anomaly is a bare statistical score, UnifiedStats is
telemetry/aggregate statistics), so there's nothing to place on a map. Not
decoded here; add a decoder if a future use for them shows up.
"""

from __future__ import annotations

import json
import time

from google.protobuf.message import DecodeError
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe
from schemas.vendors.socbx.socbx_alerts_pb2 import Alert
from schemas.vendors.socbx.socbx_unified_pb2 import UnifiedSchema

# The slot prefix is a per-deployment fabric registration id, not something
# EFDI's own code should hardcode — matched by shape instead (any slot's
# soc-bx/unified/raw/v1 or soc-bx/alerts/v1 topic), so a re-registration on
# a new slot id keeps working without a code change.
_UNIFIED_SUFFIX = "/soc-bx/unified/raw/v1"
_ALERTS_SUFFIX = "/soc-bx/alerts/v1"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

_DOMAIN_AIR = ("uav", "aircraft", "drone", "air", "helicopter", "rotary")
_DOMAIN_SEA = ("vessel", "ship", "boat", "sea", "maritime", "surface")


def _dimension(identity) -> str:
    haystack = " ".join(filter(None, [
        identity.domain, identity.object_type, identity.object_subtype,
        identity.platform_type, identity.vehicle_type,
    ])).lower()
    if any(tok in haystack for tok in _DOMAIN_AIR):
        return "air"
    if any(tok in haystack for tok in _DOMAIN_SEA):
        return "sea"
    return "land"


def _slot(identity) -> str:
    affiliation = (identity.affiliation or "").strip().lower()
    return affiliation if affiliation else "unknown"


def unified_records(payload: bytes) -> list[dict]:
    msg = UnifiedSchema()
    msg.ParseFromString(payload)
    out = []
    for object_id, encounter in msg.objects.items():
        features = encounter.features
        location = features.location
        if not features.HasField("location") or (location.latitude == 0 and location.longitude == 0):
            continue
        identity = features.identity
        kinematics = features.kinematics
        record = {
            "_ts": time.time(),
            "_src": "backbone:socbx:{}".format(msg.node_id or "unknown"),
            "uid": identity.object_id or identity.track_id or object_id,
            "lat_deg": location.latitude,
            "lon_deg": location.longitude,
            "label": identity.label or identity.name or identity.callsign or object_id,
            "affiliation": identity.affiliation or "unknown",
        }
        if location.HasField("altitude"):
            record["alt_m"] = location.altitude
        if identity.callsign:
            record["callsign"] = identity.callsign
        if identity.object_type:
            record["object_type"] = identity.object_type
        if identity.classification:
            record["classification"] = identity.classification
        if kinematics.HasField("heading"):
            record["heading_deg"] = kinematics.heading
        elif kinematics.HasField("course"):
            record["heading_deg"] = kinematics.course
        if kinematics.HasField("speed"):
            record["speed_mps"] = kinematics.speed
        elif kinematics.HasField("ground_speed"):
            record["speed_mps"] = kinematics.ground_speed
        record["_dimension"] = _dimension(identity)
        record["_slot"] = _slot(identity)
        out.append(record)
    return out


def alert_record(payload: bytes) -> dict | None:
    msg = Alert()
    msg.ParseFromString(payload)
    if msg.latitude == 0 and msg.longitude == 0:
        return None
    record = {
        "_ts": time.time(),
        "_src": "backbone:socbx:alert:{}".format(msg.vendor or msg.source or "unknown"),
        "sensor_type": "soc",
        "sensor_id": "SOCBX-{}".format(msg.id or msg.external_id or msg.node_id),
        "sensor_name": msg.title or msg.category or "SOC-BX alert",
        "lat_deg": float(msg.latitude),
        "lon_deg": float(msg.longitude),
        "is_online": True,
    }
    if msg.status.lower() not in ("closed", "resolved", "acknowledged"):
        record["last_detection_ts"] = time.time()
        record["detection_count"] = max(1, msg.event_count)
    return record


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("socbx: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        payload = payload_bytes(sample)
        try:
            if key.endswith(_UNIFIED_SUFFIX):
                for record in unified_records(payload):
                    dimension = record.pop("_dimension")
                    slot = record.pop("_slot")
                    topic = "{}/{}/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, dimension, slot)
                    session.put(topic, json.dumps(record).encode())
            elif key.endswith(_ALERTS_SUFFIX):
                record = alert_record(payload)
                if record:
                    topic = TOPIC_ROOT + "/land/backbone/neutral/sensor/tracks/v1"
                    session.put(topic, json.dumps(record).encode())
        except DecodeError as exc:
            print("socbx decode error on {}: {}".format(key, exc), flush=True)
        except Exception as exc:
            print("socbx error on {}: {}".format(key, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("socbx: {} -> tracks + sensor alerts (soc-bx/unified/raw/v1, soc-bx/alerts/v1)".format(INPUT_TOPIC), flush=True)
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
