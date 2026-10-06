#!/usr/bin/env python3
"""2T Security's "soc-bx" schema on the EFDI Backbone trial fabric ->
EFDI tracks + sensor alerts.

Unlike protocols/vendors/random/generic_json.py and geojson.py, this participant's
data comes across as real protobuf, not JSON — so it needs a real .proto
schema, not a flat-key guess. The schema was fetched from the fabric portal's
Schema viewer (this vendor uploaded one, unlike most other participants) and
is vendored at compose/schemas/vendors/socbx/socbx_unified.proto and
socbx_alerts.proto — third-party schemas live under compose/schemas/vendors/
<vendor>/, kept apart from both this decoder (compose/protocols/vendors/socbx/)
and EFDI's own /v2 envelope contracts in compose/protocols/vendors/random/proto/ — see those
files for the schema's own provenance/SHA-256.

Two topics decoded, both fed by bridges/vendors/random/backbone_bridge.py's raw relay
(TOPIC_ROOT/raw/backbone/<original key>, unchanged from the fabric):

  <slot>/soc-bx/unified/raw/v1  (protobuf:UnifiedSchema) — a map of tracked
    objects, each with an optional Location/Identity/Kinematics block. This
    is the one with map value: decoded straight into EFDI tracks.

  <slot>/soc-bx/alerts/v1  (protobuf:Alert) — SIEM alerts with a lat/lon of
    their own. Decoded into point sensor-alert markers, same shape as
    protocols/vendors/random/camera_sites.py's output (no coloring logic of its own
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
import re
import time

from google.protobuf.message import DecodeError
from protocols.vendors.random.echo_filter import EchoFilter
from protocols.vendors.random.icao_ghosts import GhostFilter
from protocols.vendors.random.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe
from schemas.vendors.socbx.proto.socbx_alerts_pb2 import Alert
from schemas.vendors.socbx.proto.socbx_unified_pb2 import UnifiedSchema

# The slot prefix is a per-deployment fabric registration id, not something
# EFDI's own code should hardcode — matched by shape instead (any slot's
# soc-bx/unified/raw/v1 or soc-bx/alerts/v1 topic), so a re-registration on
# a new slot id keeps working without a code change.
_UNIFIED_SUFFIX = "/soc-bx/unified/raw/v1"
_ALERTS_SUFFIX = "/soc-bx/alerts/v1"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

# An object whose id is a 6-digit hex number, bare or behind a word prefix ("STATES:502D5A", seen
# live), and which carries no real domain or type is an ADS-B aircraft keyed by its ICAO address. This vendor republishes ADS-B that way, and
# tags many of those objects with the generic type "sensor" (seen live: object_type
# "sensor", platform_type "Sensor" on an aircraft with a callsign), which says nothing.
_ICAO_HEX = re.compile(r"^(?:[A-Za-z]+:)?([0-9a-fA-F]{6})$")
_GENERIC_TYPES = ("", "sensor", "unknown")


def _generic(identity) -> bool:
    return (not (identity.domain or identity.object_subtype)
            and all(value.strip().lower() in _GENERIC_TYPES
                    for value in (identity.object_type, identity.platform_type, identity.vehicle_type)))

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
            record["callsign"] = identity.callsign.strip()
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
        icao = _ICAO_HEX.match(str(record["uid"]))
        if icao and _generic(identity):
            # Key it by icao24 like every other ADS-B source, so the same aircraft from
            # dangausakis or any other feed is one marker, and draw it as an aircraft
            # instead of a stationary ground unit.
            record["icao24"] = icao.group(1).lower()
            record["target_type"] = "aircraft"
            record["_dimension"] = "air"
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

    echoes = EchoFilter()
    echo_sub = subscribe(session, TOPIC_ROOT + "/land/**", echoes.on_sample)
    ghosts = GhostFilter()
    dropped = [0]

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        payload = payload_bytes(sample)
        try:
            if key.endswith(_UNIFIED_SUFFIX):
                for record in unified_records(payload):
                    if record.get("icao24") and ghosts.is_ghost(record["icao24"], record.get("callsign"), registration=record.get("registration")):
                        continue            # a bit-flipped copy of an address already seen
                    if echoes.is_echo(record):
                        dropped[0] += 1
                        if dropped[0] in (1, 100) or dropped[0] % 1000 == 0:
                            print("socbx: dropped {} echoes of our own sensors (last: {})".format(
                                dropped[0], record["uid"]), flush=True)
                        continue
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
        echo_sub.undeclare()
        session.close()


if __name__ == "__main__":
    run()
