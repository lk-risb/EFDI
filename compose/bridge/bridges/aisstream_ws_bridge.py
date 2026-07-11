#!/usr/bin/env python3
"""ais_bridge.py — aisstream.io → Zenoh bridge.

Streams live AIS vessel positions from aisstream.io (free WebSocket API)
and publishes each vessel track as JSON to the EFDI Zenoh fabric.

No extra dependencies — WebSocket is implemented using stdlib ssl + socket.

Default coverage: worldwide (all oceans/seas). Narrow with --bbox if needed.
Free-tier note: aisstream.io free plan is rate-limited; worldwide gives high
volume — expect hundreds of messages per second during busy periods.

Zenoh topic:  <ORG>/ais/aisstream/tracks/v1
Proto schema: ais_track.proto  (message AisTrack, package ltu.cis.tracks.v1)

Run:
    venv/bin/python3 ais_bridge.py --apikey <key>
    AISSTREAM_KEY=<key> venv/bin/python3 ais_bridge.py
    # Baltic Sea only:
    venv/bin/python3 ais_bridge.py --bbox '[[53.5,9.0],[66.0,30.0]]' --apikey <key>
"""

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import time

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = "LTU/CISB/" + ORG   # organization prefix precedes the pod namespace
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

AISSTREAM_HOST = "stream.aisstream.io"
AISSTREAM_PATH = "/v0/stream"

# Two-polygon coverage: Baltic/North Sea + Mediterranean/Middle East
# aisstream accepts a list of bounding boxes  [[sw_lat,sw_lon],[ne_lat,ne_lon]]
DEFAULT_BBOX = [[[41, 14], [62, 35]], [[41, 30], [55, 45]]]

RECONNECT_DELAY_S = 10.0

_NAV_STATUS = {
    0: "under_way_engine", 1: "at_anchor", 2: "not_under_command",
    3: "restricted_manoeuvrability", 4: "constrained_by_draught",
    5: "moored", 6: "aground", 7: "engaged_in_fishing",
    8: "under_way_sailing", 15: "not_defined",
}

# AIS ship type codes → readable label (class groups via //10)
_SHIP_TYPE_CLASS = {
    2: "WIG", 3: "Fishing / Towing", 4: "High Speed",
    5: "Special craft", 6: "Passenger", 7: "Cargo",
    8: "Tanker", 9: "Other",
}
_SHIP_TYPE_EXACT = {
    30: "Fishing", 31: "Towing", 32: "Towing (large)", 36: "Sailing",
    37: "Pleasure craft", 50: "Pilot vessel", 51: "Search & Rescue",
    52: "Tug", 53: "Port tender", 54: "Anti-pollution", 55: "Law enforcement",
    58: "Medical transport", 59: "Non-combatant", 79: "Cargo (hazardous)",
    89: "Tanker (hazardous)",
}

def _ship_type_label(code: int) -> str:
    if code in _SHIP_TYPE_EXACT:
        return _SHIP_TYPE_EXACT[code]
    cls = _SHIP_TYPE_CLASS.get(code // 10)
    return "{} ({})".format(cls, code) if cls else "Type {}".format(code)

# Static data cache: MMSI → enrichment dict (from AIS Type 5 / Type 24)
_STATIC: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Minimal stdlib WebSocket client
# ---------------------------------------------------------------------------

def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def ws_connect(host: str, path: str) -> ssl.SSLSocket:
    key = base64.b64encode(os.urandom(16)).decode()
    ctx = ssl.create_default_context()
    raw = socket.create_connection((host, 443), timeout=30)
    sock = ctx.wrap_socket(raw, server_hostname=host)
    sock.settimeout(60.0)

    req = (
        "GET {} HTTP/1.1\r\n"
        "Host: {}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: {}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).format(path, host, key)
    sock.sendall(req.encode())

    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)
    if b"101" not in resp:
        raise ConnectionError("WebSocket upgrade failed: " + resp[:200].decode(errors="replace"))
    return sock


def ws_send(sock: ssl.SSLSocket, text: str):
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n <= 125:
        header = bytes([0x81, 0x80 | n])
    elif n <= 65535:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    sock.sendall(header + mask + masked)


def ws_recv(sock: ssl.SSLSocket) -> str | None:
    """Read one WebSocket frame; return text payload or None (ping/non-text)."""
    h = _recv_exact(sock, 2)
    opcode = h[0] & 0x0F
    masked = bool(h[1] & 0x80)
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask_key = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, n)
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        raise EOFError("server sent close frame")
    if opcode == 0x9:  # ping → pong
        sock.sendall(b"\x8A\x00")
        return None
    if opcode in (0x1, 0x2):
        return payload.decode("utf-8", errors="replace")
    return None


# ---------------------------------------------------------------------------
# AIS message normalization
# ---------------------------------------------------------------------------

def normalize(msg: dict) -> dict | None:
    meta     = msg.get("MetaData", {})
    msg_type = msg.get("MessageType", "")
    inner    = msg.get("Message", {}).get(msg_type, {})
    mmsi_raw = meta.get("MMSI") or inner.get("Mmsi")
    mmsi_key = str(mmsi_raw) if mmsi_raw else None

    # --- Static data (AIS Type 5 / Type 24) — cache and return None (no position) ---
    if msg_type == "ShipStaticData":
        if not mmsi_key:
            return None
        static = {}
        imo = inner.get("ImoNumber")
        if imo:
            static["imo"] = str(imo)
        cs = (inner.get("CallSign") or "").strip()
        if cs:
            static["callsign"] = cs
        name = (meta.get("ShipName") or inner.get("Name") or "").strip()
        if name:
            static["ship_name"] = name
        ship_type = inner.get("Type")
        if ship_type is not None:
            try:
                static["ship_type"] = _ship_type_label(int(ship_type))
                static["ship_type_code"] = int(ship_type)
            except (ValueError, TypeError):
                pass
        dim = inner.get("Dimension") or {}
        a = dim.get("A", 0); b = dim.get("B", 0)
        c = dim.get("C", 0); d = dim.get("D", 0)
        length = (a or 0) + (b or 0)
        beam   = (c or 0) + (d or 0)
        if length > 0:
            static["length_m"] = length
        if beam > 0:
            static["beam_m"] = beam
        draft = inner.get("Draught")
        if draft:
            try:
                static["draft_m"] = round(float(draft), 1)
            except (ValueError, TypeError):
                pass
        dest = (inner.get("Destination") or "").strip()
        if dest and dest not in ("", "NOWHERE", "@@@@@@@@@", "NO DEST"):
            static["destination"] = dest
        eta = inner.get("Eta") or {}
        mo = eta.get("Month", 0); dy = eta.get("Day", 0)
        hr = eta.get("Hour", 0);  mi = eta.get("Minute", 0)
        if mo and dy:
            static["eta"] = "{:02d}-{:02d} {:02d}:{:02d} UTC".format(mo, dy, hr, mi)
        if static:
            _STATIC[mmsi_key] = static
        return None   # no position in static messages

    # --- Position reports ---
    lat = meta.get("latitude") or inner.get("Latitude")
    lon = meta.get("longitude") or inner.get("Longitude")
    if lat is None or lon is None:
        return None

    track = {
        "_ts":      time.time(),
        "_src":     "aisstream",
        "mmsi":     mmsi_raw,
        "msg_type": msg_type,
        "lat_deg":  round(float(lat), 6),
        "lon_deg":  round(float(lon), 6),
    }

    name = meta.get("ShipName", "").strip()
    if name:
        track["ship_name"] = name

    sog = inner.get("Sog")
    if sog is not None:
        track["sog_ms"] = round(float(sog) * 0.514444, 2)

    cog = inner.get("Cog")
    if cog is not None:
        track["cog_deg"] = round(float(cog), 1)

    hdg = inner.get("TrueHeading")
    if hdg is not None and int(hdg) != 511:
        track["heading_deg"] = int(hdg)

    nav = inner.get("NavigationalStatus")
    if nav is not None:
        track["nav_status"] = _NAV_STATUS.get(int(nav), str(nav))

    t = meta.get("time_utc", "")
    if t:
        track["time_utc"] = t

    # Merge any static data we have for this MMSI
    if mmsi_key and mmsi_key in _STATIC:
        for k, v in _STATIC[mmsi_key].items():
            track.setdefault(k, v)

    return track


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args):
    topic = "{}/sea/aisstream/ais/civ/vessel/tracks/v1".format(TOPIC_ROOT)
    print("Zenoh topic:", topic, flush=True)
    print("AIS bounding box:", args.bbox, flush=True)

    session = zenoh.open(make_config())
    pub = session.declare_publisher(topic)

    subscribe_msg = json.dumps({
        "APIKey": args.apikey,
        "BoundingBoxes": args.bbox,
        "FilterMessageTypes": [
            "PositionReport",
            "ExtendedClassBPositionReport",
            "StandardClassBPositionReport",
            "ShipStaticData",          # AIS Type 5/24 — IMO, callsign, dims, dest
        ],
    })

    try:
        while True:
            try:
                print("Connecting to aisstream.io…", flush=True)
                sock = ws_connect(AISSTREAM_HOST, AISSTREAM_PATH)
                ws_send(sock, subscribe_msg)
                print("Subscribed — streaming AIS…", flush=True)
                while True:
                    text = ws_recv(sock)
                    if text is None:
                        continue
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    track = normalize(msg)
                    if track is None:
                        continue
                    payload = json.dumps(track)
                    pub.put(payload.encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                    print("PUB", payload[:140], flush=True)

            except (EOFError, OSError, TimeoutError, ConnectionError) as exc:
                print("Connection error: {} — reconnecting in {}s".format(
                    exc, RECONNECT_DELAY_S), flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(RECONNECT_DELAY_S)

    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


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


def main():
    ap = argparse.ArgumentParser(description="aisstream.io → Zenoh bridge")
    ap.add_argument("--apikey", default=os.environ.get("AISSTREAM_KEY", ""),
                    help="aisstream.io API key")
    ap.add_argument("--bbox", type=json.loads, default=DEFAULT_BBOX,
                    help='[[min_lat,min_lon],[max_lat,max_lon]]')
    ap.add_argument("--topic-suffix", default="tracks/v1")
    args = ap.parse_args()
    if not args.apikey:
        ap.error("API key required — use --apikey or set AISSTREAM_KEY")
    run(args)


if __name__ == "__main__":
    main()
