#!/usr/bin/env python3
"""Generic flat/nested JSON with a position -> normalized tracks.

bridges/vendors/random/backbone_bridge.py lands anything it can't identify as ASTERIX under
TOPIC_ROOT/raw/backbone/<key>, unmodified — a trial fabric has no shared
schema, so this translator accepts a deliberately small, documented contract
and ignores anything that does not match, same approach as
protocols/vendors/random/mqtt_json.py for MQTT's equally schema-less broker traffic:

  required : a latitude/longitude, either a flat key pair (lat|latitude|
             lat_deg / lon|lng|longitude|lon_deg) or nested one level under
             "location" (covers the "catalyst" convention several trial-
             fabric participants use: {"location": {"latitude", "longitude"}})
  optional : id (ci_uuid|ci_name|id|uid), affiliation (top-level
             "affiliation", or nested "metadata.affiliation"), dimension
             (top-level "dimension": air|land|sea|space, else guessed from
             a "type"/"device.type" string containing uav/aircraft/vessel/
             ship, else "land"), label (ci_name|label|name)

A payload missing a position is not a map object and is skipped — its exact
bytes remain on the raw topic for protocols/vendors/random/geojson.py or a future
normalizer to try.
"""

from __future__ import annotations

import json
import re
import time

from echo_filter import EchoFilter
from icao_ghosts import GhostFilter
from gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

_LAT_KEYS = ("lat", "latitude", "lat_deg")
_LON_KEYS = ("lon", "lng", "longitude", "lon_deg")
_ID_KEYS = ("ci_uuid", "ci_name", "id", "uid")
_LABEL_KEYS = ("ci_name", "label", "name")
_AFFILIATION_SLOT = {
    "friendly": "friendly",
    "hostile": "hostile",
    "neutral": "neutral",
}
_DIMENSIONS = ("air", "land", "sea", "space")
# A participant that sends ADS-B names the object by its bare 6-digit ICAO address and
# declares no dimension or type; without this it is drawn as a stationary ground unit.
_ICAO_HEX = re.compile(r"^[0-9a-fA-F]{6}$")


def _first(payload: dict, keys) -> object:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _position(payload: dict) -> tuple[float, float] | None:
    lat = _number(_first(payload, _LAT_KEYS))
    lon = _number(_first(payload, _LON_KEYS))
    if lat is None or lon is None:
        location = payload.get("location")
        if isinstance(location, dict):
            lat = _number(_first(location, ("latitude",) + _LAT_KEYS))
            lon = _number(_first(location, ("longitude",) + _LON_KEYS))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    return lat, lon


def _dimension(payload: dict) -> str:
    explicit = str(payload.get("dimension", "")).lower()
    if explicit in _DIMENSIONS:
        return explicit
    kind = " ".join(str(v) for v in (
        payload.get("type"), (payload.get("device") or {}).get("type"),
    ) if v).lower()
    if "uav" in kind or "aircraft" in kind or "drone" in kind:
        return "air"
    if "vessel" in kind or "ship" in kind or "boat" in kind:
        return "sea"
    return "land"


def _affiliation_slot(payload: dict) -> str:
    raw = str(payload.get("affiliation") or (payload.get("metadata") or {}).get("affiliation", "")).lower()
    return _AFFILIATION_SLOT.get(raw, "unknown")


def normalize(payload: dict, origin: str) -> dict | None:
    """Map one participant's payload onto the fabric contract, or None if it cannot be."""
    if not isinstance(payload, dict):
        return None
    position = _position(payload)
    if position is None:
        return None
    lat, lon = position
    raw_id = _first(payload, _ID_KEYS) or origin
    label = _first(payload, _LABEL_KEYS)

    record = {
        "_ts": time.time(),
        "_src": "backbone:json:" + origin,
        "uid": "BACKBONE-" + "".join(
            ch if ch.isalnum() or ch in "._:-" else "_" for ch in str(raw_id))[:120],
        "lat_deg": round(lat, 6),
        "lon_deg": round(lon, 6),
        "callsign": str(label)[:120] if label else str(raw_id)[:120],
    }
    dimension = _dimension(payload)
    if (dimension == "land" and not payload.get("dimension") and not payload.get("type")
            and _ICAO_HEX.match(record["callsign"])):
        # Key it by icao24 like every other ADS-B source, so it merges with the same aircraft
        # from dangausakis or any other feed, and draw it as an aircraft.
        record["icao24"] = record["callsign"].lower()
        record["target_type"] = "aircraft"
        dimension = "air"
    return record, dimension, _affiliation_slot(payload)


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("json translator: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    prefix = INPUT_TOPIC[:-len("**")]
    echoes = EchoFilter()
    echo_sub = subscribe(session, TOPIC_ROOT + "/land/**", echoes.on_sample)
    ghosts = GhostFilter()

    def on_sample(sample) -> None:
        try:
            payload = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        try:
            key = str(sample.key_expr)
            origin = key[len(prefix):].split("/", 1)[0] if key.startswith(prefix) else key
            result = normalize(payload, origin)
            if result is None:
                return
            record, dimension, slot = result
            if echoes.is_echo(record):      # a partner's copy of one of our own sensors
                return
            if record.get("icao24") and ghosts.is_ghost(record["icao24"], record.get("callsign"), registration=record.get("registration")):
                return                      # a bit-flipped copy of an address already seen
            topic = "{}/{}/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, dimension, slot)
            session.put(topic, json.dumps(record).encode())
        except Exception as exc:
            print("json translator decode error:", exc, flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("json translator: {} -> {}/{{air,land,sea,space}}/backbone/**/unit/tracks/v1".format(INPUT_TOPIC, TOPIC_ROOT), flush=True)
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
