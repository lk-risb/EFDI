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
means adding one more block like the two here, not a new mechanism.

Currently covers:
  - Emergency squawk (ICAO Annex 10: 7500 hijack, 7600 comms failure,
    7700 mayday) on air tracks.
  - Ship distress (AIS nav_status: aground, not under command) on sea tracks.
"""

import argparse
import json
import math
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

_EMERGENCY_SQUAWK = {"7500": "HIJACK", "7600": "COMMS FAILURE", "7700": "MAYDAY"}
_DISTRESS_NAV = frozenset({"aground", "not_under_command", "not under command"})

# Raw pre-fusion radar tracks are duplicates of what fusion will shortly
# publish under air/trackfusion/fused/** — alerting on both would fire twice
# for the same aircraft. Same filter tak_layer.py uses for the same reason.
_RAW_SENSOR_SOURCE_PREFIXES = ("ASTERIX CAT-48", "ASTERIX CAT-20")

# Views that carry the same object as the flat JSON and must not be processed
# twice. Anything else — including a bare topic with no view suffix — is
# treated as the JSON payload.
_NON_JSON_VIEWS = frozenset({"sapient", "proto", "raw"})

_alert_lock = threading.Lock()
_alerted: set = set()   # uids currently in a known emergency/distress state (no re-alert)


def _terminal_view(key: str) -> str:
    """The view segment, ignoring a trailing /tracks/vN version tail."""
    parts = key.split("/")
    if (len(parts) >= 2 and parts[-2] == "tracks"
            and parts[-1][:1] == "v" and parts[-1][1:].isdigit()):
        parts = parts[:-2]
    return parts[-1] if parts else ""


def _is_unfused_sensor_track(track: dict, key: str) -> bool:
    """True for a raw radar/MLAT track that must first pass through fusion."""
    if "/fused/" in key:
        return False
    source = str(track.get("_src", ""))
    return any(source.startswith(prefix) for prefix in _RAW_SENSOR_SOURCE_PREFIXES)


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


def _hae_ft(track: dict) -> int | None:
    for key, scale in (
        ("geo_alt_m",   1.0 / 0.3048), ("alt_geom_ft", 1.0),
        ("baro_alt_m",  1.0 / 0.3048), ("alt_baro_ft", 1.0),
        ("alt_3d_ft",   1.0), ("mode_c_alt_ft", 1.0),
        ("alt_ft",      1.0), ("alt_m", 1.0 / 0.3048), ("alt_km", 1000.0 / 0.3048),
    ):
        v = track.get(key)
        if v is not None:
            try:
                number = float(v) * scale
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and number != 0:
                return int(number)
    return None


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
    """True the first time this uid enters an emergency/distress state; False
    on every subsequent update while it stays in that state."""
    with _alert_lock:
        fire = uid not in _alerted
        _alerted.add(uid)
    return fire


def make_squawk_handler(sender, verbose: bool):
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
        if _is_unfused_sensor_track(track, key):
            return
        uid = _uid(track)
        sq = str(track.get("squawk") or "")
        if sq not in _EMERGENCY_SQUAWK:
            _clear_alert(uid)
            return
        if not _fire_once(uid):
            return
        lat = track.get("lat_deg", 0)
        lon = track.get("lon_deg", 0)
        cs  = (track.get("callsign") or track.get("registration") or
               track.get("icao24") or "UNKNOWN").upper()
        alt_ft = _hae_ft(track)
        fl = "{:03d}".format(alt_ft // 100) if alt_ft is not None else "UNKNOWN"
        msg = "[{}] {} {} - squawk {} - FL{} - {:.3f}/{:.3f}".format(
            _EMERGENCY_SQUAWK[sq], sq, cs, sq, fl, lat, lon)
        _send_geochat_alert(sender, uid, lat, lon, msg)
        if verbose:
            print("ALERT {}".format(msg), flush=True)
    return handler


def make_distress_handler(sender, verbose: bool):
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
        uid = _uid(track)
        nav_key = str(track.get("nav_status") or "").lower().replace(" ", "_")
        if nav_key not in _DISTRESS_NAV:
            _clear_alert(uid)
            return
        if not _fire_once(uid):
            return
        lat  = track.get("lat_deg", 0)
        lon  = track.get("lon_deg", 0)
        name = (track.get("ship_name") or str(track.get("mmsi") or "VESSEL")).upper()
        msg  = "[SOS] {} - {} - MMSI {} - {:.3f}/{:.3f}".format(
            nav_key.upper().replace("_", " "), name, track.get("mmsi", "?"), lat, lon)
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
        subscribe(session, "{}/air/**".format(TOPIC_ROOT), make_squawk_handler(sender, args.verbose)),
        subscribe(session, "{}/sea/**".format(TOPIC_ROOT), make_distress_handler(sender, args.verbose)),
    ]
    print("SUB {}/air/** → emergency squawk alerts".format(TOPIC_ROOT), flush=True)
    print("SUB {}/sea/** → ship distress alerts".format(TOPIC_ROOT), flush=True)

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
        description="Zenoh tracks → TAK Server GeoChat emergency/status alerts")
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
