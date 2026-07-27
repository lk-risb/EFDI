#!/usr/bin/env python3
"""nvg_bridge.py — SitaWare NVG export → Zenoh bridge.

Polls a SitaWare NVG Export Endpoint, decodes the NVG 2.0.2 document, and
publishes each positioned item onto the EFDI fabric so the output layers can
forward it to the other C2 systems.

This is the INGRESS side of the SitaWare integration: a _bridge brings an
external system's data into Zenoh. Sending EFDI tracks the other way is
layers/nvg_layer.py, which serves the feed SitaWare's Import Subscription
polls — SitaWare initiating that transfer does not make it an ingress.

It exists alongside sitaware_bridge.py because the two speak different
languages to the same server: sitaware_bridge reads the JSON track-server API,
while an NVG Export Endpoint returns XML, which that bridge cannot parse.

    SITAWARE_NVG_IMPORT_URL     full URL of the NVG export endpoint
                                (defaults to SITAWARE_URL + SITAWARE_API_PATH)
    SITAWARE_NVG_IMPORT_USER    basic-auth user (defaults to SITAWARE_USER)
    SITAWARE_NVG_IMPORT_PASS    basic-auth pass (defaults to SITAWARE_PASS)
    SITAWARE_NVG_IMPORT_POLL_S  seconds between polls (default 10)
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import zenoh
# One SIDC -> topic mapping for both SitaWare ingress paths; a second copy would
# drift and land the same unit on two different keys.
from bridges.sitaware_bridge import sidc_to_topic
from namespace_prefix import topic_root
from protocols.protobuf_codec import semantic_topic, add_version
from zenoh_auth import apply_zenoh_auth

ORG        = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE       = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR  = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT  = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

NVG_NS = "https://tide.act.nato.int/schemas/2012/10/nvg"
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
SOURCE = "sitaware-nvg"


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sidc_of(symbol: str | None) -> str:
    """Strip the scheme prefix NVG puts on a symbol ("2525b:SFGPU----*****")."""
    if not symbol:
        return ""
    return symbol.split(":", 1)[1] if ":" in symbol else symbol


def parse_nvg(document: bytes) -> list[dict]:
    """Decode NVG <point> elements into EFDI track dicts.

    Items without a usable position are skipped rather than published with a
    null location: a track whose position cannot be trusted is worse on a map
    than a track that is absent.
    """
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return []

    tracks: list[dict] = []
    now = time.time()
    for point in root.iter("{%s}point" % NVG_NS):
        lon = _number(point.get("x"))
        lat = _number(point.get("y"))
        if lat is None or lon is None:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue

        uri = point.get("uri") or ""
        # Our own items come back carrying the urn:efdi: uid we published; keep
        # the bare uid so a round-tripped object keeps one identity on the
        # fabric instead of appearing again under a second key.
        uid = uri.rsplit(":", 1)[-1] if uri.startswith("urn:efdi:") else uri

        track = {
            "_src":     SOURCE,
            "_ts":      now,
            "_ingress": "sitaware_nvg",
            "uid":      uid or None,
            "sidc":     _sidc_of(point.get("symbol")),
            "lat_deg":  round(lat, 6),
            "lon_deg":  round(lon, 6),
        }
        label = point.get("label")
        if label:
            track["callsign"] = label
        course = _number(point.get("course"))
        if course is not None:
            track["heading_deg"] = course
        speed = _number(point.get("speed"))
        if speed is not None:
            track["speed_ms"] = speed
        tracks.append({key: value for key, value in track.items() if value is not None})
    return tracks


def _tls_context(ca_file: str, insecure: bool) -> "ssl.SSLContext | None":
    """Verify against a pinned certificate where possible.

    SitaWare ships a self-signed server certificate, so there is no CA to chain
    to — but trusting that one certificate still beats trusting anything.
    Hostname checking is off because the certificate carries only a CN and no
    subjectAltName, which Python has refused to match since 3.7; the pin is what
    authenticates the server. --insecure disables verification outright and is
    the last resort, not the default.
    """
    if ca_file:
        context = ssl.create_default_context(cafile=ca_file)
        context.check_hostname = False
        return context
    return ssl._create_unverified_context() if insecure else None


def fetch(url: str, auth_header: str, context: "ssl.SSLContext | None") -> bytes | None:
    # No Accept header: SitaWare's NVG export answers 500 to
    # "Accept: application/xml" and 200 to a request that does not ask for a
    # type at all, even though what it returns is XML either way.
    request = urllib.request.Request(url, headers={
        "Authorization": auth_header,
        "User-Agent": "efdi-nvg-bridge/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            return response.read(MAX_DOCUMENT_BYTES)
    except urllib.error.HTTPError as exc:
        print("NVG import HTTP {} from {}".format(exc.code, url), flush=True)
    except (urllib.error.URLError, OSError) as exc:
        print("NVG import unreachable: {}".format(exc), flush=True)
    return None


def run(args) -> None:
    auth = "Basic " + base64.b64encode(
        "{}:{}".format(args.user, args.password).encode()).decode()
    print("NVG import: {} every {}s".format(args.url, args.poll_s), flush=True)

    context = _tls_context(args.ca, args.insecure)
    session = zenoh.open(make_config())
    try:
        while True:
            document = fetch(args.url, auth, context)
            if document is not None:
                tracks = parse_nvg(document)
                for track in tracks:
                    topic = add_version(semantic_topic(
                        sidc_to_topic(track.get("sidc", "")), track))
                    session.put(topic, json.dumps(track).encode(),
                                encoding=zenoh.Encoding.APPLICATION_JSON)
                    if args.verbose:
                        print("NVG in {} -> {}".format(
                            track.get("callsign") or track.get("uid"), topic), flush=True)
                print("NVG import published {} items".format(len(tracks)), flush=True)
            time.sleep(args.poll_s)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def main() -> None:
    default_url = os.environ.get("SITAWARE_NVG_IMPORT_URL", "")
    if not default_url:
        base = os.environ.get("SITAWARE_URL", "").rstrip("/")
        path = os.environ.get("SITAWARE_API_PATH", "")
        default_url = base + path if base and path else ""

    parser = argparse.ArgumentParser(description="SitaWare NVG export → Zenoh")
    parser.add_argument("--url", default=default_url)
    parser.add_argument("--user", default=os.environ.get(
        "SITAWARE_NVG_IMPORT_USER", os.environ.get("SITAWARE_USER", "")))
    parser.add_argument("--password", default=os.environ.get(
        "SITAWARE_NVG_IMPORT_PASS", os.environ.get("SITAWARE_PASS", "")))
    parser.add_argument("--poll-s", type=float, default=float(
        os.environ.get("SITAWARE_NVG_IMPORT_POLL_S", "10")))
    parser.add_argument("--ca", default=os.environ.get("SITAWARE_NVG_IMPORT_CA", ""),
                        help="PEM holding SitaWare's server certificate to pin")
    parser.add_argument(
        "--insecure", action="store_true",
        default=os.environ.get("SITAWARE_TLS_VERIFY", "1") == "0",
        help="skip TLS verification (SitaWare ships a self-signed certificate)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not args.url:
        raise SystemExit(
            "set SITAWARE_NVG_IMPORT_URL, or SITAWARE_URL + SITAWARE_API_PATH")
    run(args)


if __name__ == "__main__":
    main()
