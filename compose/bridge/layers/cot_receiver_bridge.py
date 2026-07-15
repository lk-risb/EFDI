#!/usr/bin/env python3
"""cot_receiver_bridge.py — Inbound CoT TCP/TLS bridge → Zenoh.

Receives Cursor-on-Target (CoT) XML from an external source (e.g. Giraffe radar,
another TAK device) and republishes each track as a JSON message on the Zenoh fabric
so cot_layer.py and ATAK pick them up automatically.

Two modes:
  --listen PORT   We open a TCP server. Remote side connects to us.
                  Give the Giraffe crew: <our-netbird-ip>:PORT
                  Our NetBird IP: <POD_NETBIRD_IP>

  --connect IP:PORT  We connect to the remote side. Mutual TLS and TAK-user-only
                     filtering turn this into a secure TAK Server SA client.

Tracks are published to:
  <ORG>/air/radar/cot/<affiliation>/aircraft/tracks/v1

CoT affiliation → Zenoh affiliation:
  a-f-*  →  friendly
  a-h-*  →  hostile
  a-n-*  →  neutral
  a-u-*  →  unknown   (default for radar returns)

Run:
    # Listen mode — Giraffe connects to us on port 8088
    venv/bin/python3 cot_receiver_bridge.py --listen 8088

    # Plain TCP source
    venv/bin/python3 cot_receiver_bridge.py --connect 100.x.x.x:8089

    # TAK Server mTLS — receive ground-user situational-awareness positions
    venv/bin/python3 cot_receiver_bridge.py --connect tak.example:8089 --tls \
      --cert cert.pem --key key.pem --ca ca.pem --tak-users-only
"""

import argparse
import json
import math
import os
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone

import defusedxml.ElementTree as ET
import zenoh
from namespace_prefix import prefix

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG   # org prefix (configurable) precedes the pod namespace
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

RECONNECT_S = 10
BUFSIZE     = 65536


def _netbird_ip() -> str | None:
    """Return the NetBird mesh IP (wt0 interface), or None if not found."""
    for iface in ("wt0", "netbird0"):
        try:
            import subprocess
            out = subprocess.check_output(
                ["ip", "-4", "addr", "show", iface],
                stderr=subprocess.DEVNULL, text=True)
            for tok in out.split():
                if "/" in tok and tok[0].isdigit():
                    return tok.split("/")[0]
        except Exception:
            pass
    return None


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


def _affiliation(cot_type: str) -> str:
    """Extract affiliation slug from CoT type string for Zenoh topic."""
    parts = cot_type.split("-")
    aff = parts[1] if len(parts) > 1 else "u"
    return {"f": "friendly", "h": "hostile", "n": "neutral", "u": "unknown"}.get(aff, "unknown")


def _bounded_attr(element, name: str, limit: int = 128) -> str:
    if element is None:
        return ""
    value = element.get(name, "")
    return value[:limit] if isinstance(value, str) else ""


def _optional_accuracy(point, name: str) -> float | None:
    try:
        value = float(point.get(name, ""))
    except (TypeError, ValueError):
        return None
    if math.isfinite(value) and 0 <= value < 9_999_998:
        return value
    return None


def _parse_cot(xml_str: str) -> dict | None:
    """Parse a CoT XML event into a flat track dict for Zenoh publishing."""
    try:
        root = ET.fromstring(xml_str.strip())
    except (ET.ParseError, ValueError):
        # defusedxml raises DefusedXmlException (a ValueError subclass, not
        # ET.ParseError) for entity-expansion/external-reference attacks —
        # this listener accepts XML from an unauthenticated network peer, so
        # both malformed XML and a malicious payload must fail the same way.
        return None
    if root.tag != "event":
        return None

    cot_type = root.get("type", "")
    uid      = root.get("uid", "")
    ts_str   = root.get("time", "")
    if not (isinstance(uid, str) and 1 <= len(uid) <= 256):
        return None
    if not (isinstance(cot_type, str) and 1 <= len(cot_type) <= 128 and
            re.fullmatch(r"[A-Za-z0-9_.-]+", cot_type)):
        return None

    point = root.find("point")
    if point is None:
        return None
    try:
        lat = float(point.get("lat", 0))
        lon = float(point.get("lon", 0))
        hae = float(point.get("hae", 9999999))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (lat, lon, hae)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    received_ts = time.time()
    try:
        event_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=timezone.utc)
        source_ts = event_ts.timestamp()
        if not math.isfinite(source_ts):
            source_ts = received_ts
    except (TypeError, ValueError, OverflowError):
        source_ts = received_ts

    detail = root.find("detail")
    if detail is None:
        detail = ET.Element("detail")
    contact  = detail.find("contact")
    track_el = detail.find("track")
    remarks  = detail.find("remarks")
    takv = detail.find("takv")
    group = detail.find("__group")
    if group is None:
        group = detail.find("group")
    status = detail.find("status")
    precision = detail.find("precisionlocation")

    callsign = _bounded_attr(contact, "callsign")
    speed_ms = 0.0
    heading  = 0.0
    if track_el is not None:
        try:
            speed_ms = float(track_el.get("speed", 0))
            heading  = float(track_el.get("course", 0))
        except (TypeError, ValueError):
            pass
    if not math.isfinite(speed_ms) or speed_ms < 0:
        speed_ms = 0.0
    if not math.isfinite(heading):
        heading = 0.0
    heading %= 360.0

    result = {
        "_ts":          source_ts,
        "_received_ts": received_ts,
        "_src":         "cot_rx",
        "cot_type":     cot_type,
        "uid":          uid,
        "callsign":     callsign,
        "lat_deg":      lat,
        "lon_deg":      lon,
        "alt_m":        hae if hae < 9_999_998 else None,
        "speed_ms":     speed_ms,
        "heading_deg":  heading,
        "remarks":      remarks.text[:4096] if remarks is not None and remarks.text else "",
        "tak_user":     cot_type.split("-")[2:3] == ["G"] and (takv is not None or group is not None),
    }
    optional = {
        "team": _bounded_attr(group, "name", 64),
        "role": _bounded_attr(group, "role", 64),
        "tak_device": _bounded_attr(takv, "device"),
        "tak_platform": _bounded_attr(takv, "platform"),
        "tak_os": _bounded_attr(takv, "os", 64),
        "tak_version": _bounded_attr(takv, "version", 64),
        "position_source": _bounded_attr(precision, "geopointsrc", 64),
        "altitude_source": _bounded_attr(precision, "altsrc", 64),
        "ce_m": _optional_accuracy(point, "ce"),
        "le_m": _optional_accuracy(point, "le"),
    }
    for key, value in optional.items():
        if value not in (None, ""):
            result[key] = value
    battery = _bounded_attr(status, "battery", 16)
    if battery:
        try:
            battery_value = float(battery)
        except ValueError:
            pass
        else:
            if math.isfinite(battery_value) and 0 <= battery_value <= 100:
                result["battery_pct"] = round(battery_value)
    return result


def _should_publish(track: dict, tak_users_only: bool) -> bool:
    """Reject our own round-tripped tracks and optional non-user TAK traffic."""
    if str(track.get("uid", "")).upper().startswith("EFDI-"):
        return False
    return not tak_users_only or track.get("tak_user") is True


def _topic(track: dict) -> str:
    aff = _affiliation(track.get("cot_type", "a-u-A"))
    parts = str(track.get("cot_type", "a-u-A")).split("-")
    dimension = parts[2].upper() if len(parts) > 2 else "A"
    domain, entity = {
        "A": ("air", "aircraft"),
        "G": ("land", "unit"),
        "S": ("sea", "vessel"),
        "U": ("sea", "vessel"),
        "P": ("space", "satellite"),
    }.get(dimension, ("air", "aircraft"))
    return "{}/{}/radar/cot/{}/{}/tracks/v1".format(TOPIC_ROOT, domain, aff, entity)


def _split_messages(buf: str) -> tuple[list[str], str]:
    """Split a buffer of concatenated CoT XML into complete messages + remainder."""
    messages = []
    while True:
        start = buf.find("<?xml")
        if start == -1:
            start = buf.find("<event")
        if start == -1:
            break
        end = buf.find("</event>", start)
        if end == -1:
            break
        messages.append(buf[start:end + 8])
        buf = buf[end + 8:]
    return messages, buf


def handle_connection(
    sock: socket.socket,
    addr,
    session: "zenoh.Session",
    verbose: bool,
    tak_users_only: bool = False,
    source: str = "cot_rx",
):
    print("CoT RX connected: {}".format(addr), flush=True)
    buf = ""
    try:
        while True:
            data = sock.recv(BUFSIZE)
            if not data:
                break
            buf += data.decode("utf-8", errors="replace")
            if len(buf) > 10_000_000:
                print("CoT RX connection closed: incomplete message exceeds 10 MB", flush=True)
                break
            messages, buf = _split_messages(buf)
            for xml_str in messages:
                track = _parse_cot(xml_str)
                if track is None or not _should_publish(track, tak_users_only):
                    continue
                track["_src"] = source
                track["_ingress"] = "tak_server" if tak_users_only else "cot_source"
                topic = _topic(track)
                session.put(topic, json.dumps(track).encode(),
                            encoding=zenoh.Encoding.APPLICATION_JSON)
                if verbose:
                    print("RX {} {} → {}".format(
                        track.get("cot_type", "?"),
                        track.get("callsign") or track.get("uid", "?"),
                        topic), flush=True)
    except OSError as exc:
        print("CoT RX connection error ({}): {}".format(addr, exc), flush=True)
    finally:
        sock.close()
        print("CoT RX disconnected: {}".format(addr), flush=True)


def run_listen(
    port: int,
    session: "zenoh.Session",
    verbose: bool,
    requested_bind: str = "",
    source: str = "cot_rx",
    allowed_peer: str = "",
):
    our_ip = _netbird_ip()
    # Bind the NetBird mesh IP specifically, not 0.0.0.0 — this listener has no
    # auth of its own (see module docstring: any TCP peer that connects gets its
    # CoT accepted as a genuine track), so binding every interface would also
    # accept connections over the pod's LAN/public IP, not just the intended
    # NetBird tunnel. An explicit --bind/COT_RX_BIND can opt into another
    # interface; the safe automatic fallback is loopback.
    bind_ip = requested_bind or our_ip or "127.0.0.1"
    if our_ip is None and not requested_bind:
        print("CoT RX WARNING: NetBird interface (wt0/netbird0) not found — "
              "listening on 127.0.0.1 only; pass --bind explicitly to expose "
              "another interface", flush=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, port))
    srv.listen(5)
    print("CoT RX listening on {}:{}".format(bind_ip, port), flush=True)
    print("Tell remote: connect to {}:{} (TCP)".format(our_ip or socket.gethostname(), port), flush=True)
    while True:
        try:
            sock, addr = srv.accept()
            if allowed_peer and addr[0] != allowed_peer:
                print("CoT RX rejected untrusted peer {}".format(addr[0]), flush=True)
                sock.close()
                continue
            t = threading.Thread(target=handle_connection,
                                 args=(sock, addr, session, verbose, False, source), daemon=True)
            t.start()
        except OSError as exc:
            print("CoT RX accept error:", exc, flush=True)
            break


def run_connect(
    host: str,
    port: int,
    session: "zenoh.Session",
    verbose: bool,
    tls_context: ssl.SSLContext | None = None,
    server_name: str = "",
    tak_users_only: bool = False,
    source: str = "cot_rx",
):
    while True:
        mode = "TAK TLS" if tls_context else "CoT TCP"
        print("{} RX connecting to {}:{}…".format(mode, host, port), flush=True)
        try:
            raw = socket.create_connection((host, port), timeout=10)
            if tls_context:
                sock = tls_context.wrap_socket(raw, server_hostname=server_name or host)
            else:
                sock = raw
            sock.settimeout(None)
            handle_connection(
                sock,
                "{}:{}".format(host, port),
                session,
                verbose,
                tak_users_only,
                source,
            )
        except OSError as exc:
            print("CoT RX connect failed: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
        time.sleep(RECONNECT_S)


def _make_tls_context(certfile: str, keyfile: str, cafile: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)
    return context


def main():
    ap = argparse.ArgumentParser(description="Inbound CoT TCP → Zenoh bridge")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--listen", type=int, metavar="PORT",
                      help="Listen for incoming CoT connections on PORT")
    mode.add_argument("--connect", metavar="IP:PORT",
                      help="Connect to remote CoT source (e.g. 100.x.x.x:8089)")
    ap.add_argument("--bind", default=os.environ.get("COT_RX_BIND", ""),
                    help="Listen address override (default: NetBird IP, else 127.0.0.1)")
    ap.add_argument("--tls", action=argparse.BooleanOptionalAction,
                    default=os.environ.get("COT_RX_TLS", "") == "1",
                    help="Use a mutually authenticated TLS client connection")
    ap.add_argument("--cert", default=os.environ.get("COT_RX_CERT") or os.path.join(
        _CERT_DIR, "tak", "cert.pem"
    ), help="TAK client certificate PEM")
    ap.add_argument("--key", default=os.environ.get("COT_RX_KEY") or os.path.join(
        _CERT_DIR, "tak", "key.pem"
    ), help="TAK client private key PEM")
    ap.add_argument("--ca", default=os.environ.get("COT_RX_CA") or os.path.join(
        _CERT_DIR, "tak", "ca.pem"
    ), help="TAK Server CA PEM")
    ap.add_argument("--server-name", default=os.environ.get("COT_RX_SERVER_NAME", ""),
                    help="TLS server certificate DNS name when --connect uses an IP")
    ap.add_argument("--tak-users-only", action=argparse.BooleanOptionalAction,
                    default=os.environ.get("COT_RX_TAK_USERS_ONLY", "") == "1",
                    help="Import only TAK ground-user SA events and mark them loop-protected")
    ap.add_argument("--source", default=os.environ.get("COT_RX_SOURCE", "cot_rx"),
                    help="Safe source label stored in Zenoh track records")
    ap.add_argument("--allow-peer", default=os.environ.get("COT_RX_ALLOW_PEER", ""),
                    help="Listener mode: accept only this source IP")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each received CoT track")
    args = ap.parse_args()

    if args.tls and args.listen:
        raise SystemExit("--tls is supported only with outbound --connect mode")
    if not re.fullmatch(r"[a-z0-9_.-]{1,64}", args.source):
        raise SystemExit("--source must match [a-z0-9_.-]{1,64}")
    tls_context = None
    if args.tls:
        try:
            tls_context = _make_tls_context(args.cert, args.key, args.ca)
        except (OSError, ssl.SSLError) as exc:
            raise SystemExit("Unable to load CoT RX TLS credentials: {}".format(exc)) from exc

    session = zenoh.open(make_config())
    try:
        if args.listen:
            run_listen(
                args.listen,
                session,
                args.verbose,
                args.bind,
                args.source,
                args.allow_peer,
            )
        else:
            host, port_str = args.connect.rsplit(":", 1)
            run_connect(
                host,
                int(port_str),
                session,
                args.verbose,
                tls_context,
                args.server_name,
                args.tak_users_only,
                args.source,
            )
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
