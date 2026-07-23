#!/usr/bin/env python3
"""OASIS CAP 1.2 XML on Zenoh -> normalized alert/area records.

Input publishers (weather, NOTAM, civil protection, UTM) publish complete CAP
documents under ``.../raw/cap/**``.  This translator owns no HTTP endpoint and
publishes JSON understood by the CoT/NVG layers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import time

from defusedxml import ElementTree as ET
import zenoh

from namespace_prefix import prefix
from protocols.random.cap_pb2 import CapAlert
from protocols.protobuf_codec import publish_dual
from translation_common import TOPIC_ROOT, make_config, payload_bytes


INPUT_TOPIC = os.environ.get("CAP_INPUT_TOPIC") or TOPIC_ROOT + "/raw/cap/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/cap/neutral/sensor/alerts/v1"
MAX_XML = 2_000_000
_NS = "urn:oasis:names:tc:emergency:cap:1.2"


def _text(parent, name: str) -> str:
    if parent is None:
        return ""
    for child in list(parent):
        if child.tag.split("}")[-1] == name and child.text:
            return " ".join(child.text.split())
    found = parent.find(".//{%s}%s" % (_NS, name))
    if found is not None and found.text:
        return " ".join(found.text.split())
    return ""


def _timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _point(value: str) -> tuple[float, float] | None:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) < 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _geometry(area) -> tuple[dict | None, tuple[float, float] | None]:
    polygons: list[list[list[float]]] = []
    circles: list[dict] = []
    for child in area.iter():
        local = child.tag.split("}")[-1]
        text = (child.text or "").strip()
        if local == "polygon" and text:
            points = [_point(p) for p in text.split(" ")]
            points = [p for p in points if p]
            if len(points) >= 3:
                ring = [[lon, lat] for lat, lon in points]
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                polygons.append(ring)
        elif local == "circle" and text:
            parts = text.split()
            point = _point(parts[0]) if parts else None
            try:
                radius_km = float(parts[1]) if len(parts) > 1 else 0.0
            except ValueError:
                radius_km = 0.0
            if point and math.isfinite(radius_km) and radius_km > 0:
                circles.append({"type": "Circle", "center": [point[1], point[0]],
                                "radius_km": min(radius_km, 1000.0)})
    if polygons:
        ring = polygons[0]
        lat = sum(point[1] for point in ring[:-1]) / max(1, len(ring) - 1)
        lon = sum(point[0] for point in ring[:-1]) / max(1, len(ring) - 1)
        geometry = {"type": "Polygon", "coordinates": [ring]}
        return geometry, (lat, lon)
    if circles:
        circle = circles[0]
        lon, lat = circle["center"]
        return circle, (lat, lon)
    description = _text(area, "areaDesc")
    match = re.search(r"(-?\d+(?:\.\d+)?)[, ]+(-?\d+(?:\.\d+)?)", description)
    point = _point(match.group(0).replace(" ", ",")) if match else None
    return None, point


def parse_cap(xml: bytes, now: float | None = None) -> list[dict]:
    if len(xml) > MAX_XML:
        return []
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        return []
    now = time.time() if now is None else float(now)
    identifier = _text(root, "identifier") or "cap-" + str(int(now))
    status = (_text(root, "status") or "actual").lower()
    sent = _timestamp(_text(root, "sent")) or now
    records: list[dict] = []
    infos = [element for element in root.iter()
             if element.tag.split("}")[-1] == "info"]
    for index, info in enumerate(infos):
        event = _text(info, "event")
        severity = (_text(info, "severity") or "unknown").lower()
        urgency = (_text(info, "urgency") or "unknown").lower()
        certainty = (_text(info, "certainty") or "unknown").lower()
        effective = _timestamp(_text(info, "effective")) or sent
        expires = _timestamp(_text(info, "expires"))
        for area_index, area in enumerate(
                element for element in info.iter()
                if element.tag.split("}")[-1] == "area"):
            geometry, point = _geometry(area)
            if not point:
                continue
            uid = "CAP-{}-{}".format(identifier, index * 100 + area_index)
            record = {
                "_ts": now,
                "_src": "cap",
                "uid": uid[:160],
                "lat_deg": round(point[0], 7),
                "lon_deg": round(point[1], 7),
                "source_kind": "cap_alert",
                "cap_identifier": identifier[:160],
                "cap_status": status,
                "cap_event": event[:256],
                "cap_severity": severity,
                "cap_urgency": urgency,
                "cap_certainty": certainty,
                "cap_headline": _text(info, "headline")[:512],
                "cap_description": _text(info, "description")[:1024],
                "cap_sent": sent,
                "cap_effective": effective,
                "cap_expires": expires,
                "cap_active": effective <= now and (expires is None or now <= expires),
            }
            if geometry:
                record["geometry"] = geometry
                if geometry.get("type") == "Circle":
                    record["radius_km"] = geometry["radius_km"]
                record["geometry_json"] = json.dumps(geometry, separators=(",", ":"))
            record["identifier"] = record["cap_identifier"]
            record["status"] = record["cap_status"]
            record["severity"] = record["cap_severity"]
            record["event"] = record["cap_event"]
            record["headline"] = record["cap_headline"]
            record["description"] = record["cap_description"]
            record["effective"] = record["cap_effective"]
            if expires is not None:
                record["expires"] = expires
            records.append(record)
    return records


def run(args) -> None:
    session = zenoh.open(make_config())

    def on_sample(sample) -> None:
        try:
            for record in parse_cap(payload_bytes(sample)):
                if args.active_only and not record.get("cap_active"):
                    continue
                publish_dual(session, OUTPUT_TOPIC, record, CapAlert, zenoh)
        except Exception as exc:  # malformed partner data must not kill the translator
            print("CAP decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("CAP translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CAP 1.2 XML on Zenoh -> EFDI alerts")
    parser.add_argument("--active-only", action=argparse.BooleanOptionalAction,
                        default=os.environ.get("CAP_ACTIVE_ONLY", "1") not in {"0", "false", "no"})
    run(parser.parse_args())


if __name__ == "__main__":
    main()
