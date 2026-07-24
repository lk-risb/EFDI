#!/usr/bin/env python3
"""Eclipse Sparkplug B on Zenoh -> normalized sensor records.

bridges/mqtt_bridge.py forwards broker payloads verbatim, so Sparkplug's
protobuf arrives untouched under ``.../raw/mqtt/spBv1.0/**``. The specification
reserves that topic namespace, which is why this decoder can subscribe to it
directly instead of sniffing every payload on the broker: JSON feeds stay with
protocols/random/mqtt_json.py and the two never contend.

Sparkplug topic grammar (levels become Zenoh key segments):
    spBv1.0/{group_id}/{message_type}/{edge_node_id}[/{device_id}]

Aliases are the part that catches naive decoders. A BIRTH certificate (NBIRTH /
DBIRTH) publishes each metric's full name *and* its numeric alias; the DATA
messages that follow may then carry the alias alone to save bandwidth. A decoder
that ignores BIRTHs sees nameless metrics forever, so this one keeps the
alias->name table each node declared and resolves DATA metrics through it.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from importlib import import_module

import zenoh

from protocols.protobuf_codec import publish_dual
from translation_common import TOPIC_ROOT, make_config, payload_bytes

Payload = import_module("protocols.vendors.sparkplug.sparkplug_b_pb2").Payload
SparkplugRecord = import_module(
    "protocols.vendors.sparkplug.sparkplug_track_pb2").SparkplugRecord

INPUT_TOPIC = os.environ.get("SPARKPLUG_INPUT_TOPIC") or TOPIC_ROOT + "/raw/mqtt/spBv1.0/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/sparkplug/iot/unknown/sensor"

# Bounded so a broker with churning edge nodes cannot grow this without limit.
MAX_TRACKED_NODES = int(os.environ.get("SPARKPLUG_MAX_NODES", "512"))

_BIRTH_TYPES = {"NBIRTH", "DBIRTH"}
_DEATH_TYPES = {"NDEATH", "DDEATH"}

_LAT_NAMES = {"latitude", "lat", "gps_lat", "latitude_deg", "lat_deg"}
_LON_NAMES = {"longitude", "lon", "lng", "long", "gps_lon", "longitude_deg", "lon_deg"}
_ALT_NAMES = {"altitude", "alt", "altitude_m", "alt_m", "elevation", "height"}
_SPEED_NAMES = {"speed", "speed_ms", "velocity", "ground_speed"}
_HEADING_NAMES = {"heading", "course", "bearing", "heading_deg", "track"}


def parse_topic(key: str) -> dict | None:
    """Split a Zenoh key back into the Sparkplug topic levels it came from."""
    marker = "/raw/mqtt/spBv1.0/"
    index = key.find(marker)
    if index < 0:
        return None
    levels = [level for level in key[index + len(marker):].split("/") if level]
    if len(levels) < 3:
        return None
    return {
        "group_id": levels[0],
        "message_type": levels[1],
        "edge_node_id": levels[2],
        "device_id": levels[3] if len(levels) > 3 else "",
    }


def metric_value(metric) -> object:
    """Return the metric's set value, whichever branch of the oneof carries it."""
    if metric.is_null:
        return None
    which = metric.WhichOneof("value")
    if which in (None, "dataset_value", "template_value", "extension_value"):
        return None
    if which == "bytes_value":
        return None
    return getattr(metric, which)


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


def _short_name(name: str) -> str:
    """Sparkplug metric names are paths: 'Properties/GPS/Latitude'."""
    return name.rsplit("/", 1)[-1].strip().lower().replace(" ", "_")


class AliasTable:
    """alias -> metric name, learned from each node's BIRTH certificate."""

    def __init__(self, limit: int = MAX_TRACKED_NODES):
        self._tables: dict[tuple, dict[int, str]] = {}
        self._limit = limit

    @staticmethod
    def _scope(topic: dict) -> tuple:
        return (topic["group_id"], topic["edge_node_id"], topic["device_id"])

    def learn(self, topic: dict, payload) -> None:
        scope = self._scope(topic)
        if scope not in self._tables and len(self._tables) >= self._limit:
            # Drop the oldest tracked node rather than grow without bound.
            self._tables.pop(next(iter(self._tables)))
        table = self._tables.setdefault(scope, {})
        for metric in payload.metrics:
            if metric.name and metric.alias:
                table[metric.alias] = metric.name

    def forget(self, topic: dict) -> None:
        self._tables.pop(self._scope(topic), None)

    def resolve(self, topic: dict, metric) -> str:
        if metric.name:
            return metric.name
        if metric.alias:
            return self._tables.get(self._scope(topic), {}).get(metric.alias, "")
        return ""


def normalize(payload, topic: dict, aliases: AliasTable, now: float | None = None) -> dict | None:
    """Turn one Sparkplug payload into a positioned record, or None."""
    now = time.time() if now is None else float(now)

    named: dict[str, object] = {}
    for metric in payload.metrics:
        name = aliases.resolve(topic, metric)
        if not name:
            continue
        value = metric_value(metric)
        if value is not None:
            named[name] = value

    lookup = {_short_name(name): value for name, value in named.items()}

    def pick(candidates):
        for candidate in candidates:
            if candidate in lookup:
                number = _number(lookup[candidate])
                if number is not None:
                    return number
        return None

    lat = pick(_LAT_NAMES)
    lon = pick(_LON_NAMES)
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    identity = topic["device_id"] or topic["edge_node_id"]
    stamp = payload.timestamp / 1000.0 if payload.timestamp else now

    record = {
        "_ts": stamp,
        "_src": "sparkplug",
        "uid": "SPB-" + "".join(
            ch if ch.isalnum() or ch in "._:-" else "_"
            for ch in "{}-{}".format(topic["group_id"], identity))[:120],
        "lat_deg": round(lat, 7),
        "lon_deg": round(lon, 7),
        "source_kind": "sparkplug_b",
        "group_id": topic["group_id"],
        "edge_node_id": topic["edge_node_id"],
        "message_type": topic["message_type"],
    }
    if topic["device_id"]:
        record["device_id"] = topic["device_id"]
    if payload.seq:
        record["seq"] = int(payload.seq)

    for target, candidates in (("geo_alt_m", _ALT_NAMES),
                               ("speed_ms", _SPEED_NAMES),
                               ("heading_deg", _HEADING_NAMES)):
        value = pick(candidates)
        if value is not None:
            record[target] = value

    scalars = {name: value for name, value in list(named.items())[:64]
               if isinstance(value, (str, int, float, bool))}
    record["metrics_json"] = json.dumps(scalars, separators=(",", ":"))
    return record


def run() -> None:
    session = zenoh.open(make_config())
    aliases = AliasTable()

    def on_sample(sample) -> None:
        try:
            topic = parse_topic(str(sample.key_expr))
            if topic is None:
                return
            payload = Payload()
            payload.ParseFromString(payload_bytes(sample))
        except Exception:
            # Not a Sparkplug payload after all — leave it on the raw topic.
            return
        try:
            message_type = topic["message_type"].upper()
            if message_type in _BIRTH_TYPES:
                aliases.learn(topic, payload)
            elif message_type in _DEATH_TYPES:
                aliases.forget(topic)
                return
            record = normalize(payload, topic, aliases)
            if record:
                publish_dual(session, OUTPUT_TOPIC, record, SparkplugRecord, zenoh)
        except Exception as exc:
            print("Sparkplug decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("Sparkplug B translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="Sparkplug B on Zenoh -> sensor records").parse_args()
    run()
