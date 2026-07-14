#!/usr/bin/env python3
"""airplaneslive_bridge.py — airplanes.live ADS-B API → Zenoh bridge.

Polls the airplanes.live v2 API for live aircraft positions in the Baltic
region and worldwide military traffic. No API key required.

Advantages over OpenSky: includes registration and aircraft type; no rate
limits. Run alongside opensky_bridge.py — different receiver networks give
better combined coverage.

Zenoh topics:
  <ORG>/air/airplaneslive/tracks/v1  — regional ADS-B
  <ORG>/air/airplaneslive/mil/v1     — worldwide military traffic

Proto schema: airplaneslive_track.proto (message AirplanesLiveTrack)

Run:
    venv/bin/python3 airplaneslive_bridge.py
    venv/bin/python3 airplaneslive_bridge.py --lat 57 --lon 24 --radius 400
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

import zenoh
from http_json import read_json_response
from namespace_prefix import prefix

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG   # org prefix (configurable) precedes the pod namespace
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

BASE_URL      = "https://api.airplanes.live/v2"
POLL_INTERVAL = 5    # seconds — no documented rate limit
MIL_INTERVAL  = 5    # military endpoint — same rate as civil

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


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize(ac: dict, is_military: bool) -> dict | None:
    lat = ac.get("lat")
    lon = ac.get("lon")
    if lat is None or lon is None:
        return None
    track = {
        "_ts":            time.time(),
        "_src":           "airplaneslive",
        "icao24":         ac.get("hex", "").lower(),
        "callsign":       (ac.get("flight") or "").strip(),
        "registration":   (ac.get("r") or "").strip(),
        "aircraft_type":  (ac.get("t") or "").strip(),
        "lat_deg":        lat,
        "lon_deg":        lon,
        "alt_baro_ft":    _int(ac.get("alt_baro")),
        "alt_geom_ft":    _int(ac.get("alt_geom")),
        "ground_speed_kts": float(ac.get("gs") or 0),
        "track_deg":      float(ac.get("track") or 0),
        "squawk":         (ac.get("squawk") or ""),
        "is_military":    is_military,
    }
    # Route: departure → destination (IATA codes)
    dep = (ac.get("dep_iata") or ac.get("origin") or "").strip().upper()
    arr = (ac.get("arr_iata") or ac.get("destination") or "").strip().upper()
    if dep and arr:
        track["route"] = "{} → {}".format(dep, arr)
    elif dep:
        track["route"] = dep + " →"
    elif arr:
        track["route"] = "→ " + arr
    # RSSI — signal strength from the best receiving station, in dBFS
    rssi = ac.get("rssi")
    if rssi is not None:
        try:
            track["rssi_db"] = round(float(rssi), 1)
        except (TypeError, ValueError):
            pass
    return track


def run(args):
    session = zenoh.open(make_config())
    pub_tracks = session.declare_publisher("{}/air/airplaneslive/adsb/civ/aircraft/tracks/v1".format(TOPIC_ROOT))
    pub_mil    = session.declare_publisher("{}/air/airplaneslive/adsb/mil/aircraft/tracks/v1".format(TOPIC_ROOT))

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
                    pub_tracks.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                    count += 1
            print("airplaneslive tracks: {} ({} centers)".format(count, len(POLL_CENTERS)), flush=True)

            now = time.time()
            if now - last_mil >= MIL_INTERVAL:
                mil_count = 0
                for ac in fetch(url_mil):
                    track = normalize(ac, True)
                    if track is None:
                        continue
                    pub_mil.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                    mil_count += 1
                print("airplaneslive mil: {}".format(mil_count), flush=True)
                last_mil = now

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub_tracks.undeclare()
        pub_mil.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="airplanes.live ADS-B → Zenoh bridge")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: {})".format(POLL_INTERVAL))
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
