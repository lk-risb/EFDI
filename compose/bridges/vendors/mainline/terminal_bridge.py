#!/usr/bin/env python3
"""mainline.inc TERMINAL (backend: mainframe) -> Zenoh bridge.

Logs into mainline.inc's TERMINAL drone-fleet C2 platform, subscribes to its
live per-drone state WebSocket, and republishes each drone as an EFDI track
so tak_layer.py forwards it to ATAK/WinTAK as a friendly UAV.

============================================================================
STATUS: untested shell, not a confirmed-working bridge yet
============================================================================
Reverse-engineered entirely from a HAR capture of an already-authenticated
browser session against the staging instance (terminal.dev.mainline.inc /
mainframe.dev.mainline.inc) — the capture had no sign-in request (the
session was already active) and Chrome strips session cookies from HAR
exports by design, so the actual login mechanism was never directly
observed. Specifically UNVERIFIED against a real login attempt:

- The sign-in endpoint/method/field names below (`POST /api/auth/sign-in/
  email` with `{"email": ..., "password": ...}`). `/api/auth/get-session`
  IS a confirmed, distinctively-named route from the `better-auth` JS
  library (observed live in the HAR) — if TERMINAL really uses that
  library, this is its standard email/password sign-in shape, but that's
  inference from the library's convention, not a captured request.
- Whether the WebSocket upgrade needs the session cookie forwarded
  explicitly (websocket-client's `header=` below assumes yes, matching how
  the browser's own same-origin cookie jar would have done it) or some
  other token entirely.
- The `registry` topic's payload shape (would carry friendly per-drone
  names instead of raw ULIDs) — never subscribed to in the capture, so
  `_callsign_for()` below falls back to the raw entity ID.

The `state/*` message shape (`position`/`flightState`/`battery`/`health`/
`tasking` per entity) and the `hello`/`welcome`/`sub` WebSocket protocol
ARE confirmed live from the capture — that part of this file is a real
schema, not a guess. Fix the three items above once a real login attempt
(or a fresh HAR capture starting from a logged-out state) confirms them.
Until then this bridge will either work as written or fail loudly at the
login step with an HTTP error, never silently pretend to work.

============================================================================
CONFIGURATION
============================================================================
    MAINLINE_TERMINAL_URL=https://mainframe.dev.mainline.inc   (default; staging)
    MAINLINE_TERMINAL_USER=team@mainline.inc
    MAINLINE_TERMINAL_PASS=...

Zenoh topic published (one per known drone):
    <TOPIC_ROOT>/air/mainline_terminal/friendly/uav/{entity}/tracks
so tak_layer.py's `"air/**/friendly/uav/**"` entry assigns CoT type
a-f-A-M-F-Q (friendly UAV) automatically — no tak_layer.py changes needed.

Run:
    venv/bin/python3 terminal_bridge.py
    venv/bin/python3 terminal_bridge.py --verbose
"""

import argparse
import json
import os
import ssl
import time
import urllib.error
import urllib.request

import websocket

from http_json import read_json_response
from namespace_prefix import topic_root
from protocols.gateway import open_session
import zenoh

TERMINAL_URL   = os.environ.get("MAINLINE_TERMINAL_URL", "https://mainframe.dev.mainline.inc").rstrip("/")
TERMINAL_USER  = os.environ.get("MAINLINE_TERMINAL_USER", "")
TERMINAL_PASS  = os.environ.get("MAINLINE_TERMINAL_PASS", "")
TOPIC_ROOT     = topic_root()

RECONNECT_S    = 10
STALE_S        = 120   # matches other bridges' fallback for a source with no explicit stale/TTL contract


def _login() -> str:
    """POST email/password sign-in, return the Set-Cookie session value.
    See the module docstring — this endpoint/shape is UNVERIFIED."""
    if not TERMINAL_USER or not TERMINAL_PASS:
        raise RuntimeError("MAINLINE_TERMINAL_USER/MAINLINE_TERMINAL_PASS not set")
    payload = json.dumps({"email": TERMINAL_USER, "password": TERMINAL_PASS}).encode()
    req = urllib.request.Request(
        TERMINAL_URL + "/api/auth/sign-in/email",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=15) as resp:
            read_json_response(resp)   # discard body — only the cookie is needed
            cookie = resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("TERMINAL login failed: HTTP {} {}".format(exc.code, exc.read()[:500])) from exc
    if not cookie:
        raise RuntimeError("TERMINAL login response had no Set-Cookie header")
    return cookie.split(";", 1)[0]


def _flight_state_to_track(entity: str, state: dict, name: str | None) -> dict | None:
    """Map one TERMINAL state/* payload's `state` object to an EFDI track.
    Field names/shapes here are confirmed live from the HAR capture."""
    position = state.get("position", {}).get("data")
    if not position or position.get("lat") is None or position.get("lng") is None:
        return None   # no fix yet — nothing to publish a position for
    flight = (state.get("flightState") or {}).get("data") or {}
    battery = (state.get("battery") or {}).get("data") or {}
    health = (state.get("health") or {}).get("data") or {}
    tasking = (state.get("tasking") or {}).get("data") or {}

    track = {
        "_src": "mainline_terminal",
        "_ts": time.time(),
        "uid": entity,
        "callsign": name or entity,
        "lat_deg": position["lat"],
        "lon_deg": position["lng"],
        "alt_m": position.get("alt"),
        "heading_deg": position.get("heading"),
        "ground_speed_kts": (position.get("hSpeed") or 0) * 1.943844,   # m/s -> kt
        "armed": flight.get("armed"),
        "flight_state": flight.get("state"),
        "battery_pct": battery.get("percent"),
        "mission_id": tasking.get("missionId"),
        "mission_phase": tasking.get("phase"),
    }
    alerts = health.get("alerts") or []
    if alerts:
        track["alerts"] = "; ".join(
            "{} ({})".format(a.get("message", a.get("code", "?")), a.get("severity", "?"))
            for a in alerts
        )
    return track


class _TerminalFeed:
    """Owns the WebSocket connection and the topic_dev template it publishes
    each drone's track to. One instance per bridge run."""

    def __init__(self, session: "zenoh.Session", cookie: str, verbose: bool):
        self._pub_cache: dict[str, "zenoh.Publisher"] = {}
        self._session = session
        self._cookie = cookie
        self._verbose = verbose
        self._names: dict[str, str] = {}

    def _publisher_for(self, entity: str) -> "zenoh.Publisher":
        pub = self._pub_cache.get(entity)
        if pub is None:
            topic = "{}/air/mainline_terminal/friendly/uav/{}/tracks".format(TOPIC_ROOT, entity)
            pub = self._session.declare_publisher(topic)
            self._pub_cache[entity] = pub
        return pub

    def _handle_frame(self, raw: str):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind = msg.get("t")
        if kind == "registry":
            # UNVERIFIED shape — never subscribed to in the capture. Only
            # applied if it turns out to carry an {id, name} pair per entry;
            # anything else is ignored rather than guessed at further.
            for entry in msg.get("entities", []) if isinstance(msg.get("entities"), list) else []:
                if isinstance(entry, dict) and entry.get("id") and entry.get("name"):
                    self._names[entry["id"]] = entry["name"]
        elif kind == "state":
            entity = msg.get("entity")
            state = msg.get("state")
            if not entity or not isinstance(state, dict):
                return
            track = _flight_state_to_track(entity, state, self._names.get(entity))
            if track is None:
                return
            self._publisher_for(entity).put(
                json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON
            )
            if self._verbose:
                print("TRACK", entity, track.get("callsign"),
                      "{:.5f},{:.5f}".format(track["lat_deg"], track["lon_deg"]), flush=True)

    def run_forever(self):
        ws_url = TERMINAL_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        ws = websocket.create_connection(
            ws_url, header=["Cookie: " + self._cookie], timeout=30
        )
        try:
            ws.send(json.dumps({"t": "hello", "proto": 1}))
            welcome = json.loads(ws.recv())
            if welcome.get("t") != "welcome":
                raise RuntimeError("Unexpected TERMINAL WS handshake reply: {}".format(welcome))
            print("TERMINAL WS connected — principal: {}".format(
                (welcome.get("principal") or {}).get("name", "?")), flush=True)
            for i, topic in enumerate(("registry", "state/*")):
                ws.send(json.dumps({"t": "sub", "id": "s{}".format(i + 1), "topic": topic}))
            while True:
                self._handle_frame(ws.recv())
        finally:
            ws.close()


def main():
    ap = argparse.ArgumentParser(description="mainline.inc TERMINAL -> Zenoh bridge")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("mainline_terminal Zenoh connect failed: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
            time.sleep(RECONNECT_S)

    print("mainline_terminal bridge starting against {}".format(TERMINAL_URL), flush=True)

    while True:
        try:
            cookie = _login()
            feed = _TerminalFeed(session, cookie, args.verbose)
            feed.run_forever()
        except Exception as exc:
            print("mainline_terminal connection error: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
            time.sleep(RECONNECT_S)


if __name__ == "__main__":
    main()
