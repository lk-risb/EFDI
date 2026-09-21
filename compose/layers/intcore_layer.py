#!/usr/bin/env python3
"""intcore_layer.py — Zenoh EFDI track topics → INT-CORE TopicApi (NVG 2.0.2).

C2 EGRESS half of the INT-CORE (NATO Integration Core) pair. Subscribes to all
EFDI track topics and POSTs each one into a pre-provisioned INT-CORE Topic as a
standalone NVG 2.0.2 document, reusing sitaware_layer.py's own SIDC mapping and
NVG builder — the same dialect INT-CORE's Topic already validates against.

Ingest from whatever INT-CORE disseminates back out belongs in the matching
bridges/intcore_bridge.py — this file only writes out (see sitaware_layer.py's
own docstring for why a bidirectional system gets two files, not one).

INT-CORE's ingestion contract (confirmed by live testing against a real
INT-CORE 7 instance, not inferred from documentation):
    POST <INTCORE_URL>/topicapi/Topic/SaveItems
    Header: ApiKey: <INTCORE_API_KEY>
    Body:   {"topicId": "<INTCORE_TOPIC_ID>", "content": "<NVG XML>",
             "dataSourceId": "<INTCORE_DATA_SOURCE_ID>"}
`topicId` must already exist INT-CORE-side (SaveItems 404s otherwise) and have
an NVG 2.0 schema attached, or a bad document surfaces as an unhandled 500
("Response status code does not indicate success: 400") rather than a clean
400 — a known quirk of that endpoint, not a sign the request never arrived.

Configuration (compose/.env):
    INTCORE_URL=https://localhost           # INT-CORE's nginx front, no trailing slash
    INTCORE_API_KEY=                        # TopicApi's ApiKey header value
    INTCORE_TOPIC_ID=                       # GUID of a pre-provisioned NVG-validated Topic
    INTCORE_DATA_SOURCE_ID=efdi             # tags rows/dissemination — intcore_bridge.py
                                             # uses the same value to filter out its own echo
    INTCORE_TLS_VERIFY=1                    # set 0 to skip certificate check (self-signed lab cert)

Run:
    venv/bin/python3 intcore_layer.py
    venv/bin/python3 intcore_layer.py --verbose
"""

import argparse
import json
import os
import signal
import ssl
import threading
import time
import urllib.error
import urllib.request

from namespace_prefix import topic_root
from protocols.gateway import open_session, subscribe
from layers.tak_layer import _is_unfused_sensor_track
from layers.sitaware_layer import _TOPIC_SIDC, _resolve_sidc, track_to_nvg_item, _NON_JSON_VIEWS

TOPIC_ROOT = topic_root()
ZENOH_RETRY_S = 5

_URL           = os.environ.get("INTCORE_URL", "").rstrip("/")
_API_KEY       = os.environ.get("INTCORE_API_KEY", "")
_TOPIC_ID      = os.environ.get("INTCORE_TOPIC_ID", "")
_DATA_SOURCE_ID = os.environ.get("INTCORE_DATA_SOURCE_ID", "efdi")
_TLS_VERIFY    = os.environ.get("INTCORE_TLS_VERIFY", "1") not in ("0", "false", "no")


def _terminal_view(key: str) -> str:
    """The view segment, ignoring a trailing /tracks/vN version tail
    (see tak_layer.py's own _terminal_view, which this mirrors)."""
    parts = key.split("/")
    if (len(parts) >= 2 and parts[-2] == "tracks"
            and parts[-1][:1] == "v" and parts[-1][1:].isdigit()):
        parts = parts[:-2]
    return parts[-1] if parts else ""


def _make_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not _TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def save_item(content: str, verbose: bool) -> None:
    """POST one NVG document to INT-CORE's Topic/SaveItems."""
    body = json.dumps({
        "topicId": _TOPIC_ID,
        "content": content,
        "dataSourceId": _DATA_SOURCE_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        "{}/topicapi/Topic/SaveItems".format(_URL),
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("ApiKey", _API_KEY)
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10) as resp:
            if verbose:
                print("INT-CORE SaveItems -> HTTP {}".format(resp.status), flush=True)
    except urllib.error.HTTPError as exc:
        # A schema-invalid document surfaces here as a 500, not a 400 — a
        # confirmed quirk of this endpoint. Log and move on; one bad track
        # must not stop the rest of the fabric from reaching INT-CORE.
        print("INT-CORE SaveItems rejected: HTTP {} {}".format(exc.code, exc.reason), flush=True)
    except Exception as exc:
        print("INT-CORE SaveItems failed: {}".format(exc), flush=True)


def make_handler(sidc, verbose: bool):
    def handler(sample) -> None:
        key = str(sample.key_expr)
        if _terminal_view(key) in _NON_JSON_VIEWS:
            return
        try:
            track = json.loads(bytes(sample.payload).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        if not isinstance(track, dict):
            return
        # Never re-send what intcore_bridge.py just brought in from INT-CORE
        # itself, and skip raw sensor returns the same way tak_layer/sitaware_layer
        # do — INT-CORE gets the fused picture, not every unfiltered radar plot.
        if track.get("_ingress") == "intcore" or track.get("_delete"):
            return
        if _is_unfused_sensor_track(track, key):
            return
        item = track_to_nvg_item(track, _resolve_sidc(sidc, track))
        if item is None:
            return
        _uid, xml_doc = item
        save_item(xml_doc, verbose)
        if verbose:
            print("INT-CORE <- {}".format(_uid), flush=True)

    return handler


def run(args) -> None:
    if not args.url:
        raise SystemExit("Set INTCORE_URL in .env or pass --url")
    if not args.topic_id:
        raise SystemExit("Set INTCORE_TOPIC_ID in .env or pass --topic-id")
    if not args.api_key:
        raise SystemExit("Set INTCORE_API_KEY in .env or pass --api-key")

    global _URL, _API_KEY, _TOPIC_ID, _DATA_SOURCE_ID, _TLS_VERIFY
    _URL, _API_KEY, _TOPIC_ID, _DATA_SOURCE_ID = args.url, args.api_key, args.topic_id, args.data_source_id
    _TLS_VERIFY = args.tls_verify

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("intcore_layer Zenoh connect failed: {} — retry in {}s".format(exc, ZENOH_RETRY_S), flush=True)
            time.sleep(ZENOH_RETRY_S)

    subs = []
    try:
        for suffix, sidc in _TOPIC_SIDC.items():
            key = "{}/{}".format(TOPIC_ROOT, suffix)
            subs.append(subscribe(session, key, make_handler(sidc, args.verbose)))
            print("SUB {} -> INT-CORE Topic {}".format(key, _TOPIC_ID), flush=True)

        print("INT-CORE layer running: {} -> {}/topicapi/Topic/SaveItems (topic {})".format(
            TOPIC_ROOT, _URL, _TOPIC_ID), flush=True)
        if not _TLS_VERIFY:
            print("WARNING: INTCORE_TLS_VERIFY=0 — certificate verification is OFF.", flush=True)

        # Subscribers run on Zenoh's own threads; this thread only holds the
        # process open and must actually notice SIGTERM (tak_layer.py's own
        # run() hit this exact bug first — Python's default SIGTERM
        # disposition kills the interpreter silently, no traceback, no log).
        stop = threading.Event()

        def _on_signal(signum, _frame):
            print("shutting down on {}".format(signal.Signals(signum).name), flush=True)
            stop.set()

        for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(_sig, _on_signal)
            except (OSError, ValueError):
                pass  # not the main thread, or the platform lacks this signal

        print("Layer running — Ctrl-C to stop", flush=True)
        try:
            stop.wait()
        except KeyboardInterrupt:
            print("shutting down on KeyboardInterrupt", flush=True)
    finally:
        for sub in subs:
            sub.undeclare()
        session.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Zenoh tracks -> INT-CORE TopicApi (NVG 2.0.2)")
    ap.add_argument("--url", default=os.environ.get("INTCORE_URL", "").rstrip("/"))
    ap.add_argument("--api-key", default=os.environ.get("INTCORE_API_KEY", ""))
    ap.add_argument("--topic-id", default=os.environ.get("INTCORE_TOPIC_ID", ""))
    ap.add_argument("--data-source-id", default=os.environ.get("INTCORE_DATA_SOURCE_ID", "efdi"))
    ap.add_argument("--tls-verify", action="store_true",
                     default=os.environ.get("INTCORE_TLS_VERIFY", "1") not in ("0", "false", "no"))
    ap.add_argument("--verbose", "-v", action="store_true")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
