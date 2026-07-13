#!/usr/bin/env python3
"""file-drop bridge — exchange fabric data as FILES. No Zenoh code, no HTTP, no network calls
from your app at all. Ideal for MATLAB, PLCs, old .NET, shell pipelines, and air-gapped shops
that can only read/write a directory.

The bridge (itself a Zenoh mTLS client via efdi_connect) does two things:
  OUTBOUND  — any file you drop into OUTBOX_DIR is published to <namespace>/<relative-path>,
              then moved to OUTBOX_DIR/.sent/. So `cp reading.json outbox/sensors/temp` publishes
              to release/acme/sensors/temp.
  INBOUND   — every sample matching SUB_KEYEXPR is written as a file under INBOX_DIR, named by
              its key path (+ a timestamp), so your app just watches a folder.

    pip install eclipse-zenoh
    export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
    export OUTBOX_DIR=./outbox INBOX_DIR=./inbox SUB_KEYEXPR='release/<partner>/**'
    python3 bridge.py

Poll-based (stdlib only, no watchdog dependency) so it runs anywhere, offline, on any OS.
"""
import os
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_here, "..", "..", "..", "connect", "python"), _here):
    if os.path.exists(os.path.join(_p, "efdi_connect.py")):
        sys.path.insert(0, _p)
        break
import efdi_connect  # noqa: E402

OUTBOX = os.environ.get("OUTBOX_DIR", "./outbox")
INBOX = os.environ.get("INBOX_DIR", "./inbox")
SUB_KEYEXPR = os.environ.get("SUB_KEYEXPR", "")  # empty = inbound disabled
POLL_S = float(os.environ.get("POLL_SECONDS", "1.0"))

SENT = os.path.join(OUTBOX, ".sent")
NS = efdi_connect.namespace()


def _safe_name(key: str) -> str:
    return key.replace("/", "__").replace("..", "_")


def main():
    os.makedirs(OUTBOX, exist_ok=True)
    os.makedirs(SENT, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    session = efdi_connect.session()

    # INBOUND: subscribe → write files.
    if SUB_KEYEXPR:
        def on_sample(sample):
            raw = bytes(sample.payload)
            name = f"{_safe_name(str(sample.key_expr))}.{int(time.time()*1000)}.bin"
            tmp = os.path.join(INBOX, "." + name)
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, os.path.join(INBOX, name))  # atomic appear

        session.declare_subscriber(SUB_KEYEXPR, on_sample)
        print(f"inbound: {SUB_KEYEXPR} -> {INBOX}/", flush=True)

    print(f"outbound: {OUTBOX}/ -> {NS}/<relative-path>  (sent files -> {SENT}/)", flush=True)
    print("file-drop bridge running (Ctrl-C to stop)", flush=True)

    # OUTBOUND: poll the outbox, publish + move.
    try:
        while True:
            for root, _dirs, files in os.walk(OUTBOX):
                if os.path.abspath(root).startswith(os.path.abspath(SENT)):
                    continue
                for fn in files:
                    if fn.startswith("."):  # skip in-progress writes
                        continue
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, OUTBOX)
                    # key suffix = relative path without any trailing extension noise kept as-is
                    key = efdi_connect.key(rel.replace(os.sep, "/"))
                    try:
                        with open(full, "rb") as f:
                            payload = f.read()
                        session.put(key, payload)
                        dest = os.path.join(SENT, rel.replace(os.sep, "__"))
                        os.makedirs(os.path.dirname(dest) or SENT, exist_ok=True)
                        os.replace(full, dest)
                        print(f"published {full} -> {key} ({len(payload)} B)", flush=True)
                    except OSError as e:
                        print(f"skip {full}: {e}", file=sys.stderr, flush=True)
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        session.close()


if __name__ == "__main__":
    main()
