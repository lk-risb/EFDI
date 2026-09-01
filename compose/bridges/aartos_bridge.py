#!/usr/bin/env python3
"""Aaronia AARTOS drone detection (RTSA-Suite PRO HTTP Server block) -> Zenoh raw payloads.

The AARTOS HTTP Server block is added to RTSA-Suite PRO's own flow graph on
the laptop (wired to the Tracking/Drone Detection block's output) and exposes
a documented REST API there — see the vendor's "RTSA-Suite PRO JSON Protocol
Documentation" and "HTTP Stream Server Endpoints" PDFs. This bridge holds a
single long-lived GET /stream connection open and republishes each JSON
sample verbatim, same "decode nothing here" shape as mqtt_bridge.py —
protocols/vendors/aartos/aartos.py turns the AARTOS Sample/TrackState schema
into canonical EFDI track records.

This does NOT talk to the radar hardware directly (fixed ports 54667-54669
on the radar itself). That raw connection is RTSA-Suite PRO's own private
link to the antenna and only accepts one client; the HTTP Server block runs
alongside it, on the laptop's own port (default 54664), and is the vendor's
documented external-consumption path instead.

Config (compose/.env):
  AARTOS_HOST=192.168.x.x        # required — the RTSA-Suite PRO laptop
  AARTOS_PORT=54664              # HTTP Server block's port
  AARTOS_LIMIT=                  # optional: cap samples per /stream call
  AARTOS_TIMEOUT_S=30            # socket read timeout before reconnecting
  AARTOS_RECONNECT_S=10
  AARTOS_MODE=stream             # "stream" (default) or "poll"
  AARTOS_POLL_INTERVAL_S=1.0     # poll mode only — seconds between GET /sample

Some RTSA-Suite PRO deployments never actually push data through /stream
(chunked body opens fine, HTTP 200, but no bytes ever follow, even for a
block whose /sample and /healthstatus both report real, live data) — set
AARTOS_MODE=poll to fall back to repeatedly GET /sample instead. /sample
returns one Sample object per call (the same {"data": {"trackings": [...],
...}} shape /stream would otherwise deliver framed by RS bytes), so the
decoder in protocols/vendors/aartos/aartos_json.py needs no changes either
way — this bridge just republishes each poll's raw response body verbatim.

Run:
  venv/bin/python3 bridges/aartos_bridge.py
"""

from __future__ import annotations

import argparse
import http.client
import os
import time

from namespace_prefix import topic_root
import zenoh
from protocols.gateway import open_session

TOPIC_ROOT = topic_root()
RAW_ROOT = "{}/raw/aartos".format(TOPIC_ROOT)
_RECONNECT_S = float(os.environ.get("AARTOS_RECONNECT_S", "10"))
_RS = 0x1E  # ASCII Record Separator — how /stream delimits JSON samples
MAX_SAMPLE = int(os.environ.get("AARTOS_MAX_SAMPLE", "1048576"))


def _key_segment(value: str) -> str:
    """Make the source host safe for a Zenoh key expression."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value) or "_"


def stream_samples(conn: http.client.HTTPConnection, path: str):
    """Yield each RS-delimited JSON sample from a long-lived GET <path>.

    RTSA-Suite PRO's /stream sends HTTP chunks containing complete or partial
    JSON samples separated by ASCII 30 (RS) — buffer across chunk boundaries
    the same way a newline-delimited reader would, just on a different byte.
    """
    conn.request("GET", path)
    response = conn.getresponse()
    if response.status != 200:
        raise ConnectionError("AARTOS {} returned HTTP {}".format(path, response.status))
    buf = b""
    while True:
        chunk = response.read(65536)
        if not chunk:
            return
        buf += chunk
        while True:
            idx = buf.find(bytes([_RS]))
            if idx < 0:
                break
            sample, buf = buf[:idx], buf[idx + 1:]
            if sample:
                yield sample
        if len(buf) > MAX_SAMPLE:
            print("AARTOS sample exceeds {} bytes with no RS terminator; dropping buffer".format(MAX_SAMPLE),
                  flush=True)
            buf = b""


def poll_sample(conn: http.client.HTTPConnection) -> bytes | None:
    """Fetch a single Sample via GET /sample. Returns None on an empty/absent
    sample (RTSA-Suite PRO reports "null" when nothing is available yet)."""
    conn.request("GET", "/sample")
    response = conn.getresponse()
    body = response.read()
    if response.status != 200:
        raise ConnectionError("AARTOS /sample returned HTTP {}".format(response.status))
    if body.strip() == b"null":
        return None
    return body


def run(args) -> None:
    if not args.host:
        raise SystemExit("Set AARTOS_HOST in .env or pass --host")

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("AARTOS bridge Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    key = "{}/{}".format(RAW_ROOT, _key_segment(args.host))

    if args.mode == "poll":
        print("AARTOS ingress (poll): {}:{}/sample every {}s -> {}".format(
            args.host, args.port, args.poll_interval, key), flush=True)
        try:
            while True:
                conn = http.client.HTTPConnection(args.host, args.port, timeout=args.timeout)
                try:
                    sample = poll_sample(conn)
                    if sample is not None:
                        session.put(key, sample, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                        if args.verbose:
                            print("AARTOS RAW {} bytes -> {}".format(len(sample), key), flush=True)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print("AARTOS poll error: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
                    conn.close()
                    time.sleep(_RECONNECT_S)
                    continue
                finally:
                    conn.close()
                time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            session.close()
        return

    path = "/stream?limit={}".format(args.limit) if args.limit else "/stream"
    print("AARTOS ingress: {}:{}{} -> {}".format(args.host, args.port, path, key), flush=True)

    try:
        while True:
            conn = http.client.HTTPConnection(args.host, args.port, timeout=args.timeout)
            count = 0
            try:
                for sample in stream_samples(conn, path):
                    session.put(key, sample, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                    count += 1
                    if args.verbose:
                        print("AARTOS RAW {} bytes -> {}".format(len(sample), key), flush=True)
                print("AARTOS stream ended after {} samples ({}:{}); reconnecting in {}s".format(
                    count, args.host, args.port, _RECONNECT_S), flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print("AARTOS stream error after {} samples: {} — retry in {}s".format(
                    count, exc, _RECONNECT_S), flush=True)
            finally:
                conn.close()
            time.sleep(_RECONNECT_S)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Aaronia AARTOS HTTP stream ingress -> Zenoh raw payloads")
    ap.add_argument("--host", default=os.environ.get("AARTOS_HOST", ""))
    ap.add_argument("--port", type=int, default=int(os.environ.get("AARTOS_PORT", "54664")))
    ap.add_argument("--limit", type=int, default=int(os.environ.get("AARTOS_LIMIT", "0")) or None)
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("AARTOS_TIMEOUT_S", "30")))
    ap.add_argument("--mode", choices=("stream", "poll"), default=os.environ.get("AARTOS_MODE", "stream"))
    ap.add_argument("--poll-interval", type=float, default=float(os.environ.get("AARTOS_POLL_INTERVAL_S", "1.0")))
    ap.add_argument("--verbose", "-v", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
