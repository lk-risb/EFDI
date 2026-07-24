#!/usr/bin/env python3
"""MQTT broker ingress -> Zenoh raw payloads.

MQTT is a transport, not a data format: the broker carries whatever JSON a
vendor chose to publish. This bridge therefore decodes nothing. It subscribes
to the configured broker topics and republishes each payload verbatim under the
fabric's raw namespace, where a normalizer turns it into canonical records
(protocols/random/mqtt_json.py handles the documented minimal contract).

The MQTT topic is preserved in the Zenoh key so a normalizer can tell feeds
apart: `mqtt/sensors/acoustic/7` arrives on `<root>/raw/mqtt/sensors/acoustic/7`.

Config (compose/.env):
  MQTT_HOST=broker.example            # required
  MQTT_PORT=1883                      # 8883 when MQTT_TLS=1
  MQTT_TOPIC=sensors/#                # comma-separated subscription filters
  MQTT_USER= / MQTT_PASS=             # optional broker credentials
  MQTT_TLS=1                          # optional TLS to the broker
  MQTT_CLIENT_ID=efdi-mqtt-bridge

Run:
  venv/bin/python3 bridges/mqtt_bridge.py
"""

from __future__ import annotations

import argparse
import json
import os
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
_RECONNECT_S = float(os.environ.get("MQTT_RECONNECT_S", "10"))
RAW_ROOT = "{}/raw/mqtt".format(TOPIC_ROOT)
MAX_PAYLOAD = int(os.environ.get("MQTT_MAX_PAYLOAD", "1048576"))


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


def _key_segment(value: str) -> str:
    """Make one MQTT topic level safe for a Zenoh key expression."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value) or "_"


def zenoh_key(mqtt_topic: str) -> str:
    """Map an MQTT topic onto a Zenoh key under the raw namespace."""
    levels = [_key_segment(level) for level in mqtt_topic.split("/") if level]
    return "/".join([RAW_ROOT] + levels) if levels else RAW_ROOT


def run(args) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise SystemExit("paho-mqtt is required; install compose/requirements.txt") from exc

    if not args.host:
        raise SystemExit("Set MQTT_HOST in .env or pass --host")

    while True:
        try:
            session = zenoh.open(make_config())
            break
        except Exception as exc:
            print("MQTT bridge Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    filters = [item.strip() for item in args.topic.split(",") if item.strip()]
    if not filters:
        raise SystemExit("Set MQTT_TOPIC (e.g. sensors/#) or pass --topic")

    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        if reason_code != 0:
            print("MQTT connect refused: {}".format(reason_code), flush=True)
            return
        for item in filters:
            client.subscribe(item, qos=args.qos)
            print("MQTT SUB {} -> {}/<topic>".format(item, RAW_ROOT), flush=True)

    def on_message(_client, _userdata, message):
        payload = bytes(message.payload)
        if len(payload) > MAX_PAYLOAD:
            print("MQTT payload dropped ({} bytes > {})".format(len(payload), MAX_PAYLOAD), flush=True)
            return
        key = zenoh_key(message.topic)
        session.put(key, payload, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
        if args.verbose:
            print("MQTT RAW {} -> {} ({} bytes)".format(message.topic, key, len(payload)), flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.client_id)
    if args.user:
        client.username_pw_set(args.user, args.password)
    if args.tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect
    client.on_message = on_message

    print("MQTT ingress: {}:{} -> {}".format(args.host, args.port, RAW_ROOT), flush=True)
    try:
        while True:
            try:
                client.connect(args.host, args.port, keepalive=60)
                client.loop_forever()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print("MQTT error: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
                time.sleep(_RECONNECT_S)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="MQTT broker ingress -> Zenoh raw payloads")
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", ""))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--topic", default=os.environ.get("MQTT_TOPIC", "sensors/#"),
                    help="comma-separated MQTT subscription filters")
    ap.add_argument("--user", default=os.environ.get("MQTT_USER", ""))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASS", ""))
    ap.add_argument("--tls", action="store_true", default=os.environ.get("MQTT_TLS", "") == "1")
    ap.add_argument("--qos", type=int, default=int(os.environ.get("MQTT_QOS", "0")))
    ap.add_argument("--client-id", default=os.environ.get("MQTT_CLIENT_ID", "efdi-mqtt-bridge"))
    ap.add_argument("--verbose", "-v", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
