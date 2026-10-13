#!/usr/bin/env python3
"""nffi_layer.py - TAK users as an NFFI (STANAG 5527) feed for SitaWare force tracking.

SitaWare Headquarters has an "NFFI and FFI Manager" (Coalition Gateway) that can run as an NFFI client or server. Its "IP1 Client" form
asks for a name, a destination layer and ONE port number (no host, plus a reconnect delay), so that client waits on its own port for
the data to be sent to it: this layer connects out to it (NFFI_LAYER_CONNECT=host:port, and keeps reconnecting). The other way round is
also built, a listening port the SitaWare manager is pointed at (NFFI_LAYER_PORT), for a profile that dials in. Whoever is connected gets
the current tracks at once and then every update, as NFFI XML documents (NFFI_LAYER_VERSION: 1.3 by default, the version SitaWare 6.21 itself writes; namespace
urn:nato:fft:protocols:nffi13 or nffi14, one NFFIMessage with one track each, each behind SitaWare's 16-byte IP1 header); the whole table is re-sent every NFFI_LAYER_REFRESH_S.

What is sent: the friendly unit tracks of TAK clients, which tak_bridge.py publishes under <ORG>/land/tak/**/friendly/unit/** (only a
track that reports its TAK device, so a marker a user placed is not a force track). The NFFI fields follow the NC3A schema:
  trackSource   system EFDI, subsystem TAK, country NFFI_COUNTRY, transponderId the TAK uid
  dateTime      the time of the position report, UTC, 14 digits
  coordinates   latitude, longitude, altitude (m)
  bearing/speed heading, speed in km/h
  unitSymbol    a 15-character APP-6 SIDC (letters, no wildcards): friendly ground unit, Team/Crew echelon, the function of the role
  unitShortName the callsign;  operStatusData/remarks  "<team> <role>"
The feed carries live positions of the TAK users and has no login or encryption (the NFFI profiles do not ask for any here). Connecting
out opens no port here; a listening port should be bound to the VPN address only, with the SitaWare machine in NFFI_LAYER_ALLOW.
The version, namespace and framing were taken from what a live SitaWare 6.21 itself sends: an NFFI IP1 Classic Server with a contract
for this host, read from its port, writes nffi13 documents, each behind a 16-byte header (04 00 00 00, the time in epoch seconds
big-endian, 4 zero bytes, 01 00, the document length big-endian in 2 bytes). A newer SitaWare may write another version: set
NFFI_LAYER_VERSION=1.4 and NFFI_LAYER_FRAMING to match.

Env (at least one of NFFI_LAYER_CONNECT and NFFI_LAYER_PORT is required; the layer does not start without one):
  NFFI_LAYER_CONNECT     host:port of SitaWare's NFFI client to connect out to (comma-separated for several)
  NFFI_LAYER_RECONNECT_S seconds between connection attempts (default 10)
  NFFI_LAYER_PORT        TCP port to listen on, if SitaWare dials in instead
  NFFI_LAYER_BIND        address to listen on (default 127.0.0.1: set the one address SitaWare reaches, e.g. the VPN address)
  NFFI_LAYER_ALLOW       comma-separated source addresses that may connect (default: any that can reach the port)
  NFFI_COUNTRY           3-letter country code in trackSource (default LTU)
  NFFI_LAYER_VERSION     NFFI version of the documents: 1.3 (default, SitaWare 6.21) or 1.4
  NFFI_LAYER_FRAMING     how each document is written on the socket: ip1 (default: SitaWare's 16-byte header in front), xml (XML
                         declaration, newline after), nodecl (no declaration), length (4-byte big-endian length in front) or nul
                         (a 0 byte after)
  NFFI_LAYER_STALE_S     drop a track this long after its last report (default 600)
  NFFI_LAYER_REFRESH_S   re-send every track this often (default 30)
"""

import argparse
import json
import os
import re
import signal
import socket
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from namespace_prefix import topic_root
from protocols.vendors.random import ground_unit
from protocols.vendors.random.gateway import open_session, subscribe

TOPIC_ROOT = topic_root()
NFFI_VERSION = os.environ.get("NFFI_LAYER_VERSION", "1.3").strip()
NFFI_NS = "urn:nato:fft:protocols:nffi" + ("14" if NFFI_VERSION == "1.4" else "13")
COUNTRY = (os.environ.get("NFFI_COUNTRY") or "LTU").strip().upper()[:3]
FRAMING = os.environ.get("NFFI_LAYER_FRAMING", "ip1").strip().lower()
STALE_S = float(os.environ.get("NFFI_LAYER_STALE_S", "600"))
REFRESH_S = float(os.environ.get("NFFI_LAYER_REFRESH_S", "30"))
SYSTEM = "EFDI"
SUBSYSTEM = "TAK"
TOPIC = "{}/land/tak/**".format(TOPIC_ROOT)
_SECURITY = {"secPolicyName": "0", "secClassification": "UNCLASSIFIED", "secCategory": "0"}     # as SitaWare writes them


def _clean(text, allowed: str, limit: int) -> str:
    """Text for an NFFI field: characters outside the schema's pattern become '-', cut to the length limit."""
    return re.sub("[^{}]".format(allowed), "-", str(text or ""))[:limit]


def _sub(parent, name, text=None, **attrs):
    element = ET.SubElement(parent, "{%s}%s" % (NFFI_NS, name), attrs)
    if text is not None:
        element.text = str(text)
    return element


def unit_symbol(track: dict) -> str:
    """A 15-character APP-6 SIDC without wildcards (the schema allows [A-Z0-9-]): friendly ground unit, Team/Crew, function of the role.
    The field cannot hold the numeric APP-6D "dismounted individual" symbol, so a user with no role is infantry, the letter-code
    counterpart of the APP-6D infantry individual (see docs/references/symbology/SYMBOLOGY.md)."""
    function = ground_unit.tak_function(track.get("cot_type"), track.get("tak_role")) or "UCI---"
    return "SFGP{}-{}---".format(function, ground_unit.ECHELON_TEAM)


def track_message(track: dict, now: float | None = None) -> bytes | None:
    """One NFFIMessage with one track for a TAK client's friendly unit track; None when it has no usable position or is not a TAK client."""
    try:
        lat, lon = float(track["lat_deg"]), float(track["lon_deg"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 < lon <= 180) or (lat == 0.0 and lon == 0.0):
        return None
    if not (track.get("tak_platform") or track.get("tak_device")):
        return None
    uid = _clean(track.get("uid"), r"A-Za-z0-9.,:*\-", 60)
    if not uid:
        return None
    when = track.get("cot_time_epoch_s") or track.get("_ts") or now or time.time()
    ET.register_namespace("", NFFI_NS)
    root = ET.Element("{%s}NFFIMessage" % NFFI_NS)
    node = _sub(root, "track")
    positional = _sub(node, "positionalData", **_SECURITY)
    source = _sub(positional, "trackSource")
    system = _sub(source, "sourceSystem")
    if re.fullmatch("[A-Z]{3}", COUNTRY):
        _sub(system, "country", COUNTRY)
    _sub(system, "system", SYSTEM)
    _sub(system, "subsystem", SUBSYSTEM)
    _sub(source, "transponderId", uid)
    _sub(positional, "dateTime", datetime.fromtimestamp(float(when), tz=timezone.utc).strftime("%Y%m%d%H%M%S"))
    coordinates = _sub(positional, "coordinates")
    _sub(coordinates, "latitude", round(lat, 6))
    _sub(coordinates, "longitude", round(lon, 6))
    if track.get("alt_m") is not None:
        try:
            _sub(coordinates, "altitude", round(float(track["alt_m"]), 1))
        except (TypeError, ValueError):
            pass
    try:
        if track.get("heading_deg") is not None:
            _sub(positional, "bearing", round(float(track["heading_deg"]) % 360.0, 1))
        if track.get("speed_ms") is not None:
            _sub(positional, "speed", round(max(0.0, float(track["speed_ms"])) * 3.6, 1))
    except (TypeError, ValueError):
        pass
    ident = _sub(node, "identificationData", **_SECURITY)
    _sub(ident, "unitSymbol", unit_symbol(track))
    name = _clean(track.get("callsign") or track.get("uid"), r"A-Za-z0-9.,:*\- ", 21).strip()
    if name:
        _sub(ident, "unitShortName", name)
    remarks = _clean(" ".join(str(track.get(k) or "") for k in ("tak_team", "tak_role")), r"A-Za-z0-9.,:*\- ", 60).strip()
    if remarks:
        _sub(_sub(node, "operStatusData"), "remarks", remarks)
    body = ET.tostring(root, encoding="utf-8")
    return frame((b"" if FRAMING == "nodecl" else b'<?xml version="1.0" encoding="UTF-8"?>\n') + body + b"\n", now=when)


def ip1_header(message_type: int, length: int, now: float | None = None) -> bytes:
    """The 16-byte header SitaWare 6.21 writes in front of each NFFI (type 0x04) or FFI (type 0x14, ffi_layer.py) document."""
    return (bytes([message_type]) + b"\x00\x00\x00" + int(now or time.time()).to_bytes(4, "big") + b"\x00\x00\x00\x00\x01\x00"
            + length.to_bytes(2, "big"))


def frame(document: bytes, now: float | None = None) -> bytes:
    """One document as it is written on the socket, by NFFI_LAYER_FRAMING."""
    if FRAMING == "ip1":
        return ip1_header(0x04, len(document), now) + document
    if FRAMING == "length":
        return len(document).to_bytes(4, "big") + document
    if FRAMING == "nul":
        return document + b"\x00"
    return document


class NffiFeed:
    """The table of tracks and the clients connected to the listening socket."""

    def __init__(self, builder=None, stale_s: float | None = None):
        self._build = builder or track_message          # ffi_layer.py passes its own message builder
        self._stale_s = STALE_S if stale_s is None else stale_s
        self._tracks: dict = {}                 # uid -> (time last reported, message)
        self._clients: list = []
        self._lock = threading.Lock()

    def update(self, track: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        message = self._build(track, now)
        uid = str(track.get("uid") or "")
        with self._lock:
            if track.get("_delete"):
                self._tracks.pop(uid, None)
                return
            if message is None:
                return
            self._tracks[uid] = (now, message)
        self._send(message)

    def messages(self, now: float | None = None) -> list:
        now = time.time() if now is None else now
        with self._lock:
            for uid in [u for u, (seen, _) in self._tracks.items() if now - seen > self._stale_s]:
                self._tracks.pop(uid)
            return [message for _, message in self._tracks.values()]

    def _send(self, data: bytes) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.sendall(data)
            except OSError:
                self._drop(client)

    def _drop(self, client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        try:
            client.close()
        except OSError:
            pass

    def add_client(self, client) -> None:
        with self._lock:
            self._clients.append(client)
        for message in self.messages():
            try:
                client.sendall(message)
            except OSError:
                self._drop(client)
                return

    def refresh(self) -> None:
        for message in self.messages():
            self._send(message)

    def connect_out(self, host: str, port: int, stop: threading.Event, retry_s: float = 10.0):
        """Keep a connection to host:port (SitaWare's NFFI client): send the table when it is made and then every update."""
        def loop():
            while not stop.is_set():
                try:
                    client = socket.create_connection((host, port), timeout=10)
                except OSError as exc:
                    print("NFFI connect to {}:{} failed: {} - retry in {:.0f}s".format(host, port, exc, retry_s), flush=True)
                    stop.wait(retry_s)
                    continue
                print("NFFI connected to {}:{}".format(host, port), flush=True)
                self.add_client(client)
                while not stop.is_set():                  # until the peer closes: a read that returns nothing
                    try:
                        client.settimeout(1.0)
                        if client.recv(4096) == b"":
                            break
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                self._drop(client)
                print("NFFI connection to {}:{} lost - reconnecting".format(host, port), flush=True)
                stop.wait(retry_s)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def serve(self, host: str, port: int, stop: threading.Event, allow=None):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        server.settimeout(1.0)

        def accept():
            while not stop.is_set():
                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                if allow and address[0] not in allow:
                    print("NFFI connection from {} refused (not in the allowed sources)".format(address[0]), flush=True)
                    client.close()
                    continue
                print("NFFI client connected: {}:{}".format(*address[:2]), flush=True)
                self.add_client(client)
            server.close()
        threading.Thread(target=accept, daemon=True).start()
        return server


def run(prefix: str = "NFFI_LAYER", builder=None, label: str = "NFFI", description: str = "TAK users -> NFFI feed for SitaWare force tracking") -> None:
    """The layer's main loop; ffi_layer.py calls it with its own env prefix, message builder and label."""
    env = lambda name, default="": os.environ.get("{}_{}".format(prefix, name), default)
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--connect", action="append", default=[h for h in env("CONNECT").replace(" ", "").split(",") if h],
                        help="host:port of SitaWare's client to connect out to (repeatable)")
    parser.add_argument("--port", type=int, default=int(env("PORT") or 0))
    parser.add_argument("--bind", default=env("BIND", "127.0.0.1"))
    args = parser.parse_args()
    if not args.port and not args.connect:
        raise SystemExit("{0}_CONNECT (host:port) or {0}_PORT is required".format(prefix))
    allow = {a.strip() for a in env("ALLOW").split(",") if a.strip()}
    feed, stop = NffiFeed(builder, float(env("STALE_S", "600"))), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("{} layer Zenoh connect failed: {} - retry in 10s".format(label, exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(track, dict) and "/friendly/" in str(sample.key_expr):
            feed.update(track)

    subscribe(session, TOPIC, on_sample)
    retry_s = float(env("RECONNECT_S", "10"))
    for target in args.connect:
        host, _, port = target.rpartition(":")
        if host and port.isdigit():
            feed.connect_out(host, int(port), stop, retry_s)
            print("{} feed connects out to {}:{} (TAK users from {})".format(label, host, port, TOPIC), flush=True)
        else:
            print("{}_CONNECT entry {!r} is not host:port, ignored".format(prefix, target), flush=True)
    if args.port:
        feed.serve(args.bind, args.port, stop, allow)
        print("{} feed listening on {}:{} (TAK users from {}); allowed sources: {}".format(
            label, args.bind, args.port, TOPIC, ", ".join(sorted(allow)) or "any that reach the port"), flush=True)
    try:
        while not stop.wait(float(env("REFRESH_S", "30"))):
            feed.refresh()
    finally:
        session.close()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
