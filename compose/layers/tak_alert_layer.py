#!/usr/bin/env python3
"""Emergency/status GeoChat broadcasts → TAK Server ("All Chat Rooms").

Standalone from layers/tak_layer.py on purpose: broadcasting a chat popup to
every connected TAK client is a different concern from translating a track to
its own CoT event, and needs its own on/off switch — this is a separate
service (start.sh's tak-alert-layer), not selected by default, so it only
runs when explicitly started. Uses its own TAK connection (same TAK_HOST/
TAK_PORT/TAK_TLS config as tak_layer.py's egress link — see main() below),
completely independent of tak_layer's own subscribers and connection.

Groundwork for more emergency/status conditions later: every condition below
follows the same shape — subscribe to the tracks that can carry it, check a
per-track field, dedupe per-uid so a standing condition alerts once instead
of on every update, and call _send_geochat_alert(). Adding a new condition
means adding one more block like this one, not a new mechanism.

Currently covers:
  - Acoustic sensor detection (dronuradaras.lt) on land tracks — same
    last_detection_ts tak_layer.py uses to recolor the sensor's own marker
    (see its _sensor_alert_cot_type); this is the separate GeoChat popup.
"""

import argparse
import json
import os
import signal
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from namespace_prefix import topic_root
from protocols.gateway import open_session, subscribe
from protocols.tak_transport import TcpSender, RECONNECT_S

TOPIC_ROOT = topic_root()

# Matches tak_layer.py's own _SENSOR_ALERT_HOT_S — "active" window for the
# same acoustic detection, kept in sync by comment since these two layers
# have no shared import path for it (see module docstring).
_ACOUSTIC_ALERT_HOT_S = 60

# Views that carry the same object as the flat JSON and must not be processed
# twice. Anything else — including a bare topic with no view suffix — is
# treated as the JSON payload.
_NON_JSON_VIEWS = frozenset({"sapient", "proto", "raw"})

_alert_lock = threading.Lock()
_alerted: set = set()   # uids currently in a known detection state (no re-alert)


def _terminal_view(key: str) -> str:
    """The view segment, ignoring a trailing /tracks/vN version tail."""
    parts = key.split("/")
    if (len(parts) >= 2 and parts[-2] == "tracks"
            and parts[-1][:1] == "v" and parts[-1][1:].isdigit()):
        parts = parts[:-2]
    return parts[-1] if parts else ""


def _uid(track: dict) -> str:
    # Same stable, source-agnostic identifier tak_layer.py uses, so the same
    # aircraft/vessel dedupes to one alert state regardless of which layer
    # last touched it.
    for key, id_prefix in (
        ("icao24",    "ICAO"),
        ("mmsi",      "MMSI"),
        ("sat_id",    "SAT"),
        ("radar_id",  "RAD"),
        ("sensor_id", "SENS"),
        ("uid",       "UID"),
    ):
        v = track.get(key)
        if v:
            return "EFDI-{}-{}".format(id_prefix, str(v).upper())
    src = track.get("_src", "efdi")
    cs = (track.get("callsign") or "").strip()
    if cs:
        return "EFDI-{}-{}".format(src, cs)
    return "EFDI-{}-{:.5f}-{:.5f}".format(src, track.get("lat_deg", 0), track.get("lon_deg", 0))


def _ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _send_geochat_alert(sender, uid: str, lat: float, lon: float, message: str):
    """Send a GeoChat broadcast to All Chat Rooms — shows as a popup on every ATAK device."""
    now    = time.time()
    msg_id = "{}-ALERT-{:.0f}".format(uid, now)
    event  = ET.Element("event", {
        "version": "2.0",
        "uid":     "GeoChat.EFDI.ALL.{}".format(msg_id),
        "type":    "b-t-f",
        "how":     "m-g",
        "time":    _ts(now), "start": _ts(now), "stale": _ts(now + 300),
    })
    ET.SubElement(event, "point", {
        "lat": str(round(lat, 6)), "lon": str(round(lon, 6)),
        "hae": "9999999.0", "ce": "9999999.0", "le": "9999999.0",
    })
    detail = ET.SubElement(event, "detail")
    chat = ET.SubElement(detail, "__chat", {
        "parent": "TeamTalk", "groupOwner": "false",
        "messageId": msg_id, "chatroom": "All Chat Rooms",
        "id": "All Chat Rooms", "senderCallsign": "EFDI-ALERT",
    })
    ET.SubElement(chat, "chatgrp", {
        "uid0": "EFDI-ALERT", "uid1": "All Chat Rooms", "id": "All Chat Rooms",
    })
    ET.SubElement(detail, "link", {"uid": "EFDI-ALERT", "type": "a-n-G-I", "relation": "p-p"})
    ET.SubElement(detail, "remarks", {
        "source": "EFDI-ALERT", "to": "All Chat Rooms", "time": _ts(now),
    }).text = message
    ET.SubElement(detail, "__serverdestination", {"destinations": "All Chat Rooms"})
    sender.send('<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(event, encoding="unicode"))


def _clear_alert(uid: str) -> None:
    with _alert_lock:
        _alerted.discard(uid)


def _fire_once(uid: str) -> bool:
    """True the first time this uid enters a detection state; False on every
    subsequent update while it stays in that state."""
    with _alert_lock:
        fire = uid not in _alerted
        _alerted.add(uid)
    return fire


def make_acoustic_handler(sender, verbose: bool):
    def handler(sample):
        key = str(sample.key_expr)
        if _terminal_view(key) in _NON_JSON_VIEWS:
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        if track.get("_ingress") == "tak_server":
            return
        if track.get("sensor_type") != "acoustic":
            return
        uid = _uid(track)
        last_det = track.get("last_detection_ts")
        age = time.time() - float(last_det) if last_det else None
        if age is None or age > _ACOUSTIC_ALERT_HOT_S:
            _clear_alert(uid)
            return
        if not _fire_once(uid):
            return
        lat  = track.get("lat_deg", 0)
        lon  = track.get("lon_deg", 0)
        name = (track.get("sensor_name") or track.get("sensor_id") or "SENSOR").upper()
        msg  = "[DRONE DETECTED] {} - {:.0f}s ago - {:.3f}/{:.3f}".format(name, age, lat, lon)
        _send_geochat_alert(sender, uid, lat, lon, msg)
        if verbose:
            print("ALERT {}".format(msg), flush=True)
    return handler


def run(args):
    hosts = [(h, args.port) for h in args.host]
    tls = getattr(args, "tls", False)
    if tls and not getattr(args, "ca", None):
        raise SystemExit("--ca / TAK_CA is required when --tls is specified")
    sender = TcpSender(hosts,
                       tls=tls,
                       certfile=getattr(args, "cert", None),
                       keyfile=getattr(args, "key", None),
                       cafile=getattr(args, "ca", None),
                       server_name=getattr(args, "tls_server_name", None))
    mode = "TLS mTLS" if tls else "TCP plaintext"
    print("GeoChat alerts → {} candidates: {} (TAK Server, first reachable wins)".format(
        mode, ", ".join("{}:{}".format(h, p) for h, p in hosts)), flush=True)

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("tak_alert_layer Zenoh connect failed: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
            time.sleep(RECONNECT_S)

    subs = [
        subscribe(session, "{}/land/**".format(TOPIC_ROOT), make_acoustic_handler(sender, args.verbose)),
    ]
    print("SUB {}/land/** → acoustic sensor detection alerts".format(TOPIC_ROOT), flush=True)

    stop = threading.Event()

    def _on_signal(signum, _frame):
        print("shutting down on {}".format(signal.Signals(signum).name), flush=True)
        stop.set()

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _on_signal)
        except (OSError, ValueError):
            pass

    print("tak_alert_layer running — Ctrl-C to stop", flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        print("shutting down on KeyboardInterrupt", flush=True)
    finally:
        for sub in subs:
            sub.undeclare()
        session.close()
        sender.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Zenoh tracks → TAK Server GeoChat acoustic sensor detection alerts")
    ap.add_argument("--host", action="append", default=None,
                    help="TAK Server host — repeatable, same convention as tak_layer.py; "
                         "falls back to TAK_HOST/TAK_HOST_FALLBACK/TAK_HOST_TAILSCALE env or 127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TAK_PORT", "8089")),
                    help="TAK Server port (default: 8089, same egress port tak_layer.py uses)")
    ap.add_argument("--tls", action="store_true",
                    default=os.environ.get("TAK_TLS", "") == "1",
                    help="Enable mutual TLS (requires --cert, --key, --ca)")
    ap.add_argument("--cert", default=os.environ.get("TAK_CERT"))
    ap.add_argument("--key", default=os.environ.get("TAK_KEY"))
    ap.add_argument("--ca", default=os.environ.get("TAK_CA"))
    ap.add_argument("--tls-server-name", default=os.environ.get("TAK_TLS_SERVER_NAME"))
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each GeoChat alert sent")
    args = ap.parse_args(argv)
    if not args.host:
        args.host = [
            host
            for host in (
                os.environ.get("TAK_HOST", "").strip(),
                os.environ.get("TAK_HOST_FALLBACK", "").strip(),
                os.environ.get("TAK_HOST_TAILSCALE", "").strip(),
            )
            if host
        ] or ["127.0.0.1"]
    run(args)


if __name__ == "__main__":
    main()
