#!/usr/bin/env python3
"""first-subscriber.py — org "<YOUR_NAMESPACE>" → EFDI Fabric.

Self-validation companion to first-publisher.py. Run this in one shell
and first-publisher.py in another; you'll see your own publishes echo
back. By default subscribes to your whole org subtree ("<YOUR_NAMESPACE>/**");
pass a key expression as the first arg to narrow.

Install:
    python3 -m venv venv && . venv/bin/activate
    pip install eclipse-zenoh
Run:
    . venv/bin/activate
    python3 first-subscriber.py                       # <YOUR_NAMESPACE>/**
    python3 first-subscriber.py "<YOUR_NAMESPACE>/hello/v1"         # one topic
    python3 first-subscriber.py "**"                  # everything on fabric

Windows note: use "python" instead of "python3"; activate the venv
with "venv\\Scripts\\activate" instead of ". venv/bin/activate".

Each sample prints as one line:
    HH:MM:SS  <key>  <text-or-hex-preview>

Press Ctrl-C to exit cleanly.
"""

import json
import os
import sys
import time

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"       # mesh-internal endpoint; from the OOB bundle
ORG = os.environ.get("PARTNER_NAMESPACE", "")       # your org prefix
HERE = os.path.dirname(os.path.abspath(__file__))


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([ROUTER]))
    # Same whole-block insert as first-publisher.py — per-sub-key
    # inserts do NOT enable the client-cert send path on eclipse-zenoh-python 1.x.
    conf.insert_json5("transport/link/tls", json.dumps({
        "root_ca_certificate": os.path.join(HERE, "efdi-ca-root.pem"),
        "connect_certificate": os.path.join(HERE, ORG + "-cert.pem"),
        "connect_private_key": os.path.join(HERE, ORG + "-key.pem"),
        "enable_mtls": True,
        "verify_name_on_connect": True,
    }))
    return conf


def render_payload(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and all(c.isprintable() or c in "\r\n\t" for c in text):
        return "text({}B): {!r}".format(len(raw), text)
    return "bytes({}B) hex[:64]={}".format(len(raw), raw[:64].hex())


def on_sample(sample: "zenoh.Sample") -> None:
    ts = time.strftime("%H:%M:%S")
    key = str(sample.key_expr)
    raw = bytes(sample.payload)
    print("{}  {}  {}".format(ts, key, render_payload(raw)), flush=True)


def main() -> None:
    key_expr = sys.argv[1] if len(sys.argv) > 1 else ORG + "/**"

    print("connecting to {} ...".format(ROUTER), flush=True)
    session = zenoh.open(make_config())
    sub = session.declare_subscriber(key_expr, on_sample)
    print("subscribed: {}".format(key_expr), flush=True)
    print("waiting for samples (Ctrl-C to exit)", flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nshutting down ...", flush=True)
    finally:
        sub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
