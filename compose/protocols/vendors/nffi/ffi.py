#!/usr/bin/env python3
"""Raw APP-11(D) FFI XML on Zenoh -> normalized friendly-force tracks.

The FFI counterpart of nffi.py: NFFI (STANAG 5527) and FFI (the APP-11 Edition D FriendlyForceInformation message) carry the same kind of
track, a position with an identity and a symbol, in two message formats, and SitaWare's NFFI and FFI Manager writes either. This decoder
takes the FFI one and publishes the same normalized tracks nffi.py does, on the same topics, so everything after it (fusion, tak_layer,
sitaware_layer) treats a track alike whichever format it came in.

Raw input:  <PREFIX>/<ORG>/raw/ffi/<source-id>
Output:     <PREFIX>/<ORG>/{domain}/nato/c2/friendly/unit/json/tracks   (domain by the symbol, land when it says nothing)

The structure is the one a live SitaWare Headquarters 6.21 IP1 server writes as FFI (docs/references/nffi/NFFI.md): root
mtf:FriendlyForceInformation in urn:nato:mtf:app-11(d):1:ffi, then one or more TrackInformation, each with TrackSource (TransponderId,
System, Subsystem), TrackPositionalData (Time, and Location as degrees and minutes) and TrackIdentificationData (UnitSymbol,
SymbolStandard, UnitShortName). Elements are matched by local name. Not seen in any capture, so not decoded: speed, course, altitude.

UnitSymbol is the 15-character letter code under APP-6(B), or the 20-digit APP-6(D) code under APP-6(D); the domain of a numeric symbol is
its symbol set (digits 5-6: 01 air, 02 air missile, 05 space, 10-27 land, 30-36 sea; set names checked against the APP-6D symbol-set
tables of the milstd package). A track this pod itself sent to SitaWare (this pod's system and subsystem TAK, or a transponderId this pod
published) and SitaWare sends back is dropped (fusion.is_own_force_track); a partner pod's tracks are kept.
"""

import argparse
import os
import time
from datetime import datetime, timezone

import defusedxml.ElementTree as ET
from namespace_prefix import topic_root
from protocols.vendors.nffi import nffi
from protocols.vendors.random.fusion import EchoFilter, subscribe_own
from protocols.vendors.random.gateway import subscribe

TOPIC_ROOT = topic_root()
DEFAULT_INPUT_TOPIC = "{}/raw/ffi/*".format(TOPIC_ROOT)

# APP-6(D) symbol set (digits 5-6 of the 20-digit code) -> output domain
_DOMAIN_BY_SYMBOL_SET = {
    "01": "air", "02": "air", "05": "space", "06": "space",
    "10": "land", "11": "land", "15": "land", "20": "land", "25": "land", "27": "land", "40": "land",
    "30": "sea", "35": "sea", "36": "sea",
}


def _domain_topic(unit_symbol) -> str:
    """The output topic for a UnitSymbol: by symbol set for a numeric APP-6(D) code, else as nffi.py does for a letter code."""
    symbol = str(unit_symbol or "").strip()
    if len(symbol) >= 20 and symbol.isdigit():
        return nffi.OUTPUT_TOPICS[_DOMAIN_BY_SYMBOL_SET.get(symbol[4:6], "land")]
    return nffi._output_topic(symbol)


def _text(elem, name):
    child = nffi._child(elem, name) if elem is not None else None
    if child is not None and child.text and child.text.strip():
        return child.text.strip()
    return None


def _time_epoch(elem) -> float | None:
    try:
        parts = [int(_text(elem, tag)) for tag in ("Year4Digit", "MonthNumeric", "Day", "HourTime", "MinuteTime", "SecondTime")]
        return datetime(*parts, tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _coordinate(location, prefix: str, negative: str) -> float | None:
    """Degrees and decimal minutes with a hemisphere letter (LatitudeDegrees, LatitudeMinutes04DecimalPlaces, LatitudinalHemisphere)."""
    hemisphere = {"Latitude": "LatitudinalHemisphere", "Longitude": "LongitudinalHemisphere"}[prefix]
    try:
        degrees = float(_text(location, prefix + "Degrees"))
        minutes = float(_text(location, prefix + "Minutes04DecimalPlaces"))
    except (TypeError, ValueError):
        return None
    value = degrees + minutes / 60.0
    return -value if (_text(location, hemisphere) or "").upper() == negative else value


def _parse_track(info) -> dict | None:
    positional = nffi._child(info, "TrackPositionalData")
    location = nffi._child(positional, "Location") if positional is not None else None
    if location is None:
        return None
    lat, lon = _coordinate(location, "Latitude", "S"), _coordinate(location, "Longitude", "W")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    source = nffi._child(info, "TrackSource")
    system, subsystem, transponder = _text(source, "System"), _text(source, "Subsystem"), _text(source, "TransponderId")
    ident = nffi._child(info, "TrackIdentificationData")
    symbol, standard, short_name = _text(ident, "UnitSymbol"), _text(ident, "SymbolStandard"), _text(ident, "UnitShortName")

    stable_id = "-".join(part for part in (system, subsystem, transponder) if part) or short_name or "unknown"
    track = {
        "_ts": time.time(),
        "_src": "ffi",
        "uid": "FFI-" + stable_id,
        "sensor_id": stable_id,
        "callsign": short_name or stable_id,
        "lat_deg": round(lat, 6),
        "lon_deg": round(lon, 6),
        "affiliation": "friendly",           # a Friendly Force Information exchange is friendly-force-only by definition
        "nffi_affiliation": "FRIEND",
        "source_system": system,
        "source_subsystem": subsystem,
        "transponder_id": transponder,
    }
    epoch = _time_epoch(nffi._child(positional, "Time"))
    if epoch is not None:
        track["ffi_time_epoch_s"] = epoch
    if symbol:
        track["unit_type"] = symbol
        if standard:
            track["symbol_standard"] = standard
        echelon = nffi._echelon(symbol) if not symbol.isdigit() else None
        if echelon:
            track["nffi_echelon"] = echelon
    label = nffi._child(info, "TrackSecurityLabel")
    classification = _text(nffi._child(label, "TrackSecurityClassification") if label is not None else None, "SecurityClassificationExtended")
    if classification:
        track["classification"] = classification
    return track


def parse_ffi(xml_bytes: bytes) -> list[dict]:
    """Parse an APP-11(D) FriendlyForceInformation document; return one track dict per TrackInformation."""
    try:
        root = ET.fromstring(xml_bytes)
    except (ET.ParseError, ValueError) as exc:
        print("FFI XML parse error:", exc, flush=True)
        return []
    tracks = []
    for info in (e for e in root if nffi._local(e.tag) == "TrackInformation"):
        parsed = _parse_track(info)
        if parsed is not None:
            tracks.append(parsed)
    return tracks


def run(args):
    session = nffi._open_session()
    echoes = EchoFilter()
    echo_subs = subscribe_own(session, TOPIC_ROOT, echoes, subscribe)       # the ids this pod publishes: its own TAK users
    subscriber = subscribe(session, args.input_topic,
                           nffi.make_handler(session, args.verbose, parse=parse_ffi, label="FFI", domain_of=_domain_topic, echoes=echoes))
    print("FFI raw Zenoh input:", args.input_topic, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Raw APP-11(D) FFI XML on Zenoh -> normalized tracks")
    ap.add_argument("--input-topic", default=os.environ.get("FFI_INPUT_TOPIC") or DEFAULT_INPUT_TOPIC)
    ap.add_argument("--verbose", "-v", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
