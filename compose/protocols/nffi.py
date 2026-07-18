#!/usr/bin/env python3
"""Raw NATO NFFI XML on Zenoh -> normalized friendly-force tracks.

Subscribes to complete NFFI XML documents already published by a partner system
through its attached Zenoh router. This module owns no source socket, endpoint,
or vendor-specific connection logic.

NFFI is the Friendly Force Tracking exchange used by ADatP-36 / STANAG 5527.
STANAG 4677 is a separate dismounted-soldier interoperability family; one of
its profiles can use an NFFI transport, but this generic NFFI translator does
not claim to decode the STANAG 4677 JDSSDM profile.

Raw input:  <PREFIX>/<ORG>/raw/nffi/<source-id>
Output:     <PREFIX>/<ORG>/land/nato/nffi/friendly/unit/tracks/v1
"""

import argparse
import json
import os
import time

import defusedxml.ElementTree as ET
import zenoh
from namespace_prefix import topic_root

ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

MAX_NFFI_XML = 10_000_000
DEFAULT_INPUT_TOPIC = "{}/raw/nffi/*".format(TOPIC_ROOT)
OUTPUT_TOPIC = "{}/land/nato/nffi/friendly/unit/tracks/v1".format(TOPIC_ROOT)
ZENOH_RETRY_S = 5

# NFFI XML namespaces used by ADatP-36 / STANAG 5527 implementations
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
        if child is not root:
            _process(child)

    return tracks


def make_handler(publisher, verbose: bool = False):
    def on_sample(sample) -> None:
        xml_bytes = bytes(sample.payload)
        if not xml_bytes or len(xml_bytes) > MAX_NFFI_XML:
            if verbose:
                print("NFFI ignored invalid payload size from", sample.key_expr, flush=True)
            return
        for track in parse_nffi(xml_bytes):
            publisher.put(
                json.dumps(track, separators=(",", ":")).encode(),
                encoding=zenoh.Encoding.APPLICATION_JSON,
            )
            if verbose:
                print(
                    "NFFI {} {} lat={} lon={}".format(
                        track["nffi_affil"],
                        track["callsign"],
                        track["lat_deg"],
                        track["lon_deg"],
                    ),
                    flush=True,
                )

    return on_sample


def _open_session() -> "zenoh.Session":
    while True:
        try:
            return zenoh.open(make_config())
        except zenoh.ZError as exc:
            print("NFFI Zenoh connect failed: {} — retry in {}s".format(exc, ZENOH_RETRY_S), flush=True)
            time.sleep(ZENOH_RETRY_S)


def run(args):
    session = _open_session()
    publisher = session.declare_publisher(OUTPUT_TOPIC)
    subscriber = session.declare_subscriber(
        args.input_topic,
        make_handler(publisher, args.verbose),
    )
    print("NFFI raw Zenoh input:", args.input_topic, flush=True)
    print("NFFI normalized output:", OUTPUT_TOPIC, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        publisher.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Raw NFFI XML on Zenoh -> normalized tracks")
    ap.add_argument(
        "--input-topic",
        default=os.environ.get("NFFI_INPUT_TOPIC") or DEFAULT_INPUT_TOPIC,
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
