#!/usr/bin/env python3
"""Oro navigacija UTM flight feed -> normalized Zenoh UAV tracks.

``utm.ans.lt`` is a UTM platform for declared/planned Lithuanian drone
flights. It is not a public ASTM F3411/OpenDroneID receiver network. This
bridge therefore accepts only an explicitly configured, authorized JSON or
GeoJSON export/API URL supplied by Oro navigacija or the deployment owner; it
does not scrape the public map or guess private endpoints.

Zenoh output:
  <PREFIX>/<ORG>/air/utm_ans/c2/unknown/uav/json/tracks

The record is marked ``source_kind=declared_utm_flight`` and never claims that
the aircraft was observed by Remote ID. Actual broadcast Remote ID remains the
responsibility of ``protocols/random/opendroneid.py`` and a receiver publishing raw
messages into Zenoh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import ssl
import time
import urllib.error
import urllib.request

import zenoh
from http_json import read_json_response
from namespace_prefix import topic_root
from zenoh_auth import apply_zenoh_auth

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

TOPIC_UAV = "{}/air/utm_ans/c2/unknown/uav/tracks/v1".format(TOPIC_ROOT)
SOURCE = "utm_ans"
MAX_TEXT = 256
MAX_RESPONSE_BYTES = 10_000_000


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


def _number(value, cast=float):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = cast(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(result, float) and not math.isfinite(result):
        return None
    return result


def _text(value, limit: int = MAX_TEXT) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    value = " ".join(str(value).split())
    return value[:limit]


def _get(record: dict, *paths):
    for path in paths:
        current = record
        try:
            for part in path.split("."):
                if not isinstance(current, dict):
                    raise KeyError(part)
                current = current[part]
        except (KeyError, TypeError):
            continue
        if current is not None:
            return current
    return None


def _records(payload) -> list[dict]:
    """Extract bounded flight objects from common JSON/GeoJSON wrappers."""
    if isinstance(payload, list):
        result = []
        for item in payload:
            if isinstance(item, dict) and item.get("type") == "Feature":
                result.extend(_records(item))
            elif isinstance(item, dict):
                result.append(item)
        return result
    if not isinstance(payload, dict):
        return []
    if payload.get("type") == "Feature" and isinstance(payload.get("properties"), dict):
        result = dict(payload["properties"])
        geometry = payload.get("geometry")
        if isinstance(geometry, dict):
            result["geometry"] = geometry
        return [result]
    for key in ("flights", "flight_plans", "flightPlans", "tracks", "data", "items", "results", "features"):
        value = payload.get(key)
        if isinstance(value, list):
            return _records(value)
    return [payload]


def _position(record: dict) -> tuple[float, float] | None:
    geometry = record.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type") == "Point":
        coordinates = geometry.get("coordinates")
        if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
            lon = _number(coordinates[0])
            lat = _number(coordinates[1])
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon

    lat = _number(_get(record, "lat", "latitude", "lat_deg", "position.lat", "position.latitude"))
    lon = _number(_get(record, "lon", "longitude", "lon_deg", "position.lon", "position.longitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _stable_id(record: dict, position: tuple[float, float]) -> str:
    raw = _get(
        record,
        "flight_id", "flightId", "operation_id", "operationId", "id", "uuid",
        "uas_id", "uasId", "remote_id", "remoteId", "serial_number", "serialNumber",
        "registration", "callsign",
    )
    value = _text(raw, 96)
    if value:
        safe = "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in value)
        return safe[:96]
    digest = hashlib.sha256(
        "{:.6f},{:.6f}".format(position[0], position[1]).encode("ascii")
    ).hexdigest()[:20]
    return "position-" + digest


def normalize(record: dict, now: float | None = None) -> dict | None:
    """Normalize one UTM declared-flight object; omit records without position."""
    if not isinstance(record, dict):
        return None
    position = _position(record)
    if position is None:
        return None
    lat, lon = position
    now = time.time() if now is None else float(now)
    flight_id = _stable_id(record, position)
    track = {
        "_ts": now,
        "_src": SOURCE,
        "uid": "UTM-ANS-" + flight_id,
        "lat_deg": round(lat, 7),
        "lon_deg": round(lon, 7),
        "utm_flight_id": flight_id,
        "utm_vehicle_type": "uav",
        "source_kind": "declared_utm_flight",
        "utm_remote_id_observed": False,
    }

    for target, paths in {
        "alt_m": ("altitude_m", "height_m", "altitude", "height", "position.altitude"),
        "heading_deg": ("heading_deg", "heading", "course_deg", "course", "track"),
        "speed_ms": ("speed_ms", "speed", "velocity_ms"),
    }.items():
        value = _number(_get(record, *paths))
        if value is not None and math.isfinite(value):
            track[target] = value
    feet = _number(_get(record, "altitude_ft", "height_ft", "position.altitude_ft"))
    if "alt_m" not in track and feet is not None:
        track["alt_m"] = round(feet * 0.3048, 2)
    knots = _number(_get(record, "speed_kt", "speed_kts", "ground_speed_kts"))
    if "speed_ms" not in track and knots is not None:
        track["speed_ms"] = round(knots * 0.514444, 2)
    if "heading_deg" in track:
        track["heading_deg"] %= 360.0
    if "speed_ms" in track and track["speed_ms"] < 0:
        track.pop("speed_ms")

    for target, paths in {
        "callsign": ("callsign", "flight_name", "name"),
        "registration": ("registration", "uas_registration", "aircraft_registration"),
        "utm_status": ("status", "flight_status", "operation_status"),
        "utm_authorization_status": ("authorization_status", "approval_status", "permit_status"),
        "utm_planned_start": ("planned_start", "start_time", "startTime"),
        "utm_planned_end": ("planned_end", "end_time", "endTime"),
    }.items():
        value = _text(_get(record, *paths))
        if value:
            track[target] = value

    uas_id = _text(_get(record, "uas_id", "uasId", "remote_id", "remoteId", "serial_number", "serialNumber"), 96)
    if uas_id:
        track["utm_uas_id"] = uas_id
    return track


def _ssl_context(verify: bool) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def fetch(url: str, token: str, verify_tls: bool):
    headers = {
        "Accept": "application/json, application/geo+json",
        "User-Agent": "efdi-utm-ans-bridge/1.0",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20, context=_ssl_context(verify_tls)) as response:
        return read_json_response(response, max_bytes=MAX_RESPONSE_BYTES)


def run(args) -> None:
    if not args.url:
        raise SystemExit("UTM_ANS_API_URL is required; obtain an authorized JSON/GeoJSON export URL")
    if not args.url.lower().startswith("https://"):
        raise SystemExit("UTM_ANS_API_URL must use https://")
    if args.interval < 5:
        raise SystemExit("--interval must be at least 5 seconds")

    session = zenoh.open(make_config())
    publisher = session.declare_publisher(TOPIC_UAV)
    print("UTM.ans declared UAV bridge: {} -> {}".format(args.url, TOPIC_UAV), flush=True)
    try:
        while True:
            try:
                payload = fetch(args.url, args.token, args.verify_tls)
                tracks = [normalize(item) for item in _records(payload)]
                tracks = [track for track in tracks if track is not None]
                for track in tracks:
                    publisher.put(
                        json.dumps(track, separators=(",", ":")).encode(),
                        encoding=zenoh.Encoding.APPLICATION_JSON,
                    )
                print("UTM.ans declared UAV tracks: {}".format(len(tracks)), flush=True)
                if args.once:
                    return
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                print("UTM.ans fetch error: {}".format(exc), flush=True)
                if args.once:
                    raise SystemExit(1) from exc
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        publisher.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorized Oro navigacija UTM JSON/GeoJSON -> Zenoh UAV bridge")
    parser.add_argument("--url", default=os.environ.get("UTM_ANS_API_URL", ""))
    parser.add_argument("--token", default=os.environ.get("UTM_ANS_API_TOKEN", ""))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("UTM_ANS_POLL_S", "15")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--verify-tls", action=argparse.BooleanOptionalAction,
        default=os.environ.get("UTM_ANS_TLS_VERIFY", "1") not in {"0", "false", "no"},
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
