#!/usr/bin/env python3
"""Expose live EFDI tracks as a pull-based NVG 2.0.2 feed for SitaWare HQ.

SitaWare Headquarters 6.22 can create an ``NVG Import Subscription`` whose
remote endpoint is polled periodically.  This native process subscribes to the
same Zenoh track topics as the Edge NVG adapter, keeps a bounded live snapshot,
and serves that snapshot as one NVG document over HTTP(S).

This is deliberately separate from ``nato_nvg_layer.py``'s SitaWare Edge REST
client: HQ pulls this document; Edge receives per-item PUT/DELETE requests.

Required configuration:

    SITAWARE_HQ_NVG_USER=efdi-feed
    SITAWARE_HQ_NVG_PASS=<dedicated-random-password>

For a remote HQ host, either configure TLS or explicitly acknowledge an
isolated lab-only HTTP connection:

    SITAWARE_HQ_NVG_BIND=0.0.0.0
    SITAWARE_HQ_NVG_PORT=8088
    SITAWARE_HQ_NVG_TLS_CERT=/path/to/server-cert.pem
    SITAWARE_HQ_NVG_TLS_KEY=/path/to/server-key.pem

The SitaWare subscription URL is then ``https://<efdi-host>:8088/nvg``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import ssl
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import zenoh
from nato_nvg_layer import (
    NVG_NS,
    NVG_VERSION,
    TOPIC_ROOT,
    _TOPIC_SIDC,
    make_config,
    track_to_nvg_item,
)

MAX_ZENOH_PAYLOAD = 1_000_000
_TOPIC_STALE_S = {
    "env/weather/station/**": 7200.0,
}
_HQ_SYMBOL_SCHEME = "2525b"


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class NVGFeedCache:
    """Thread-safe, size-bounded snapshot of recently received NVG items."""

    def __init__(
        self,
        stale_s: float,
        max_tracks: int,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not math.isfinite(stale_s) or stale_s <= 0:
            raise ValueError("stale_s must be a positive finite number")
        if max_tracks <= 0:
            raise ValueError("max_tracks must be positive")
        self._stale_s = stale_s
        self._max_tracks = max_tracks
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, float, float]] = {}

    def upsert(self, track: dict, sidc: str, stale_s: float | None = None) -> str | None:
        item_stale_s = self._stale_s if stale_s is None else stale_s
        if not math.isfinite(item_stale_s) or item_stale_s <= 0:
            raise ValueError("item stale_s must be a positive finite number")
        result = track_to_nvg_item(
            track,
            sidc,
            symbol_scheme=_HQ_SYMBOL_SCHEME,
            valid_until=self._wall_clock() + item_stale_s,
        )
        if result is None:
            return None
        uid, xml = result
        now = self._clock()
        with self._lock:
            if uid not in self._items and len(self._items) >= self._max_tracks:
                oldest_uid = min(self._items, key=lambda key: self._items[key][1])
                del self._items[oldest_uid]
            self._items[uid] = (xml, now, item_stale_s)
        return uid

    def _snapshot(self) -> list[tuple[str, str]]:
        now = self._clock()
        with self._lock:
            expired = [
                uid for uid, (_, seen_at, stale_s) in self._items.items()
                if now - seen_at > stale_s
            ]
            for uid in expired:
                del self._items[uid]
            return sorted((uid, xml) for uid, (xml, _, _) in self._items.items())

    def document(self) -> tuple[bytes, int]:
        snapshot = self._snapshot()
        ET.register_namespace("", NVG_NS)
        root = ET.Element("{%s}nvg" % NVG_NS, {"version": NVG_VERSION})
        count = 0
        for _, item_xml in snapshot:
            # This XML was generated locally by track_to_nvg_item; no external
            # or user-supplied XML is parsed here.
            try:
                item_root = ET.fromstring(item_xml)
            except ET.ParseError:
                continue
            for child in item_root:
                root.append(child)
                count += 1
        body = b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(
            root, encoding="utf-8"
        )
        return body, count


def basic_authorized(header: str | None, username: str, password: str) -> bool:
    if not header or not username or not password:
        return False
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    expected = "Basic " + token
    return hmac.compare_digest(header, expected)


class NVGFeedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        cache: NVGFeedCache,
        feed_path: str,
        username: str,
        password: str,
        allow_anonymous: bool,
        verbose: bool,
    ) -> None:
        self.cache = cache
        self.feed_path = feed_path
        self.username = username
        self.password = password
        self.allow_anonymous = allow_anonymous
        self.verbose = verbose
        super().__init__(address, NVGFeedHandler)


class NVGFeedHandler(BaseHTTPRequestHandler):
    server: NVGFeedServer
    server_version = "EFDI-NVG/1.0"
    sys_version = ""

    def _authorized(self) -> bool:
        return self.server.allow_anonymous or basic_authorized(
            self.headers.get("Authorization"),
            self.server.username,
            self.server.password,
        )

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _reject_unauthorized(self) -> None:
        body = b"Authentication required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="EFDI NVG feed", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self, include_body: bool) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path not in {self.server.feed_path, "/healthz"}:
            body = b"Not found\n"
            self._headers(404, "text/plain; charset=utf-8", len(body))
            if include_body:
                self.wfile.write(body)
            return
        if not self._authorized():
            self._reject_unauthorized()
            return

        if path == "/healthz":
            _, count = self.server.cache.document()
            body = json.dumps({"status": "ok", "tracks": count}, separators=(",", ":")).encode()
            content_type = "application/json; charset=utf-8"
        else:
            body, _ = self.server.cache.document()
            content_type = "application/xml; charset=utf-8"
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route(include_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = b"Method not allowed\n"
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        if self.server.verbose:
            print("NVG HTTP {} - {}".format(self.client_address[0], fmt % args), flush=True)


def make_handler(
    sidc: str,
    cache: NVGFeedCache,
    verbose: bool,
    stale_s: float | None = None,
):
    def handler(sample) -> None:
        try:
            payload = bytes(sample.payload)
            if len(payload) > MAX_ZENOH_PAYLOAD:
                raise ValueError("payload exceeds 1 MB")
            track = json.loads(payload.decode("utf-8"))
            if not isinstance(track, dict):
                raise ValueError("track payload is not an object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            if verbose:
                print("NVG feed ignored invalid Zenoh sample: {}".format(exc), flush=True)
            return
        uid = cache.upsert(track, sidc, stale_s=stale_s)
        if verbose and uid:
            print("NVG feed cached {}".format(uid), flush=True)

    return handler


def _validate_args(args, password: str) -> None:
    if not args.path.startswith("/") or "?" in args.path or "#" in args.path:
        raise SystemExit("SITAWARE_HQ_NVG_PATH must be an absolute path without query/fragment")
    if not (1 <= args.port <= 65535):
        raise SystemExit("SITAWARE_HQ_NVG_PORT must be between 1 and 65535")
    if bool(args.tls_cert) != bool(args.tls_key):
        raise SystemExit("Set both SITAWARE_HQ_NVG_TLS_CERT and SITAWARE_HQ_NVG_TLS_KEY")
    if not args.allow_anonymous and (not args.user or not password):
        raise SystemExit(
            "Set SITAWARE_HQ_NVG_USER and SITAWARE_HQ_NVG_PASS, or explicitly "
            "set SITAWARE_HQ_NVG_ALLOW_ANONYMOUS=1"
        )
    non_loopback = args.bind not in {"127.0.0.1", "::1", "localhost"}
    if non_loopback and not args.tls_cert and not args.allow_insecure_http:
        raise SystemExit(
            "Refusing a non-loopback plain-HTTP feed. Configure TLS, or explicitly "
            "set SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1 for an isolated lab network."
        )


def run(args) -> None:
    password = os.environ.get("SITAWARE_HQ_NVG_PASS", "")
    _validate_args(args, password)

    cache = NVGFeedCache(args.stale_s, args.max_tracks)
    server = NVGFeedServer(
        (args.bind, args.port),
        cache,
        args.path,
        args.user,
        password,
        args.allow_anonymous,
        args.verbose,
    )
    scheme = "http"
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"

    session = zenoh.open(make_config())
    subscribers = []
    try:
        for suffix, sidc in _TOPIC_SIDC.items():
            key = "{}/{}".format(TOPIC_ROOT, suffix)
            item_stale_s = max(args.stale_s, _TOPIC_STALE_S.get(suffix, args.stale_s))
            subscribers.append(
                session.declare_subscriber(
                    key,
                    make_handler(sidc, cache, args.verbose, stale_s=item_stale_s),
                )
            )
            print("SUB {} -> SIDC {}".format(key, sidc), flush=True)

        print(
            "SitaWare HQ NVG feed listening on {}://{}:{}{} (stale={}s, max={})".format(
                scheme, args.bind, args.port, args.path, args.stale_s, args.max_tracks
            ),
            flush=True,
        )
        if scheme == "http" and args.user:
            print(
                "WARNING: Basic Auth is being sent over plain HTTP. Use only on an "
                "isolated lab network and migrate the feed to HTTPS.",
                flush=True,
            )
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        for subscriber in subscribers:
            subscriber.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Zenoh tracks -> SitaWare HQ NVG pull feed")
    parser.add_argument("--bind", default=os.environ.get("SITAWARE_HQ_NVG_BIND", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SITAWARE_HQ_NVG_PORT", "8088"))
    )
    parser.add_argument("--path", default=os.environ.get("SITAWARE_HQ_NVG_PATH", "/nvg"))
    parser.add_argument("--user", default=os.environ.get("SITAWARE_HQ_NVG_USER", ""))
    parser.add_argument(
        "--tls-cert", default=os.environ.get("SITAWARE_HQ_NVG_TLS_CERT", "")
    )
    parser.add_argument("--tls-key", default=os.environ.get("SITAWARE_HQ_NVG_TLS_KEY", ""))
    parser.add_argument(
        "--stale-s",
        type=float,
        default=float(os.environ.get("SITAWARE_HQ_NVG_STALE_S", "120")),
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        default=int(os.environ.get("SITAWARE_HQ_NVG_MAX_TRACKS", "10000")),
    )
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        default=_env_true("SITAWARE_HQ_NVG_ALLOW_ANONYMOUS"),
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        default=_env_true("SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP"),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
