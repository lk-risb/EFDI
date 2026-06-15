#!/usr/bin/env python3
"""subscribe.py — receive data from the goat fabric (modern Python).

    python3 subscribe.py                          # your own namespace, follow forever
    python3 subscribe.py 'release/goat/**'  # inbound data goat sends you
    python3 subscribe.py '<keyexpr>' 5             # exit after 5 samples

Default key-expr is '<namespace>/**' (everything under your prefix). Use ** for any depth,
* for a single segment.
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "connect", "python"))
import goat_connect


def main() -> None:
    keyexpr = sys.argv[1] if len(sys.argv) > 1 else f"{goat_connect.namespace()}/**"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0  # 0 = follow forever
    seen = 0

    with goat_connect.session() as s:
        def on_sample(sample):
            nonlocal seen
            payload = bytes(sample.payload)
            try:
                shown = payload.decode("utf-8")
            except UnicodeDecodeError:
                shown = f"<{len(payload)} bytes> {payload[:32].hex()}"
            print(f"{time.strftime('%H:%M:%S')}  {sample.key_expr}  {shown}", flush=True)
            seen += 1

        sub = s.declare_subscriber(keyexpr, on_sample)
        print(f"subscribed: {keyexpr} (Ctrl-C to stop)", flush=True)
        try:
            while limit == 0 or seen < limit:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            sub.undeclare()


if __name__ == "__main__":
    main()
