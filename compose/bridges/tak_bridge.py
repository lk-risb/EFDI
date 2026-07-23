#!/usr/bin/env python3
"""TAK CoT ingress → Zenoh.

This bridge is the reverse side of the CoT output path: it consumes
Cursor-on-Target XML from a TAK-visible TCP feed, normalizes the event into the
same JSON track model used by the rest of the EFDI fabric, and republishes it to
Zenoh.

The bridge is intentionally conservative:
- it publishes the raw CoT XML on a raw topic for future decoders
- it republishes a normalized JSON track when the event carries a point
- it tags the record as TAK ingress so the CoT output layer can avoid echoing it
  straight back into the same TAK connection

The CoT feed is assumed to be line- or stream-delimited XML over a TAK Server
TCP connection or a forwarded TAK-visible socket.  If the feed is absent, the
bridge reconnects and the downstream Zenoh-native bridges keep operating from
their own direct sources.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import zenoh

from namespace_prefix import topic_root
from protocols.random.normalized_track_pb2 import NormalizedTrack
from protocols.protobuf_codec import publish_dual
from protocols.translation_common import base_record, make_config

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))

_TAK_SOURCE = os.environ.get("TAK_COT_SOURCE", "tak").strip() or "tak"
_TAK_PROTOCOL = os.environ.get("TAK_COT_PROTOCOL", "cot").strip() or "cot"
_TAK_DEFAULT_DOMAIN = os.environ.get("TAK_COT_DEFAULT_DOMAIN", "air").strip() or "air"
_TAK_DEFAULT_ENTITY = os.environ.get("TAK_COT_DEFAULT_ENTITY", "uav").strip() or "uav"
_RECONNECT_S = float(os.environ.get("TAK_COT_RECONNECT_S", "10"))
_READ_CHUNK = int(os.environ.get("TAK_COT_READ_CHUNK", "65536"))
_RAW_TOPIC = "{}/raw/tak/cot".format(TOPIC_ROOT)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    for item in parent:
        if _local_name(item.tag) == name:
            return item
    return None


def _text(value: str | None, limit: int = 256) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _to_float(value: str | None) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts.timestamp()


def _describe_detail(detail: ET.Element | None) -> dict[str, object]:
    if detail is None:
        return {}
    out: dict[str, object] = {}
    links: list[dict[str, str]] = []
    for child in detail:
        name = _local_name(child.tag)
        attrs = {k: v for k, v in child.attrib.items() if v}
        text = _text(child.text)
        if name == "contact":
            if attrs.get("callsign"):
                out["callsign"] = attrs["callsign"]
            if attrs.get("endpoint"):
                out["endpoint"] = attrs["endpoint"]
            if attrs.get("device"):
                out["tak_device"] = attrs["device"]
            if attrs.get("platform"):
                out["tak_platform"] = attrs["platform"]
        elif name == "takv":
            for key in ("platform", "device", "version", "os"):
                if attrs.get(key):
                    out[f"tak_{key}"] = attrs[key]
        elif name == "track":
            for key in ("course", "speed", "bearing", "pitch", "roll", "sin", "angle"):
                if attrs.get(key):
                    out[f"cot_{key}"] = attrs[key]
        elif name == "remarks" and text:
            out["remarks"] = text
        elif name == "link":
            link = {k: v for k, v in attrs.items() if v}
            if text:
                link["text"] = text
            if link:
                links.append(link)
        elif text or attrs:
            out[f"detail_{name}"] = attrs if attrs else text
    if links:
        out["links"] = links
    return out


def _affiliation_from_cot(cot_type: str) -> str:
    parts = cot_type.split("-")
    if len(parts) >= 2:
        code = parts[1].lower()
        if code in {"f", "h", "n", "u"}:
            return {
                "f": "friendly",
                "h": "hostile",
                "n": "neutral",
                "u": "unknown",
            }[code]
    return "unknown"


def _domain_from_cot(cot_type: str) -> str:
    parts = cot_type.split("-")
    if len(parts) >= 3:
        code = parts[2].upper()
        if code == "A":
            return "air"
        if code == "G":
            return "land"
        if code == "S":
            return "sea"
        if code == "P":
            return "space"
    return _TAK_DEFAULT_DOMAIN


def _entity_from_cot(domain: str, cot_type: str, detail: dict[str, object]) -> str:
    cot_upper = cot_type.upper()
    if domain == "air":
        if "Q" in cot_upper or "UAV" in cot_upper or "UAV" in json.dumps(detail).upper():
            return "uav"
        return "aircraft"
    if domain == "land":
        if "-E-V" in cot_upper or "VEHICLE" in json.dumps(detail).upper():
            return "vehicle"
        if "-E-S" in cot_upper or "SENSOR" in json.dumps(detail).upper():
            return "sensor"
        return "unit"
    if domain == "sea":
        return "vessel"
    if domain == "space":
        return "satellite"
    return "track"


def _topic_for_record(record: dict[str, object]) -> str:
    cot_type = str(record.get("cot_type") or "")
    domain = _domain_from_cot(cot_type)
    affiliation = _affiliation_from_cot(cot_type)
    entity = _entity_from_cot(domain, cot_type, record.get("cot_detail", {}) if isinstance(record.get("cot_detail"), dict) else {})
    return "{}/{}/{}/{}/{}/{}".format(
        TOPIC_ROOT, domain, _TAK_SOURCE, _TAK_PROTOCOL, affiliation, entity
    )


def _normalize_event(event: ET.Element) -> tuple[str, dict[str, object]] | None:
    uid = _text(event.get("uid")) or _text(event.get("UID")) or _text(event.get("id"))
    cot_type = _text(event.get("type")) or ""
    how = _text(event.get("how"))
    cot_time = _text(event.get("time"))
    cot_start = _text(event.get("start"))
    cot_stale = _text(event.get("stale"))

    point = _child(event, "point")
    if point is None and uid is None and not cot_type:
        return None

    detail = _child(event, "detail")
    detail_map = _describe_detail(detail)

    record = base_record(
        _TAK_SOURCE,
        uid or hashlib.sha1(ET.tostring(event, encoding="utf-8")).hexdigest()[:16],
        _ingress="tak_server",
        cot_type=cot_type or None,
        cot_how=how or None,
        cot_time=cot_time,
        cot_start=cot_start,
        cot_stale=cot_stale,
        cot_detail=detail_map if detail_map else None,
    )

    if cot_time:
        record["cot_time_epoch_s"] = _parse_time(cot_time)
    if cot_start:
        record["cot_start_epoch_s"] = _parse_time(cot_start)
    if cot_stale:
        stale_epoch = _parse_time(cot_stale)
        if stale_epoch is not None:
            record["cot_stale_epoch_s"] = stale_epoch
            now = time.time()
            record["stale_s"] = max(1.0, stale_epoch - now)

    if point is not None:
        lat = _to_float(point.get("lat"))
        lon = _to_float(point.get("lon"))
        if lat is not None and lon is not None:
            record["lat_deg"] = lat
            record["lon_deg"] = lon
            record["lat"] = lat
            record["lon"] = lon
        hae = _to_float(point.get("hae"))
        if hae is not None:
            record["alt_m"] = hae
        ce = _to_float(point.get("ce"))
        le = _to_float(point.get("le"))
        if ce is not None:
            record["ce_m"] = ce
        if le is not None:
            record["le_m"] = le

    track = _child(detail, "track")
    if track is not None:
        course = _to_float(track.get("course") or track.get("bearing"))
        speed = _to_float(track.get("speed"))
        if course is not None:
            record["heading_deg"] = course
        if speed is not None:
            record["speed_ms"] = speed

    domain = _domain_from_cot(cot_type)
    affiliation = _affiliation_from_cot(cot_type)
    entity = _entity_from_cot(domain, cot_type, detail_map)
    record["cot_domain"] = domain
    record["cot_affiliation"] = affiliation
    record["cot_entity"] = entity

    topic = _topic_for_record(record)
    return topic, record


def _connect(host: str, port: int, tls: bool, certfile: str | None, keyfile: str | None, cafile: str | None) -> socket.socket:
    raw = socket.create_connection((host, port), timeout=30)
    raw.settimeout(60.0)
    if not tls:
        return raw
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    if cafile:
        ctx.load_verify_locations(cafile)
    if certfile and keyfile:
        ctx.load_cert_chain(certfile, keyfile)
    return ctx.wrap_socket(raw, server_hostname=host)


def _raw_topic_message(xml: str) -> bytes:
    return xml.encode("utf-8", errors="replace")


def _extract_events(buffer: list[str]) -> list[str]:
    """Extract complete CoT <event> frames from a rolling text buffer."""

    text = buffer[0]
    frames: list[str] = []
    while True:
        start = text.find("<event")
        if start < 0:
            buffer[0] = text[-64:]
            return frames
        end = text.find("</event>", start)
        if end < 0:
            buffer[0] = text[start:]
            return frames
        frames.append(text[start:end + len("</event>")])
        text = text[end + len("</event>"):]
        buffer[0] = text


def run(args) -> None:
    hosts = [h for h in args.host if h]
    if not hosts:
        env_hosts = [
            os.environ.get("TAK_HOST", "").strip(),
            os.environ.get("TAK_HOST_FALLBACK", "").strip(),
        ]
        hosts = [h for h in env_hosts if h]
    if not hosts:
        raise SystemExit("Set TAK_HOST or pass one or more --host values")

    session = zenoh.open(make_config())
    raw_pub = session.declare_publisher(_RAW_TOPIC)

    print("TAK CoT ingress bridge started", flush=True)
    print("  Hosts  : {}".format(", ".join("{}:{}".format(h, args.port) for h in hosts)), flush=True)
    print("  TLS    : {}".format("on" if args.tls else "off"), flush=True)
    print("  Raw    : {}".format(_RAW_TOPIC), flush=True)

    idx = 0
    while True:
        host = hosts[idx % len(hosts)]
        idx += 1
        sock = None
        try:
            sock = _connect(host, args.port, args.tls, args.cert, args.key, args.ca)
            mode = "mTLS" if args.tls else "TCP"
            print("TAK {} connected → {}:{}".format(mode, host, args.port), flush=True)
            text_buffer = [""]
            while True:
                chunk = sock.recv(_READ_CHUNK)
                if not chunk:
                    break
                text_buffer[0] += chunk.decode("utf-8", errors="replace")
                for xml in _extract_events(text_buffer):
                    try:
                        elem = ET.fromstring(xml)
                    except ET.ParseError:
                        continue
                    if _local_name(elem.tag) != "event":
                        continue
                    raw_pub.put(_raw_topic_message(xml), encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                    normalized = _normalize_event(elem)
                    if normalized is not None:
                        topic, record = normalized
                        publish_dual(session, topic, record, NormalizedTrack, zenoh)
                        if args.verbose:
                            print("TAK PUB {} uid={} type={}".format(
                                topic, record.get("uid"), record.get("cot_type", "")), flush=True)
                    elem.clear()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("TAK ingest error on {}:{} — {} — retry in {}s".format(
                host, args.port, exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    raw_pub.undeclare()
    session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="TAK CoT → Zenoh bridge")
    parser.add_argument("--host", action="append", default=[], help="TAK host / IP (repeatable; fallback path supported)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TAK_PORT", "8087")))
    parser.add_argument("--tls", action="store_true", default=os.environ.get("TAK_TLS", "0") == "1")
    parser.add_argument("--cert", default=os.environ.get("TAK_CERT"))
    parser.add_argument("--key", default=os.environ.get("TAK_KEY"))
    parser.add_argument("--ca", default=os.environ.get("TAK_CA"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
