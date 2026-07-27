#!/usr/bin/env python3
"""DJI Cloud API MQTT 5 bridge -> normalized EFDI aircraft tracks.

This is a subscriber for an already configured DJI Cloud API broker. It is not
a DJI Fly scraper and does not make consumer Mavic aircraft expose telemetry.
DJI Pilot 2 or a DJI Dock must first be enrolled with the partner's Cloud API
deployment and publish ``thing/product/{aircraft-sn}/osd`` messages.

Official protocol references:
https://developer.dji.com/doc/cloud-api-tutorial/en/overview/basic-concept/mqtt.html
https://developer.dji.com/doc/cloud-api-tutorial/en/api-reference/pilot-to-cloud/mqtt/aircraft/properties.html
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import ssl
import time

import zenoh
from namespace_prefix import topic_root
from zenoh_auth import apply_zenoh_auth

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

ZENOH_TOPIC = "{}/air/dji/telemetry/friendly/uav/tracks/v1".format(TOPIC_ROOT)
MAX_MQTT_PAYLOAD = 1_048_576
_OSD_TOPIC = re.compile(r"(?:^|/)thing/product/([A-Za-z0-9._:-]{1,128})/osd$")
_AIRCRAFT_HINTS = frozenset(
    {
        "height", "elevation", "horizontal_speed", "vertical_speed",
        "attitude_head", "attitude_pitch", "attitude_roll", "mode_code",
    }
)


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5(
            "transport/link/tls",
            json.dumps(
                {
                    "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
                    "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
                    "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
                    "enable_mtls": True,
                    "verify_name_on_connect": True,
                }
            ),
        )
    return conf


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value) -> float:
    result = _number(value)
    if result is None:
        return time.time()
    if result > 10_000_000_000:
        result /= 1000.0
    return result


def decode_osd(topic: str, payload: bytes) -> dict | None:
    """Decode one bounded DJI aircraft OSD message; reject dock/RC positions."""
    match = _OSD_TOPIC.search(topic)
    if not match or not payload or len(payload) > MAX_MQTT_PAYLOAD:
        return None
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    data = document.get("data")
    if not isinstance(data, dict) or not _AIRCRAFT_HINTS.intersection(data):
        return None
    lat = _number(data.get("latitude"))
    lon = _number(data.get("longitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    serial = match.group(1)
    track = {
        "_src": "DJI Cloud API",
        "_ts": _timestamp(document.get("timestamp")),
        "uid": "dji-cloud-{}".format(serial),
        "callsign": "DJI-{}".format(serial[-8:]),
        "dji_aircraft_sn": serial,
        "classification": "DJI UAV",
        "lat_deg": round(lat, 7),
        "lon_deg": round(lon, 7),
    }
    absolute_height = _number(data.get("height"))
    relative_height = _number(data.get("elevation"))
    horizontal_speed = _number(data.get("horizontal_speed"))
    vertical_speed = _number(data.get("vertical_speed"))
    heading = _number(data.get("attitude_head"))
    if absolute_height is not None:
        track["geo_alt_m"] = round(absolute_height, 2)
    if relative_height is not None:
        track["height_m"] = round(relative_height, 2)
    if horizontal_speed is not None:
        track["speed_ms"] = round(horizontal_speed, 2)
    if vertical_speed is not None:
        track["vertical_rate_ms"] = round(vertical_speed, 2)
    if heading is not None:
        track["heading_deg"] = round(heading % 360.0, 2)

    safe_fields = (
        "attitude_pitch", "attitude_roll", "mode_code", "mode_code_reason",
        "gear", "home_distance", "home_latitude", "home_longitude",
        "total_flight_time", "total_flight_distance", "total_flight_sorties",
        "wind_speed", "wind_direction", "control_source",
    )
    for key in safe_fields:
        value = data.get(key)
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 128:
            track["dji_" + key] = value
    return track


def run(args) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise SystemExit("paho-mqtt is required; install compose/requirements.txt") from exc

    session = zenoh.open(make_config())
    publisher = session.declare_publisher(ZENOH_TOPIC)
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
        protocol=mqtt.MQTTv5,
    )
    if args.username:
        client.username_pw_set(args.username, os.environ.get("DJI_MQTT_PASSWORD", ""))
    if args.tls:
        client.tls_set(
            ca_certs=os.environ.get("DJI_MQTT_CA") or None,
            certfile=os.environ.get("DJI_MQTT_CERT") or None,
            keyfile=os.environ.get("DJI_MQTT_KEY") or None,
            cert_reqs=ssl.CERT_REQUIRED,
        )

    def on_connect(mqtt_client, _userdata, _flags, reason_code, _properties):
        if reason_code.is_failure:
            print("DJI MQTT connection rejected: {}".format(reason_code), flush=True)
            return
        mqtt_client.subscribe(args.topic, qos=1)
        print("DJI MQTT subscribed:", args.topic, flush=True)

    def on_message(_client, _userdata, message):
        track = decode_osd(message.topic, message.payload)
        if track is None:
            if args.verbose:
                print("DJI ignored non-aircraft/invalid OSD:", message.topic, flush=True)
            return
        publisher.put(
            json.dumps(track, separators=(",", ":")).encode(),
            encoding=zenoh.Encoding.APPLICATION_JSON,
        )
        if args.verbose:
            print("DJI", track["callsign"], track["lat_deg"], track["lon_deg"], flush=True)

    client.on_connect = on_connect
    client.on_message = on_message
    print("DJI Cloud API broker: {}:{} TLS={}".format(args.host, args.port, args.tls), flush=True)
    print("Zenoh topic:", ZENOH_TOPIC, flush=True)
    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        publisher.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="DJI Cloud API MQTT 5 OSD -> Zenoh")
    parser.add_argument("--host", default=os.environ.get("DJI_MQTT_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DJI_MQTT_PORT", "8883")))
    parser.add_argument("--topic", default=os.environ.get("DJI_MQTT_TOPIC", "thing/product/+/osd"))
    parser.add_argument("--username", default=os.environ.get("DJI_MQTT_USERNAME", ""))
    parser.add_argument("--client-id", default=os.environ.get("DJI_MQTT_CLIENT_ID", "efdi-dji-reader"))
    parser.add_argument(
        "--tls", action=argparse.BooleanOptionalAction,
        default=os.environ.get("DJI_MQTT_TLS", "1") != "0",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not args.host:
        parser.error("--host or DJI_MQTT_HOST is required")
    if not 1 <= args.port <= 65535:
        parser.error("MQTT port must be between 1 and 65535")
    run(args)


if __name__ == "__main__":
    main()
