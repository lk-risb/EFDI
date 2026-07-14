#!/usr/bin/env python3
"""nato_nffi_layer.py — NATO NFFI (Friendly Forces Information) → Zenoh bridge.

Receives NATO NFFI XML messages over TCP (length-prefixed or newline-delimited)
and publishes each unit position as a track JSON to the EFDI fabric.

NFFI (STANAG 4677 / FMN NFFI) defines a standard XML schema for sharing
friendly force position, status, and identification between C2 systems.

Two framing modes:
  --framing length   4-byte big-endian length prefix + XML bytes (default)
  --framing newline  newline-delimited XML documents

Zenoh topic:  <ORG>/fffi/nffi/tracks/v1

Run:
    venv/bin/python3 nato_nffi_layer.py --host 192.0.2.20 --port 7010
    venv/bin/python3 nato_nffi_layer.py --host 192.0.2.20 --port 7010 --framing newline
"""

import argparse
import json
import os
import re
import socket
import struct
import time

import defusedxml.ElementTree as ET
import zenoh
from namespace_prefix import prefix

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG   # org prefix (configurable) precedes the pod namespace
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

RECONNECT_S = 5

# NFFI XML namespace (FMN NFFI / STANAG 4677)
_NS = {
    "nffi":  "urn:nato:nffi:2.0",
    "pos":   "urn:nato:nffi:position:2.0",
    "unit":  "urn:nato:nffi:unit:2.0",
    "id":    "urn:nato:nffi:identification:2.0",
}

# Fallback: also handle unnamespaced NFFI from some implementations
_LAT_TAGS = {"Latitude", "lat", "LAT"}
_LON_TAGS = {"Longitude", "lon", "LON", "Long"}


# ---------------------------------------------------------------------------
# Zenoh
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# NFFI XML parser
# ---------------------------------------------------------------------------

def _text(elem, *tags) -> str | None:
    """Try multiple tag names (with and without namespace) and return text."""
    for tag in tags:
        for ns_prefix in ("nffi", "pos", "unit", "id", ""):
            if ns_prefix:
                e = elem.find(".//{%s}%s" % (_NS.get(ns_prefix, ""), tag))
            else:
                e = elem.find(".//" + tag)
            if e is not None and e.text and e.text.strip():
                return e.text.strip()
    return None


def _find_lat_lon(elem):
    """Walk all elements to find latitude/longitude, handling any namespace."""
    lat = lon = None
    for child in elem.iter():
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local in _LAT_TAGS and child.text:
            try:
                lat = float(child.text.strip())
            except ValueError:
                pass
        elif local in _LON_TAGS and child.text:
            try:
                lon = float(child.text.strip())
            except ValueError:
                pass
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


def parse_nffi(xml_bytes: bytes) -> list[dict]:
    """Parse an NFFI XML document; return list of unit track dicts."""
    try:
        root = ET.fromstring(xml_bytes)
    except (ET.ParseError, ValueError) as exc:
        # defusedxml raises DefusedXmlException (a ValueError subclass, not
        # ET.ParseError) on entity-expansion/external-reference attacks.
        print("NFFI XML parse error:", exc, flush=True)
        return []

    tracks = []

    # NFFI messages may wrap multiple UnitInfo / Track elements
    # Walk any element that looks like a unit or track record
    unit_like_tags = {"UnitInfo", "Unit", "Track", "FriendlyForce",
                      "FriendlyForceUnit", "UnitTrack"}

    def _process(elem):
        local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local not in unit_like_tags:
            return
        lat, lon = _find_lat_lon(elem)
        if lat is None or lon is None:
            return

        unit_id   = _text(elem, "UnitID", "TrackID", "ID", "id") or ""
        name      = _text(elem, "Name", "UnitName", "Callsign", "callsign") or unit_id
        affil     = _text(elem, "Affiliation", "affiliation", "AffiliationCode") or "FRIEND"
        speed_str = _text(elem, "Speed", "speed")
        hdg_str   = _text(elem, "Heading", "heading", "Direction")
        alt_str   = _text(elem, "Altitude", "altitude", "Elevation")

        try:
            speed = float(speed_str) if speed_str else 0.0
        except ValueError:
            speed = 0.0
        try:
            heading = float(hdg_str) if hdg_str else 0.0
        except ValueError:
            heading = 0.0
        try:
            alt = float(alt_str) if alt_str else None
        except ValueError:
            alt = None

        tracks.append({
            "_ts":         time.time(),
            "_src":        "nffi",
            "sensor_id":   unit_id or name,
            "callsign":    name,
            "lat_deg":     round(lat, 6),
            "lon_deg":     round(lon, 6),
            "geo_alt_m":   round(alt, 1) if alt is not None else None,
            "speed_ms":    round(speed, 2),
            "heading_deg": round(heading, 1),
            "nffi_affil":  affil,
        })

    # Process root itself and all descendants
    _process(root)
    for child in root.iter():
        _process(child)

    return tracks


# ---------------------------------------------------------------------------
# TCP framing
# ---------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def iter_frames_length(sock: socket.socket):
    """4-byte big-endian length prefix framing."""
    while True:
        raw_len = _recv_exact(sock, 4)
        length  = struct.unpack(">I", raw_len)[0]
        if length == 0 or length > 10_000_000:
            raise ValueError(f"invalid NFFI frame length: {length}")
        yield _recv_exact(sock, length)


def iter_frames_newline(sock: socket.socket):
    """Newline-delimited XML framing — accumulate until closing root tag."""
    f = sock.makefile("rb")
    buf = b""
    for line in f:
        buf += line
        if len(buf) > 10_000_000:
            raise ValueError("NFFI newline frame exceeds 10 MB")
        # Emit when we have a complete XML document (rough heuristic)
        stripped = buf.strip()
        if stripped and stripped.startswith(b"<") and (
            stripped.endswith(b">") and re.search(rb"</\w+>\s*$", stripped)
        ):
            yield buf
            buf = b""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    topic = "{}/land/nato/nffi/friendly/unit/tracks/v1".format(TOPIC_ROOT)
    print("NFFI → Zenoh topic:", topic, flush=True)

    session = zenoh.open(make_config())
    pub = session.declare_publisher(topic)

    try:
        while True:
            try:
                print("Connecting to NFFI server {}:{}…".format(args.host, args.port), flush=True)
                sock = socket.create_connection((args.host, args.port), timeout=10)
                sock.settimeout(60)
                print("Connected.", flush=True)

                frame_iter = (iter_frames_newline(sock) if args.framing == "newline"
                              else iter_frames_length(sock))

                for xml_bytes in frame_iter:
                    tracks = parse_nffi(xml_bytes)
                    for track in tracks:
                        pub.put(json.dumps(track).encode(),
                                encoding=zenoh.Encoding.APPLICATION_JSON)
                        if args.verbose:
                            print("NFFI {} {} lat={} lon={}".format(
                                track["nffi_affil"], track["callsign"],
                                track["lat_deg"], track["lon_deg"]), flush=True)

            except (EOFError, OSError, TimeoutError, ValueError) as exc:
                print("NFFI connection error: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(RECONNECT_S)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="NATO NFFI XML → Zenoh bridge")
    ap.add_argument("--host", default=os.environ.get("NFFI_HOST", "127.0.0.1"),
                    help="NFFI server host")
    ap.add_argument("--port", type=int, default=int(os.environ.get("NFFI_PORT", "7010")),
                    help="NFFI server port (default: 7010)")
    ap.add_argument("--framing", choices=["length", "newline"], default="length",
                    help="Message framing: 4-byte length prefix or newline-delimited (default: length)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
