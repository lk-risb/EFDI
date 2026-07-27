#!/usr/bin/env python3
"""airplaneslive_bridge.py — airplanes.live ADS-B API → Zenoh bridge.

Polls the airplanes.live v2 API for live aircraft positions in the Baltic
region and worldwide military traffic. No API key required.

Includes registration, aircraft type, detailed kinematics, autopilot
selections, and ADS-B quality values. Run alongside adsblol_bridge.py so two
independent receiver networks can improve combined coverage.

Zenoh topics:
  <ORG>/air/airplaneslive/adsb/json/tracks  — regional ADS-B
  <ORG>/air/airplaneslive/adsb/mil/v1     — worldwide military traffic

Proto schema: airplaneslive_track.proto (message AirplanesLiveTrack)

Run:
    venv/bin/python3 airplaneslive_bridge.py
    venv/bin/python3 airplaneslive_bridge.py --lat 57 --lon 24 --radius 400
"""

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request
from protocols.random.airplaneslive_track_pb2 import AirplanesLiveTrack

import zenoh
from http_json import read_json_response
from namespace_prefix import topic_root
from protocols.protobuf_codec import publish_collection
from zenoh_auth import apply_zenoh_auth

ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

BASE_URL      = "https://api.airplanes.live/v2"
# Five requests per full cycle (four point queries plus the military endpoint).
# The public free tier currently allows 500 requests/day, so 900 seconds keeps
# an always-on instance within that budget. ADSB.lol is the high-rate default.
POLL_INTERVAL = 900
MIL_INTERVAL  = 900
MIN_REQUEST_GAP_S = 1.05  # documented public API limit is one request/second
_last_request_at = 0.0

# API hard-limits radius to 250 nm per query. Poll multiple centers to cover
# the full operational area (20°N–73°N, 4°E–65°E).
MAX_RADIUS = 250  # nm — enforced by airplanes.live API (403 above this)
POLL_CENTERS = [
    (57.0, 22.0),   # Baltic / Scandinavia
    (53.0, 28.0),   # Belarus / western Ukraine
    (48.0, 32.0),   # Ukraine / Black Sea
    (44.0, 36.0),   # Crimea / eastern Black Sea
]


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


def fetch(url: str) -> list:
    global _last_request_at
    wait_s = MIN_REQUEST_GAP_S - (time.monotonic() - _last_request_at)
    if wait_s > 0:
        time.sleep(wait_s)
    _last_request_at = time.monotonic()
    req = urllib.request.Request(url, headers={
        "User-Agent": "efdi-airplaneslive-bridge/1.0",
        "Accept":     "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = read_json_response(resp)
            return data.get("ac", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("airplanes.live fetch error:", exc, flush=True)
        return []


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


def _copy_number(track: dict, ac: dict, target: str, source: str, cast=float) -> None:
    value = _number(ac.get(source), cast)
    if value is not None:
        track[target] = value


def _copy_text(track: dict, ac: dict, target: str, source: str) -> None:
    value = ac.get(source)
    if value is not None:
        text = str(value).strip()
        if text:
            track[target] = text


def normalize(ac: dict, is_military: bool) -> dict | None:
    lat = ac.get("lat")
    lon = ac.get("lon")
    if lat is None or lon is None:
        return None
    track: dict = {
        "_ts":            time.time(),
        "_src":           "airplaneslive",
        "icao24":         ac.get("hex", "").lower(),
        "callsign":       (ac.get("flight") or "").strip(),
        "registration":   (ac.get("r") or "").strip(),
        "aircraft_type":  (ac.get("t") or "").strip(),
        "lat_deg":        lat,
        "lon_deg":        lon,
        "squawk":         (ac.get("squawk") or ""),
        "is_military":    is_military,
    }

    # Preserve every stable, documented ADS-B field that is useful to an
    # operator. Missing values stay absent; zero is a valid measurement and is
    # no longer overloaded to mean "unknown".
    _copy_text(track, ac, "aircraft_description", "desc")
    _copy_text(track, ac, "pos_source", "type")
    alt_baro = ac.get("alt_baro")
    if isinstance(alt_baro, str) and alt_baro.strip().lower() == "ground":
        track["on_ground"] = True
    else:
        barometric_altitude = _number(alt_baro, int)
        if barometric_altitude is not None:
            track["alt_baro_ft"] = barometric_altitude
            track["on_ground"] = False
    _copy_number(track, ac, "alt_geom_ft", "alt_geom", int)
    for target, source, cast in (
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
        ("wind_dir_deg", "wd", float),
        ("wind_speed_kt", "ws", float),
        ("temp_c", "oat", float),
        ("total_air_temp_c", "tat", float),
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
        _copy_number(track, ac, target, source, cast)

    if "selected_alt_ft" in track:
        track["selected_alt_source"] = "MCP/FCU"
    _copy_text(track, ac, "sil_type", "sil_type")
    _copy_text(track, ac, "emergency_str", "emergency")
    if track.get("emergency_str") == "none":
        track.pop("emergency_str")
    _copy_text(track, ac, "emitter_category_str", "category")
    nav_modes = ac.get("nav_modes")
    if isinstance(nav_modes, list):
        modes = [str(mode).strip() for mode in nav_modes if str(mode).strip()]
        if modes:
            track["nav_modes"] = ", ".join(modes)
    if "spi" in ac:
        track["spi"] = bool(ac["spi"])
    if "alert" in ac:
        track["alert"] = bool(ac["alert"])
    flags = _number(ac.get("dbFlags"), int)
    if flags is not None:
        track["database_flags"] = flags
        track["is_interesting"] = bool(flags & 2)
        track["is_pia"] = bool(flags & 4)
        track["is_ladd"] = bool(flags & 8)
    # Route: departure → destination (IATA codes)
    dep = (ac.get("dep_iata") or ac.get("origin") or "").strip().upper()
    arr = (ac.get("arr_iata") or ac.get("destination") or "").strip().upper()
    if dep and arr:
        track["route"] = "{} → {}".format(dep, arr)
    elif dep:
        track["route"] = dep + " →"
    elif arr:
        track["route"] = "→ " + arr
    return track


def run(args):
    session = zenoh.open(make_config())
    pub_tracks = "{}/air/airplaneslive/adsb/civ/aircraft".format(TOPIC_ROOT)
    pub_mil = "{}/air/airplaneslive/adsb/mil/aircraft".format(TOPIC_ROOT)

    url_mil = "{}/mil".format(BASE_URL)
    print("airplanes.live: {} centers radius={}nm  poll={}s".format(
        len(POLL_CENTERS), MAX_RADIUS, args.interval), flush=True)

    last_mil = 0.0
    try:
        while True:
            seen = set()
            count = 0
            for lat, lon in POLL_CENTERS:
                url = "{}/point/{}/{}/{}".format(BASE_URL, lat, lon, MAX_RADIUS)
                for ac in fetch(url):
                    icao = ac.get("hex", "").lower()
                    if icao in seen:
                        continue
                    seen.add(icao)
                    track = normalize(ac, False)
                    if track is None:
                        continue
                    publish_collection(session, pub_tracks, track, AirplanesLiveTrack, zenoh)
                    count += 1
            print("airplaneslive tracks: {} ({} centers)".format(count, len(POLL_CENTERS)), flush=True)

            now = time.time()
            if now - last_mil >= MIL_INTERVAL:
                mil_count = 0
                for ac in fetch(url_mil):
                    track = normalize(ac, True)
                    if track is None:
                        continue
                    publish_collection(session, pub_mil, track, AirplanesLiveTrack, zenoh)
                    mil_count += 1
                print("airplaneslive mil: {}".format(mil_count), flush=True)
                last_mil = now

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def main():
    ap = argparse.ArgumentParser(description="airplanes.live ADS-B → Zenoh bridge")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: {})".format(POLL_INTERVAL))
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
