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

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = "1851281db70ccc0409dad4ecfc874cf5"
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)

BASE_URL      = "https://api.airplanes.live/v2"
POLL_INTERVAL = 30   # seconds — no strict rate limit, but be polite
MIL_INTERVAL  = 60   # military endpoint — less time-critical

# Baltic region center + radius covering ~53-66°N, 9-32°E
DEFAULT_LAT    = 58.5
DEFAULT_LON    = 20.5
DEFAULT_RADIUS = 9999  # nautical miles — effectively worldwide


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([ROUTER]))
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
            data = json.loads(resp.read().decode())
            return data.get("ac", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("airplanes.live fetch error:", exc, flush=True)
        return []


def normalize(ac: dict, is_military: bool) -> dict | None:
    lat = ac.get("lat")
    lon = ac.get("lon")
    if lat is None or lon is None:
        return None
    return {
        "_ts":            time.time(),
        "_src":           "airplaneslive",
        "icao24":         ac.get("hex", "").lower(),
        "callsign":       (ac.get("flight") or "").strip(),
        "registration":   (ac.get("r") or "").strip(),
        "aircraft_type":  (ac.get("t") or "").strip(),
        "lat_deg":        lat,
        "lon_deg":        lon,
        "alt_baro_ft":    int(ac.get("alt_baro") or 0),
        "alt_geom_ft":    int(ac.get("alt_geom") or 0),
        "ground_speed_kts": float(ac.get("gs") or 0),
        "track_deg":      float(ac.get("track") or 0),
        "squawk":         (ac.get("squawk") or ""),
        "is_military":    is_military,
    }


def run(args):
    session = zenoh.open(make_config())
    pub_tracks = session.declare_publisher("{}/air/civ/tracks/v1".format(ORG))
    pub_mil    = session.declare_publisher("{}/air/mil/tracks/v1".format(ORG))

    url_region = "{}/point/{}/{}/{}".format(BASE_URL, args.lat, args.lon, args.radius)
    url_mil    = "{}/mil".format(BASE_URL)

    print("airplanes.live: center {}/{} radius={}nm  poll={}s".format(
        args.lat, args.lon, args.radius, args.interval), flush=True)

    last_mil = 0.0
    try:
        while True:
            aircraft = fetch(url_region)
            count = 0
            for ac in aircraft:
                track = normalize(ac, False)
                if track is None:
                    continue
                pub_tracks.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                count += 1
            print("airplaneslive tracks: {}".format(count), flush=True)

            now = time.time()
            if now - last_mil >= MIL_INTERVAL:
                mil = fetch(url_mil)
                mil_count = 0
                for ac in mil:
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
    ap.add_argument("--lat",    type=float, default=DEFAULT_LAT,
                    help="Center latitude for regional poll (default: {})".format(DEFAULT_LAT))
    ap.add_argument("--lon",    type=float, default=DEFAULT_LON,
                    help="Center longitude for regional poll (default: {})".format(DEFAULT_LON))
    ap.add_argument("--radius", type=int,   default=DEFAULT_RADIUS,
                    help="Radius in nautical miles (default: {})".format(DEFAULT_RADIUS))
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: {})".format(POLL_INTERVAL))
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
