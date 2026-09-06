#!/usr/bin/env python3
"""Raw NATO NFFI XML on Zenoh -> normalized friendly-force tracks.

Subscribes to complete NFFI XML documents already published by a partner system
through its attached Zenoh router. This module owns no source socket, endpoint,
or vendor-specific connection logic.

NFFI is the Friendly Force Tracking exchange used by ADatP-36 / STANAG 5527.
STANAG 4677 is a separate dismounted-soldier interoperability family; one of
its profiles can use an NFFI transport, but this generic NFFI translator does
not claim to decode the STANAG 4677 JDSSDM profile.

Targets the real STANAG 5527 / NFFI 1.4 schema (namespace
urn:nato:fft:protocols:nffi14, root NFFIMessage/track*) — see
docs/references/nffi/NFFI.md for the source and how it was confirmed.

Raw input:  <PREFIX>/<ORG>/raw/nffi/<source-id>
Output:     <PREFIX>/<ORG>/land/nato/c2/friendly/unit/json/tracks
"""

import argparse
import os
import time

import defusedxml.ElementTree as ET
from namespace_prefix import topic_root
from protocols.gateway import ZError, open_session, publish_dual, subscribe
from protocols.proto.nffi_pb2 import NffiTrack

TOPIC_ROOT = topic_root()

MAX_NFFI_XML = 10_000_000
DEFAULT_INPUT_TOPIC = "{}/raw/nffi/*".format(TOPIC_ROOT)
OUTPUT_TOPIC = "{}/land/nato/c2/friendly/unit".format(TOPIC_ROOT)
ZENOH_RETRY_S = 5

# The real STANAG 5527 / NFFI 1.4 namespace, per the primary NC3A-authored
# XSD (fetched and read in full 2026-09-06 from a public NATO-DDS-vendor
# mirror — see docs/references/nffi/NFFI.md). Earlier versions of this file
# used a "urn:nato:nffi:2.0"-family namespace and PascalCase tag names
# (UnitInfo/Latitude/Heading/...) that do not exist in any confirmed NFFI
# edition (1.3 or 1.4, the only two with any public evidence at all) — those
# were never checked against a real schema and would not have matched real
# NFFI traffic. This parser now targets the real 1.4 element structure,
# with a namespace-less fallback for transports that strip it.
_NFFI_NS = "urn:nato:fft:protocols:nffi14"


# ---------------------------------------------------------------------------
# NFFI XML parser
# ---------------------------------------------------------------------------

def _child(elem, tag):
    """Find a direct child by local tag name, NFFI-namespaced or not."""
    e = elem.find("{%s}%s" % (_NFFI_NS, tag))
    if e is None:
        e = elem.find(tag)
    return e


def _child_text(elem, tag) -> str | None:
    e = _child(elem, tag)
    if e is not None and e.text and e.text.strip():
        return e.text.strip()
    return None


def _first_attr(elems, name: str) -> str | None:
    """First non-empty value of `name` across `elems`, in order."""
    for elem in elems:
        if elem is None:
            continue
        value = elem.get(name)
        if value:
            return value
    return None


def _parse_track(track_elem) -> dict | None:
    """Decode one NFFI 1.4 <track> element (trackType) into a track dict.

    Structure per the schema: positionalData (mandatory: trackSource,
    dateTime, coordinates, optional bearing/speed/reliability/inclination),
    identificationData (optional: unitSymbol, unitShortName), operStatusData
    (optional: footprint/strength/statusCode/alert/remarks). detailData is a
    security-only extension point in this edition and carries no payload.

    Every section also independently carries secPolicyName/secClassification/
    secCategory attributes — NFFI's own flattening of the same confidentiality
    -label vocabulary (PolicyIdentifier/Classification/Category) STANAG 4778's
    Metadata Binding schema defines for NATO Core Metadata generally. Checked
    positionalData (always present) first, then identificationData, then
    operStatusData, per attribute independently.
    """
    positional = _child(track_elem, "positionalData")
    if positional is None:
        return None

    coordinates = _child(positional, "coordinates")
    if coordinates is None:
        return None
    lat = _child_text(coordinates, "latitude")
    lon = _child_text(coordinates, "longitude")
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except ValueError:
        return None

    # trackSource (sourceSystem.system/.subsystem + transponderId) is the
    # schema's own "unambiguous identification of data source" — the stable
    # per-device identity. dateTime is deliberately NOT folded into uid: the
    # schema pairs it with trackSource only to dedupe identical retransmitted
    # records, and every uid using it would create a new "track" per update.
    system = subsystem = transponder_id = None
    track_source = _child(positional, "trackSource")
    if track_source is not None:
        source_system = _child(track_source, "sourceSystem")
        if source_system is not None:
            system = _child_text(source_system, "system")
            subsystem = _child_text(source_system, "subsystem")
        transponder_id = _child_text(track_source, "transponderId")

    identification = _child(track_elem, "identificationData")
    unit_symbol = unit_short_name = None
    if identification is not None:
        unit_symbol = _child_text(identification, "unitSymbol")
        unit_short_name = _child_text(identification, "unitShortName")

    uid_parts = [p for p in (system, subsystem, transponder_id) if p]
    stable_id = "-".join(uid_parts) if uid_parts else (unit_short_name or "unknown")

    track = {
        "_ts":  time.time(),
        "_src": "nffi",
        "uid":  "NFFI-" + stable_id,
        "sensor_id": stable_id,
        "callsign": unit_short_name or stable_id,
        "lat_deg": round(lat_f, 6),
        "lon_deg": round(lon_f, 6),
        # NFFI 1.4 carries no affiliation field at all — a Friendly Force
        # Tracking exchange is friendly-force-only by definition, the same
        # reasoning stanag.py applies to PPLI (see stanag.py's own comment).
        "affiliation": "friendly",
        "nffi_affiliation": "FRIEND",
    }

    altitude = _child_text(coordinates, "altitude")
    if altitude is not None:
        try:
            track["geo_alt_m"] = round(float(altitude), 1)  # schema unit: metres MSL
        except ValueError:
            pass

    bearing = _child_text(positional, "bearing")
    if bearing is not None:
        try:
            track["heading_deg"] = round(float(bearing), 1)
        except ValueError:
            pass

    speed = _child_text(positional, "speed")
    if speed is not None:
        try:
            track["speed_ms"] = round(float(speed) / 3.6, 2)  # schema unit: km/h
        except ValueError:
            pass

    date_time = _child_text(positional, "dateTime")
    if date_time:
        track["nffi_date_time"] = date_time  # schema format: YYYYMMDDhhmmss

    if unit_symbol:
        track["unit_type"] = unit_symbol  # raw 15-char APP-6(A) SIDC

    oper_status = _child(track_elem, "operStatusData")
    if oper_status is not None:
        alert = _child_text(oper_status, "alert")
        if alert is not None:
            track["emergency"] = alert.strip().lower() == "true"
        strength = _child_text(oper_status, "strength")
        if strength is not None:
            try:
                track["nffi_strength"] = int(strength)
            except ValueError:
                pass
        status_code = _child_text(oper_status, "statusCode")
        if status_code:
            track["nffi_status"] = status_code

    sections = (positional, identification, oper_status)
    policy = _first_attr(sections, "secPolicyName")
    if policy:
        track["nffi_sec_policy"] = policy
    classification = _first_attr(sections, "secClassification")
    if classification:
        track["nffi_sec_classification"] = classification
        # Generic keys tak_layer.py (CoT "access") and sitaware_layer.py
        # (NVG root "classification") both read — any source protocol that
        # decodes classification data sets these, not just NFFI.
        track["classification"] = classification
    category = _first_attr(sections, "secCategory")
    if category:
        track["nffi_sec_category"] = category
        track["classification_caveat"] = category  # -> CoT "caveat"

    return track


def parse_nffi(xml_bytes: bytes) -> list[dict]:
    """Parse an NFFI 1.4 XML document (NFFIMessage/track*); return track dicts."""
    try:
        root = ET.fromstring(xml_bytes)
    except (ET.ParseError, ValueError) as exc:
        # defusedxml raises DefusedXmlException (a ValueError subclass, not
        # ET.ParseError) on entity-expansion/external-reference attacks.
        print("NFFI XML parse error:", exc, flush=True)
        return []

    track_elems = root.findall("{%s}track" % _NFFI_NS) or root.findall("track")
    if not track_elems:
        root_local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if root_local == "track":
            track_elems = [root]

    tracks = []
    for track_elem in track_elems:
        parsed = _parse_track(track_elem)
        if parsed is not None:
            tracks.append(parsed)
    return tracks


def make_handler(session, verbose: bool = False):
    def on_sample(sample) -> None:
        xml_bytes = bytes(sample.payload)
        if not xml_bytes or len(xml_bytes) > MAX_NFFI_XML:
            if verbose:
                print("NFFI ignored invalid payload size from", sample.key_expr, flush=True)
            return
        for track in parse_nffi(xml_bytes):
            publish_dual(session, OUTPUT_TOPIC, track, NffiTrack)
            if verbose:
                print(
                    "NFFI {} lat={} lon={}".format(
                        track["callsign"],
                        track["lat_deg"],
                        track["lon_deg"],
                    ),
                    flush=True,
                )

    return on_sample


def _open_session():
    while True:
        try:
            return open_session()
        except ZError as exc:
            print("NFFI Zenoh connect failed: {} — retry in {}s".format(exc, ZENOH_RETRY_S), flush=True)
            time.sleep(ZENOH_RETRY_S)


def run(args):
    session = _open_session()
    subscriber = subscribe(
        session,
        args.input_topic,
        make_handler(session, args.verbose),
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
