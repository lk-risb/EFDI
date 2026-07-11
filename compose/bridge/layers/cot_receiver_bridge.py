#!/usr/bin/env python3
"""cot_receiver_bridge.py — Inbound CoT TCP bridge → Zenoh.

Receives Cursor-on-Target (CoT) XML from an external source (e.g. Giraffe radar,
another TAK device) and republishes each track as a JSON message on the Zenoh fabric
so cot_layer.py and ATAK pick them up automatically.

Two modes:
  --listen PORT   We open a TCP server. Remote side connects to us.
                  Give the Giraffe crew: <our-netbird-ip>:PORT
                  Our NetBird IP: <POD_NETBIRD_IP>

  --connect IP:PORT  We connect to the remote side. They give us their IP:PORT.

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

    # Connect mode — we connect to Giraffe at their NetBird IP
    venv/bin/python3 cot_receiver_bridge.py --connect 100.x.x.x:8089
"""

import argparse
import json
import os
import socket
import threading
import time
import xml.etree.ElementTree as ET

import zenoh

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = "LTU/CISB/" + ORG   # organization prefix precedes the pod namespace
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

RECONNECT_S = 10
BUFSIZE     = 65536


def _netbird_ip() -> str:
    """Return the NetBird mesh IP (wt0 interface), or fallback to hostname."""
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
    return socket.gethostname()


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


def _parse_cot(xml_str: str) -> dict | None:
    """Parse a CoT XML event into a flat track dict for Zenoh publishing."""
    try:
        root = ET.fromstring(xml_str.strip())
    except ET.ParseError:
        return None
    if root.tag != "event":
        return None

    cot_type = root.get("type", "")
    uid      = root.get("uid", "")
    ts_str   = root.get("time", "")

    point = root.find("point")
    if point is None:
        return None
    try:
        lat = float(point.get("lat", 0))
        lon = float(point.get("lon", 0))
        hae = float(point.get("hae", 9999999))
    except (TypeError, ValueError):
        return None

    detail   = root.find("detail") or ET.Element("detail")
    contact  = detail.find("contact")
    track_el = detail.find("track")
    remarks  = detail.find("remarks")

    callsign = contact.get("callsign", "") if contact is not None else ""
    speed_ms = 0.0
    heading  = 0.0
    if track_el is not None:
        try:
            speed_ms = float(track_el.get("speed", 0))
            heading  = float(track_el.get("course", 0))
        except (TypeError, ValueError):
            pass

    return {
        "_ts":          time.time(),
        "_src":         "cot_rx",
        "cot_type":     cot_type,
        "uid":          uid,
        "callsign":     callsign,
        "lat_deg":      lat,
        "lon_deg":      lon,
        "alt_m":        hae if hae < 9_999_998 else None,
        "speed_ms":     speed_ms,
        "heading_deg":  heading,
        "remarks":      remarks.text if remarks is not None and remarks.text else "",
    }


def _topic(track: dict) -> str:
    aff = _affiliation(track.get("cot_type", "a-u-A"))
    return "{}/air/radar/cot/{}/aircraft/tracks/v1".format(TOPIC_ROOT, aff)


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


def handle_connection(sock: socket.socket, addr, session: "zenoh.Session", verbose: bool):
    print("CoT RX connected: {}".format(addr), flush=True)
    buf = ""
    try:
        while True:
            data = sock.recv(BUFSIZE)
            if not data:
                break
            buf += data.decode("utf-8", errors="replace")
            messages, buf = _split_messages(buf)
            for xml_str in messages:
                track = _parse_cot(xml_str)
                if track is None:
                    continue
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


def run_listen(port: int, session: "zenoh.Session", verbose: bool):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    our_ip = _netbird_ip()
    print("CoT RX listening on 0.0.0.0:{}".format(port), flush=True)
    print("Tell remote: connect to {}:{} (TCP)".format(our_ip, port), flush=True)
    while True:
        try:
            sock, addr = srv.accept()
            t = threading.Thread(target=handle_connection,
                                 args=(sock, addr, session, verbose), daemon=True)
            t.start()
        except OSError as exc:
            print("CoT RX accept error:", exc, flush=True)
            break


def run_connect(host: str, port: int, session: "zenoh.Session", verbose: bool):
    while True:
        print("CoT RX connecting to {}:{}…".format(host, port), flush=True)
        try:
            sock = socket.create_connection((host, port), timeout=10)
            handle_connection(sock, "{}:{}".format(host, port), session, verbose)
        except OSError as exc:
            print("CoT RX connect failed: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
        time.sleep(RECONNECT_S)


def main():
    ap = argparse.ArgumentParser(description="Inbound CoT TCP → Zenoh bridge")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--listen", type=int, metavar="PORT",
                      help="Listen for incoming CoT connections on PORT")
    mode.add_argument("--connect", metavar="IP:PORT",
                      help="Connect to remote CoT source (e.g. 100.x.x.x:8089)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each received CoT track")
    args = ap.parse_args()

    session = zenoh.open(make_config())
    try:
        if args.listen:
            run_listen(args.listen, session, args.verbose)
        else:
            host, port_str = args.connect.rsplit(":", 1)
            run_connect(host, int(port_str), session, args.verbose)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
