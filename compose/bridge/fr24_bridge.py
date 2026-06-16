#!/usr/bin/env python3
"""fr24_bridge.py — FlightRadar24 API → Zenoh bridge.

Streams live aircraft positions from the FlightRadar24 v1 API and publishes
each as an Fr24Track JSON. Includes registration, type, and airline — fields
OpenSky does not provide. Use both bridges for complementary coverage.

API key: https://fr24api.flightradar24.com/key-management  (free tier: limited credits)
Credit cost: varies by endpoint — check your dashboard at fr24api.flightradar24.com

Zenoh topic:  <ORG>/air/fr24/tracks/v1
Proto schema: fr24_track.proto  (message Fr24Track, package ltu.cis.tracks.v1)

Run:
    FR24_KEY=<key> venv/bin/python3 fr24_bridge.py
    FR24_KEY=<key> venv/bin/python3 fr24_bridge.py --interval 60
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

FR24_BASE     = "https://fr24api.flightradar24.com/api/live"
POLL_INTERVAL = 60  # seconds — free tier is credit-limited, don't poll faster

# Baltic + surrounding region: south,north,west,east (FR24 uses lat_min,lat_max,lon_min,lon_max)
DEFAULT_BOUNDS = "-90,90,-180,180"  # worldwide


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


def fetch_flights(api_key: str, bounds: str) -> list:
    url = "{}/flight-positions/light?bounds={}".format(FR24_BASE, bounds)
    req = urllib.request.Request(url, headers={
        "User-Agent":    "efdi-fr24-bridge/1.0",
        "Accept":        "application/json",
        "Authorization": "Bearer {}".format(api_key),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("data", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("FR24 fetch error:", exc, flush=True)
        return []


def normalize(flight: dict) -> dict | None:
    lat = flight.get("lat")
    lon = flight.get("lon")
    if lat is None or lon is None:
        return None
    return {
        "_ts":          time.time(),
        "_src":         "fr24",
        "fr24_id":      flight.get("fr24_id", flight.get("id", "")),
        "callsign":     (flight.get("callsign") or "").strip(),
        "registration": (flight.get("reg") or "").strip(),
        "aircraft_type": (flight.get("type") or "").strip(),
        "lat_deg":      lat,
        "lon_deg":      lon,
        "alt_ft":       int(flight.get("alt", 0) or 0),
        "speed_kts":    int(flight.get("speed", 0) or 0),
        "heading_deg":  int(flight.get("heading", 0) or 0),
        "on_ground":    bool(flight.get("on_ground", False)),
        "painted_as":   (flight.get("painted_as") or "").strip(),
        "operating_as": (flight.get("operating_as") or "").strip(),
    }


def run(args):
    if not args.key:
        raise SystemExit(
            "FR24_KEY not set — get a key at https://fr24api.flightradar24.com/key-management"
        )

    session = zenoh.open(make_config())
    pub = session.declare_publisher("{}/air/civ/tracks/v1".format(ORG))
    print("Bounds:", args.bounds, "  interval:", args.interval, "s", flush=True)

    try:
        while True:
            flights = fetch_flights(args.key, args.bounds)
            published = 0
            for f in flights:
                track = normalize(f)
                if track is None:
                    continue
                pub.put(json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                published += 1
            print("FR24 poll: {} flights published".format(published), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="FlightRadar24 → Zenoh bridge")
    ap.add_argument("--key", default=os.environ.get("FR24_KEY", ""),
                    help="FR24 API Bearer token (or set FR24_KEY env var)")
    ap.add_argument("--bounds", default=DEFAULT_BOUNDS,
                    help="lat_min,lat_max,lon_min,lon_max (default: Baltic region)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 60)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
