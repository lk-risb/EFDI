#!/usr/bin/env python3
"""rest-http bridge — talk to a goat-moon-pod over plain HTTP, no Zenoh code in your app.

The bridge is itself a Zenoh client (mTLS, via the shared goat_connect helper). It exposes a
tiny localhost HTTP API so any language/tool that can do an HTTP request — curl, MATLAB,
old .NET, a PLC, a shell script — can publish and receive fabric data.

    pip install eclipse-zenoh
    export GOAT_ROUTER=tls/127.0.0.1:7447 GOAT_CERT=... GOAT_KEY=... GOAT_CA=... GOAT_NAMESPACE=release/acme
    python3 bridge.py                     # serves on 127.0.0.1:8080

Endpoints:
    POST /pub/<suffix>        body → publish to <namespace>/<suffix>           (201)
    GET  /sub/<keyexpr>?count=N   block for next N samples, return JSON array   (default N=1)
    GET  /stream/<keyexpr>    Server-Sent Events; one `data: {...}` per sample (continuous)

Optional outbound webhook: set WEBHOOK_URL + WEBHOOK_KEYEXPR to POST every matching sample to
an external URL (e.g. into your own system) with no client at all.

SECURITY: binds 127.0.0.1 only by default (BRIDGE_BIND to override). It is an UNAUTHENTICATED
local proxy holding your fabric credentials — keep it on loopback / behind your own auth.
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Reuse the canonical mTLS connect helper. Look for it relative to the repo first, then next to
# this script (Docker copies it alongside), then on PATH/site-packages.
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_here, "..", "..", "..", "connect", "python"), _here):
    if os.path.exists(os.path.join(_p, "goat_connect.py")):
        sys.path.insert(0, _p)
        break
import goat_connect  # noqa: E402

BIND = os.environ.get("BRIDGE_BIND", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8080"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_KEYEXPR = os.environ.get("WEBHOOK_KEYEXPR", "")

SESSION = goat_connect.session()
NS = goat_connect.namespace()


def _abs_key(suffix_or_key: str) -> str:
    # An absolute fabric key (e.g. release/goat/...) is passed through; a bare suffix is
    # scoped under your namespace.
    s = suffix_or_key.lstrip("/")
    return s if s.startswith(NS + "/") or s == NS else f"{NS}/{s}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/pub/"):
            return self._send(404, b'{"error":"POST /pub/<suffix>"}')
        suffix = path[len("/pub/"):]
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length else b""
        key = _abs_key(suffix)
        SESSION.put(key, payload)
        self._send(201, json.dumps({"published": key, "bytes": len(payload)}).encode())

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/sub/"):
            return self._collect(u.path[len("/sub/"):], parse_qs(u.query))
        if u.path.startswith("/stream/"):
            return self._stream(u.path[len("/stream/"):])
        return self._send(404, b'{"error":"GET /sub/<keyexpr> or /stream/<keyexpr>"}')

    def _collect(self, keyexpr, qs):
        count = int(qs.get("count", ["1"])[0])
        timeout = float(qs.get("timeout", ["30"])[0])
        out, done = [], threading.Event()

        def on_sample(sample):
            out.append(_sample_dict(sample))
            if len(out) >= count:
                done.set()

        sub = SESSION.declare_subscriber(_abs_key(keyexpr), on_sample)
        done.wait(timeout)
        sub.undeclare()
        self._send(200, json.dumps(out).encode())

    def _stream(self, keyexpr):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = []
        cv = threading.Condition()

        def on_sample(sample):
            with cv:
                q.append(_sample_dict(sample))
                cv.notify()

        sub = SESSION.declare_subscriber(_abs_key(keyexpr), on_sample)
        try:
            while True:
                with cv:
                    cv.wait(timeout=15)
                    items, q[:] = list(q), []
                if items:
                    for it in items:
                        self.wfile.write(f"data: {json.dumps(it)}\n\n".encode())
                else:
                    self.wfile.write(b": keepalive\n\n")  # SSE comment heartbeat
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            sub.undeclare()


def _sample_dict(sample) -> dict:
    raw = bytes(sample.payload)
    try:
        text = raw.decode("utf-8")
        return {"key": str(sample.key_expr), "ts": time.time(), "text": text}
    except UnicodeDecodeError:
        import base64
        return {"key": str(sample.key_expr), "ts": time.time(), "b64": base64.b64encode(raw).decode()}


def _webhook_loop():
    def on_sample(sample):
        body = json.dumps(_sample_dict(sample)).encode()
        try:
            req = urllib.request.Request(WEBHOOK_URL, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            print(f"webhook POST failed: {e}", file=sys.stderr, flush=True)

    SESSION.declare_subscriber(WEBHOOK_KEYEXPR, on_sample)
    print(f"webhook: {WEBHOOK_KEYEXPR} -> {WEBHOOK_URL}", flush=True)


def main():
    if WEBHOOK_URL and WEBHOOK_KEYEXPR:
        _webhook_loop()
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"rest-http bridge on http://{BIND}:{PORT}  (namespace {NS})", flush=True)
    print("  POST /pub/<suffix>   GET /sub/<keyexpr>?count=N   GET /stream/<keyexpr>", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
        SESSION.close()


if __name__ == "__main__":
    main()
