#!/usr/bin/env python3
"""backbone_layer.py — EFDI tracks -> EFDI Backbone trial fabric.

Symmetric counterpart to bridges/backbone_bridge.py (which brings the
fabric's data IN). This one sends this pod's own tracks OUT, the same way
tak_layer.py/sitaware_layer.py send them out to their own C2 systems:
subscribe to this pod's own track topics, translate, put to the far side.

Holds its own direct mTLS session to the real backbone router — same
identity, same reasoning, same identity directory as backbone_bridge.py
(${POD_STATE_DIR}/zenoh-backbone/tls/{cert,key,ca-roots}.pem; see that
file's module docstring for why this doesn't go through
zenoh-router-backbone's router-to-router relay).

The fabric's own wire-layer ACL only accepts writes under the prefix
matching our uploaded identity's certificate — read directly off that same
certificate's CN (confirmed live: CN and the URI SAN's "org=" both carry
this pod's assigned vendor id, e.g. "f2121acad40b27c52728fa461b63a5d7"), so
there is nothing to configure for this: whatever identity is uploaded is
whatever prefix this pod is allowed to publish under.

Publishes each local track under "<vendor_prefix>/efdi/<suffix>", where
<suffix> is this track's own key with TOPIC_ROOT stripped (so a remote
participant sees this pod's own land/air/sea/... structure, unmodified) —
and forwards the exact JSON payload as-is; there's no shared schema on this
trial fabric to translate into (see backbone_bridge.py's docstring: every
participant publishes its own shape).

Never re-exports what backbone_bridge.py itself just imported — that would
hand another participant's own track back to them relabeled as ours. Two
independent guards catch it: the key itself (anything already under
"land/backbone/**") and the payload's "_src" field (backbone_bridge.py
always tags translated tracks "backbone:<origin>").
"""

import argparse
import json
import os
import time

import zenoh
from cryptography import x509
from cryptography.x509.oid import NameOID
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, payload_json, subscribe

_POD_STATE_DIR = os.environ.get("POD_STATE_DIR", "/root/efdi-router/compose/state")
_BACKBONE_TLS_DIR = os.path.join(_POD_STATE_DIR, "zenoh-backbone", "tls")


def _backbone_endpoints() -> list[str]:
    raw = os.environ.get("EFDI_BACKBONE_FABRIC_ENDPOINTS", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    return [str(parsed)]


def _vendor_prefix(cert_path: str) -> str | None:
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except Exception:
        return None


def _open_backbone_session():
    """Direct mTLS session to the real backbone fabric. Retries forever —
    the identity may not be uploaded yet, or the fabric endpoint may be
    temporarily unreachable."""
    cert = os.path.join(_BACKBONE_TLS_DIR, "cert.pem")
    key = os.path.join(_BACKBONE_TLS_DIR, "key.pem")
    ca = os.path.join(_BACKBONE_TLS_DIR, "ca-roots.pem")
    while True:
        endpoints = _backbone_endpoints()
        missing = [p for p in (cert, key, ca) if not os.path.isfile(p)]
        vendor_prefix = None if missing else _vendor_prefix(cert)
        if not endpoints or missing or not vendor_prefix:
            print(
                "backbone layer: no backbone identity/endpoint configured yet "
                "(missing {}) — upload one via the Certificates page. Retry in 15s"
                .format(missing or ("EFDI_BACKBONE_FABRIC_ENDPOINTS" if not endpoints else "a readable vendor CN")),
                flush=True,
            )
            time.sleep(15)
            continue
        conf = zenoh.Config()
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", json.dumps(endpoints))
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": ca,
            "connect_certificate": cert,
            "connect_private_key": key,
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
        try:
            return zenoh.open(conf), vendor_prefix
        except Exception as exc:
            print("backbone layer: backbone connect failed: {} — retry in 15s".format(exc), flush=True)
            time.sleep(15)


def _handle(backbone_session, vendor_prefix: str, sample, verbose: bool):
    key = str(sample.key_expr)
    suffix = key[len(TOPIC_ROOT) + 1:] if key.startswith(TOPIC_ROOT + "/") else key
    if "/backbone/" in suffix or suffix.startswith("raw/") or "/@" in key:
        return  # our own re-export of something we imported, an unclaimed raw sample, or an internal control topic
    try:
        obj = payload_json(sample)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(obj, dict):
        return
    if obj.get("lat_deg") is None or obj.get("lon_deg") is None:
        return
    if str(obj.get("_src", "")).startswith("backbone:"):
        return

    topic = "{}/efdi/{}".format(vendor_prefix, suffix)
    backbone_session.put(topic, payload_bytes(sample), encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        print("EFDI track {} -> {}".format(key, topic), flush=True)


def main():
    ap = argparse.ArgumentParser(description="EFDI tracks -> EFDI Backbone trial fabric")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            local_session = open_session()
            break
        except Exception as exc:
            print("backbone layer: local Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    backbone_session, vendor_prefix = _open_backbone_session()
    print("backbone layer starting — sending EFDI tracks to the backbone as {}/efdi/**".format(vendor_prefix), flush=True)
    sub = subscribe(local_session, TOPIC_ROOT + "/**", lambda sample: _handle(backbone_session, vendor_prefix, sample, args.verbose))

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sub.undeclare()
        local_session.close()
        backbone_session.close()


if __name__ == "__main__":
    main()
