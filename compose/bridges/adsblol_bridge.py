#!/usr/bin/env python3
"""ADSB.lol open-data aircraft API -> normalized Zenoh tracks.

Polls the public ADSB.lol v2 point endpoint over a bounded Baltic/eastern-Europe
area. ADSB.lol is a named external service, so this is a source bridge rather
than an ADS-B protocol translator.

Current JSON topics:
  <PREFIX>/<ORG>/air/adsblol/adsb/civ/aircraft/json/tracks
  <PREFIX>/<ORG>/air/adsblol/adsb/mil/aircraft/json/tracks

The API source is BSD-3-Clause and its public data is licensed ODbL 1.0. Public
rate limits are dynamic; production users should coordinate with ADSB.lol and
contribute a feeder as requested by the service operator.
"""

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request

import zenoh
from zenoh_auth import apply_zenoh_auth

from http_json import read_json_response
from namespace_prefix import topic_root
from protocols.random.adsblol_track_pb2 import AdsbLolTrack
from protocols.protobuf_codec import source_track_to_message

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

BASE_URL = "https://api.adsb.lol/v2"
POLL_INTERVAL = 15.0
MIN_REQUEST_GAP_S = 1.05
MAX_RADIUS_NM = 250.0
POLL_CENTERS = (
    (57.0, 22.0),  # Baltic / Scandinavia
    (53.0, 28.0),  # Belarus / western Ukraine
    (48.0, 32.0),  # Ukraine / Black Sea
    (44.0, 36.0),  # Crimea / eastern Black Sea
)

_last_request_at = 0.0


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


def fetch_aircraft(lat: float, lon: float, radius_nm: float) -> list[dict]:
    """Fetch one bounded point query; return an empty list on remote failure."""
    global _last_request_at
    wait_s = MIN_REQUEST_GAP_S - (time.monotonic() - _last_request_at)
    if wait_s > 0:
        time.sleep(wait_s)
    _last_request_at = time.monotonic()

    url = "{}/point/{}/{}/{}".format(BASE_URL, lat, lon, radius_nm)
    request = urllib.request.Request(url, headers={
        "User-Agent": "efdi-adsblol-bridge/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = read_json_response(response)
    except urllib.error.HTTPError as exc:
        print("ADSB.lol HTTP {}: {}".format(exc.code, exc.reason), flush=True)
        return []
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print("ADSB.lol fetch error:", exc, flush=True)
        return []

    aircraft = data.get("ac") if isinstance(data, dict) else None
    return aircraft if isinstance(aircraft, list) else []


def _number(value, cast=float):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = cast(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(number, float) and not math.isfinite(number):
        return None
    return number


def _copy_number(track: dict, aircraft: dict, target: str, source: str, cast=float) -> None:
    value = _number(aircraft.get(source), cast)
    if value is not None:
        track[target] = value


def _copy_text(track: dict, aircraft: dict, target: str, source: str) -> None:
    value = aircraft.get(source)
    if value is not None:
        text = str(value).strip()
        if text:
            track[target] = text


def normalize(aircraft: dict) -> dict | None:
    """Normalize one readsb-compatible ADSB.lol aircraft record."""
    lat = _number(aircraft.get("lat"))
    lon = _number(aircraft.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    flags = _number(aircraft.get("dbFlags"), int)
    track = {
        "_ts": time.time(),
        "_src": "adsblol",
        "icao24": str(aircraft.get("hex") or "").strip().lower(),
        "callsign": str(aircraft.get("flight") or "").strip(),
        "registration": str(aircraft.get("r") or "").strip(),
        "aircraft_type": str(aircraft.get("t") or "").strip(),
        "lat_deg": round(lat, 6),
        "lon_deg": round(lon, 6),
        "squawk": str(aircraft.get("squawk") or "").strip(),
        "is_military": bool(flags & 1) if flags is not None else False,
    }

    _copy_text(track, aircraft, "aircraft_description", "desc")
    _copy_text(track, aircraft, "pos_source", "type")
    alt_baro = aircraft.get("alt_baro")
    if isinstance(alt_baro, str) and alt_baro.strip().lower() == "ground":
        track["on_ground"] = True
    else:
        altitude = _number(alt_baro, int)
        if altitude is not None:
            track["alt_baro_ft"] = altitude
            track["on_ground"] = False

    for target, source, cast in (
        ("alt_geom_ft", "alt_geom", int),
        ("ground_speed_kts", "gs", float),
        ("ias_kt", "ias", float),
        ("tas_kt", "tas", float),
        ("mach", "mach", float),
        ("track_deg", "track", float),
        ("track_angle_rate_degs", "track_rate", float),
        ("roll_deg", "roll", float),
        ("mag_hdg_deg", "mag_heading", float),
        ("true_heading_deg", "true_heading", float),
        ("baro_vr_fpm", "baro_rate", int),
        ("geo_vr_fpm", "geom_rate", int),
        ("baro_setting_mb", "nav_qnh", float),
        ("selected_alt_ft", "nav_altitude_mcp", int),
        ("fms_selected_alt_ft", "nav_altitude_fms", int),
        ("selected_heading_deg", "nav_heading", float),
        ("nic", "nic", int),
        ("radius_containment_m", "rc", int),
        ("position_age_s", "seen_pos", float),
        ("adsb_version", "version", int),
        ("nic_baro", "nic_baro", int),
        ("nac_p", "nac_p", int),
        ("nac_v", "nac_v", int),
        ("sil", "sil", int),
        ("gva", "gva", int),
        ("sda", "sda", int),
        ("message_count", "messages", int),
        ("message_age_s", "seen", float),
        ("rssi_db", "rssi", float),
    ):
        _copy_number(track, aircraft, target, source, cast)

    _copy_text(track, aircraft, "sil_type", "sil_type")
    _copy_text(track, aircraft, "emergency_str", "emergency")
    if track.get("emergency_str") == "none":
        track.pop("emergency_str")
    _copy_text(track, aircraft, "emitter_category_str", "category")
    if "selected_alt_ft" in track:
        track["selected_alt_source"] = "MCP/FCU"
    if flags is not None:
        track["database_flags"] = flags
        track["is_interesting"] = bool(flags & 2)
        track["is_pia"] = bool(flags & 4)
        track["is_ladd"] = bool(flags & 8)
    if "spi" in aircraft:
        track["spi"] = bool(aircraft["spi"])
    if "alert" in aircraft:
        track["alert"] = bool(aircraft["alert"])
    nav_modes = aircraft.get("nav_modes")
    if isinstance(nav_modes, list):
        modes = [str(mode).strip() for mode in nav_modes if str(mode).strip()]
        if modes:
            track["nav_modes"] = ", ".join(modes)
    return track


def run(args) -> None:
    if not 0 < args.radius <= MAX_RADIUS_NM:
        raise SystemExit("--radius must be greater than zero and at most 250 NM")
    if args.interval < 5:
        raise SystemExit("--interval must be at least 5 seconds")

    session = zenoh.open(make_config())
    civil = session.declare_publisher(
        "{}/air/adsblol/adsb/civ/aircraft".format(TOPIC_ROOT)
    )
    military = session.declare_publisher(
        "{}/air/adsblol/adsb/mil/aircraft".format(TOPIC_ROOT)
    )
    civil_v2 = session.declare_publisher(
        "{}/air/adsblol/adsb/civ/aircraft".format(TOPIC_ROOT)
    )
    military_v2 = session.declare_publisher(
        "{}/air/adsblol/adsb/mil/aircraft".format(TOPIC_ROOT)
    )
    print(
        "ADSB.lol: {} centers radius={}nm interval={}s".format(
            len(POLL_CENTERS), args.radius, args.interval
        ),
        flush=True,
    )

    try:
        while True:
            seen: set[str] = set()
            published = 0
            for lat, lon in POLL_CENTERS:
                for aircraft in fetch_aircraft(lat, lon, args.radius):
                    icao24 = str(aircraft.get("hex") or "").strip().lower()
                    if icao24 and icao24 in seen:
                        continue
                    if icao24:
                        seen.add(icao24)
                    track = normalize(aircraft)
                    if track is None:
                        continue
                    publisher = military if track["is_military"] else civil
                    publisher.put(
                        json.dumps(track, separators=(",", ":")).encode(),
                        encoding=zenoh.Encoding.APPLICATION_JSON,
                    )
                    publisher_v2 = military_v2 if track["is_military"] else civil_v2
                    publisher_v2.put(
                        source_track_to_message(AdsbLolTrack, track).SerializeToString(),
                        encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
                    )
                    published += 1
            print("ADSB.lol tracks published: {}".format(published), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        civil.undeclare()
        military.undeclare()
        civil_v2.undeclare()
        military_v2.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ADSB.lol open-data API -> Zenoh")
    parser.add_argument("--radius", type=float, default=MAX_RADIUS_NM)
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
