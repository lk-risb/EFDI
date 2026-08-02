#!/usr/bin/env python3
"""first-publisher.py — org "<YOUR_NAMESPACE>" → EFDI Fabric.

A working starting point. Run this from the directory the tarball
unpacked into; the cert/key/root paths assume sibling files.

Pre-wired for the trial fabric's mTLS posture: enable_mtls=true,
verify_name_on_connect=true. The router endpoint is a DNS hostname
that resolves on the mesh CoreDNS resolver pushed by netbird-mgmt
and matches a DNS SAN on the router leaf cert, so standard TLS
hostname verification stays on (closes F-DOG-10).

Install:
    python3 -m venv venv && . venv/bin/activate
    pip install eclipse-zenoh protobuf
Run:
    . venv/bin/activate
    python3 first-publisher.py            # 5 publishes
    python3 first-publisher.py 50 0.2     # 50 publishes at 200ms intervals

Windows note: use "python" instead of "python3" (the latter bypasses
the venv on Windows and hits the system Python, causing ModuleNotFoundError).
Activate with: venv\\Scripts\\activate  (not venv/bin/activate).
"""

import json
import os
import sys
import time

import zenoh

ROUTER = os.environ.get("EFDI_ROUTER", "tls/zenoh.efdi.netbird.efdi-backbone.net:7447")  # mesh-internal endpoint; from the OOB bundle
ORG = os.environ.get("PARTNER_NAMESPACE", "")       # your org prefix
HERE = os.path.dirname(os.path.abspath(__file__))


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([ROUTER]))
    # Insert the whole TLS block at once. Per-sub-key inserts
    # (transport/link/tls/connect_certificate, etc.) do NOT enable the
    # client-cert mTLS send path on at least eclipse-zenoh-python 1.x.
    conf.insert_json5("transport/link/tls", json.dumps({
        "root_ca_certificate": os.path.join(HERE, "efdi-ca-root.pem"),
        "connect_certificate": os.path.join(HERE, ORG + "-cert.pem"),
        "connect_private_key": os.path.join(HERE, ORG + "-key.pem"),
        "enable_mtls": True,
        "verify_name_on_connect": True,
    }))
    return conf


def main() -> None:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    interval_s = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    session = zenoh.open(make_config())
    key = ORG + "/hello/v1"
    pub = session.declare_publisher(key)
    try:
        for i in range(iterations):
            payload = "hello from {} at {:.3f}".format(ORG, time.time())
            pub.put(payload)
            print("PUT  i={:02d}  key={}  payload={}".format(i, key, payload), flush=True)
            time.sleep(interval_s)
    finally:
        pub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
