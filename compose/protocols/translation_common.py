"""Small shared helpers for Zenoh-native protocol translators.

The live EFDI data plane remains JSON.  These helpers deliberately keep the
transport envelope independent from the protocol payload so a receiver bridge
can publish bytes and a decoder can be replaced without changing the source.
"""

from __future__ import annotations

import json
import os
import time

import zenoh

from namespace_prefix import prefix


ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG
HERE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")


def make_config() -> "zenoh.Config":
    config = zenoh.Config()
    config.insert_json5("mode", '"client"')
    config.insert_json5("connect/endpoints", json.dumps([ENDPOINT]))
    if ENDPOINT.startswith("tls"):
        config.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return config


def payload_bytes(sample) -> bytes:
    payload = getattr(sample, "payload", sample)
    if hasattr(payload, "to_bytes"):
        return payload.to_bytes()
    return bytes(payload)


def payload_json(sample) -> object:
    return json.loads(payload_bytes(sample).decode("utf-8"))


def put_json(publisher, record: dict) -> None:
    publisher.put(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode(),
                  encoding=zenoh.Encoding.APPLICATION_JSON)


def base_record(source: str, uid: str, **fields) -> dict:
    record = {"_ts": time.time(), "_src": source, "uid": uid}
    record.update(fields)
    return record

