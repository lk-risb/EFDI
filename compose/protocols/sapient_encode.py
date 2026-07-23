#!/usr/bin/env python3
"""Encode EFDI tracks as BSI Flex 335 v2 SAPIENT messages.

flex335.py is the reading half — it decodes SAPIENT arriving from a sensor.
This is the writing half: it turns any normalized EFDI track, whatever protocol
produced it, into a real `SapientMessage` carrying a `DetectionReport`. A
consumer then needs to understand SAPIENT alone rather than ASTERIX, MAVLink,
AIS and the rest.

Encoding targets the official schema vendored at compose/vendor/sapient_msg
(Apache-2.0, see compose/vendor/README.md), not a local approximation, so the
output is interoperable with real SAPIENT systems.

SAPIENT is a common-denominator contract: it deliberately cannot express every
field a specific sensor knows (an ASTERIX CAT-48 return has no place for
`spi` or `mil_emergency`). This conversion is therefore lossy BY DESIGN, and is
published ALONGSIDE the per-protocol /v2 message and the byte-exact
/native/v1 tier rather than replacing them.
"""

from __future__ import annotations

import os
import time
import uuid

from sapient_msg.bsi_flex_335_v2_0.detection_report_pb2 import DetectionReport
from sapient_msg.bsi_flex_335_v2_0.location_pb2 import (
    LocationCoordinateSystem,
    LocationDatum,
)
from sapient_msg.bsi_flex_335_v2_0.sapient_message_pb2 import SapientMessage

# Crockford base32, per the ULID spec — excludes I, L, O and U so the encoded
# form cannot be misread. report_id/object_id are declared `is_ulid` in the
# schema, so a plain UUID would be the wrong shape.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# This pod's SAPIENT node identity. The schema requires a UUID; a deployment
# that has not set one gets a stable per-process fallback so output is still
# well-formed rather than empty.
_NODE_ID = os.environ.get("SAPIENT_NODE_ID", "") or str(uuid.uuid4())

# EFDI classification vocabulary -> SAPIENT classification strings. Values not
# listed fall through unchanged: an unknown-but-present classification is more
# useful to a consumer than silently dropping it.
_CLASSIFICATION = {
    "aircraft": "Air Vehicle",
    "uav": "Air Vehicle",
    "drone": "Air Vehicle",
    "helicopter": "Air Vehicle",
    "vessel": "Surface Vessel",
    "ship": "Surface Vessel",
    "boat": "Surface Vessel",
    "vehicle": "Land Vehicle",
    "car": "Land Vehicle",
    "truck": "Land Vehicle",
    "person": "Human",
    "pedestrian": "Human",
}


def _ulid(value: int | None = None) -> str:
    """26-character Crockford-base32 ULID.

    With no argument: 48-bit millisecond timestamp + 80 random bits, the normal
    ULID shape. With an argument the whole 128-bit value is supplied by the
    caller, which is how a per-object id stays byte-identical across reports.
    """
    if value is None:
        ms = int(time.time() * 1000) & ((1 << 48) - 1)
        value = (ms << 80) | (uuid.uuid4().int & ((1 << 80) - 1))
    value &= (1 << 128) - 1
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _stable_object_id(track: dict) -> str:
    """A ULID derived entirely from the object's identity.

    The whole 128 bits come from the identity hash — NOT just the random half.
    Deriving only the low bits leaves the timestamp half moving, so two reports
    for one aircraft a millisecond apart would produce different object_ids and
    the consumer would see two contacts instead of a track.
    """
    ident = str(
        track.get("uid")
        or track.get("icao24")
        or track.get("mmsi")
        or track.get("track_num")
        or track.get("callsign")
        or ""
    )
    if not ident:
        return _ulid()
    return _ulid(uuid.uuid5(uuid.NAMESPACE_OID, ident).int)


def _classification_for(track: dict) -> str | None:
    for key in ("classification", "sapient_class", "target_type", "entity"):
        raw = track.get(key)
        if isinstance(raw, str) and raw.strip():
            lowered = raw.lower()
            for token, mapped in _CLASSIFICATION.items():
                if token in lowered:
                    return mapped
            return raw
    return None


def track_to_sapient(track: dict, node_id: str = "") -> SapientMessage | None:
    """Build a SapientMessage/DetectionReport from a normalized EFDI track.

    Returns None when the track has no position: `location` is inside a
    mandatory oneof, so a DetectionReport without one is not a valid SAPIENT
    message and is better not published than published malformed.
    """
    lat, lon = track.get("lat_deg"), track.get("lon_deg")
    if lat is None or lon is None:
        return None

    message = SapientMessage()
    message.timestamp.FromNanoseconds(int(float(track.get("_ts") or time.time()) * 1e9))
    message.node_id = node_id or _NODE_ID

    report = message.detection_report
    report.report_id = _ulid()
    report.object_id = _stable_object_id(track)

    location = report.location
    location.x = float(lon)          # schema: x is normally longitude
    location.y = float(lat)          # schema: y is normally latitude
    altitude = track.get("geo_alt_m", track.get("baro_alt_m", track.get("alt_m")))
    if altitude is not None:
        location.z = float(altitude)
    location.coordinate_system = LocationCoordinateSystem.LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M
    location.datum = LocationDatum.LOCATION_DATUM_WGS84_G

    confidence = track.get("detection_confidence", track.get("confidence"))
    if confidence is not None:
        try:
            report.detection_confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            pass

    classification = _classification_for(track)
    if classification:
        entry = report.classification.add()
        entry.type = classification

    # Ground speed + heading describe the same vector ENUVelocity wants, just in
    # polar form; convert rather than drop it.
    speed, heading = track.get("speed_ms"), track.get("heading_deg")
    if speed is not None and heading is not None:
        try:
            import math

            radians = math.radians(float(heading))
            velocity = report.enu_velocity
            velocity.east_rate = float(speed) * math.sin(radians)
            velocity.north_rate = float(speed) * math.cos(radians)
            vertical = track.get("vertical_rate_ms")
            if vertical is not None:
                velocity.up_rate = float(vertical)
        except (TypeError, ValueError):
            pass

    identifier = track.get("callsign") or track.get("icao24") or track.get("uid")
    if identifier:
        report.id = str(identifier)[:128]

    return message


def sapient_topic(topic: str) -> str:
    """SAPIENT view of an object key (.../{id} -> .../{id}/sapient).

    Named like every other view so no format is implicit — a consumer reading
    the key always knows what the bytes are.
    """
    if topic.endswith("/sapient"):
        return topic
    return topic + "/sapient"


def publish_sapient(session, topic: str, track: dict, zenoh, node_id: str = "") -> None:
    """Publish the SAPIENT view of a track. Best-effort, like the other tiers:
    a conversion failure must never take down the JSON or protobuf legs."""
    try:
        message = track_to_sapient(track, node_id=node_id)
        if message is None:
            return
        payload = message.SerializeToString()
    except Exception as exc:  # noqa: BLE001 — never break the other tiers
        print("sapient encode failed for {}: {}".format(topic, exc), flush=True)
        return
    session.put(sapient_topic(topic), payload, encoding=zenoh.Encoding.APPLICATION_PROTOBUF)
