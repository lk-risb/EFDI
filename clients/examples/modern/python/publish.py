#!/usr/bin/env python3
"""publish.py — send data to the EFDI fabric (modern Python).

    pip install eclipse-zenoh
    export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
    python3 publish.py                      # one JSON sample
    python3 publish.py 50 0.2               # 50 samples at 200ms

Publishes JSON under <namespace>/sensors/temp. Real payloads can be anything (bytes, protobuf,
CBOR); JSON here for legibility.
"""
import json
import sys
import time
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "connect", "python"))
import efdi_connect


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    key = efdi_connect.key("sensors/temp")

    with efdi_connect.session() as s:
        pub = s.declare_publisher(key)
        for i in range(n):
            payload = json.dumps({"ts": time.time(), "seq": i, "temp_c": 21.5 + i * 0.1})
            pub.put(payload.encode("utf-8"))
            print(f"published -> {key}: {payload}", flush=True)
            if i + 1 < n:
                time.sleep(interval)


if __name__ == "__main__":
    main()
