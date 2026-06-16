#!/usr/bin/env python3
"""openmeteo_bridge.py — Open-Meteo → Zenoh bridge.

Polls the Open-Meteo forecast API (free, no key) for current weather conditions
at configured Baltic locations and publishes each as a WeatherPoint JSON to the
EFDI Zenoh fabric.

Zenoh topic:  <ORG>/weather/openmeteo/<place>/current/v1
Proto schema: weather_point.proto  (message WeatherPoint, package ltu.cis.tracks.v1)

Run:
    venv/bin/python3 openmeteo_bridge.py
    venv/bin/python3 openmeteo_bridge.py --interval 900
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

OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
POLL_INTERVAL = 900  # 15 min — Open-Meteo current updates every 15 min

DEFAULT_LOCATIONS = [
    {"name": "vilnius",  "lat": 54.6872, "lon": 25.2797},
    {"name": "kaunas",   "lat": 54.8982, "lon": 23.9045},
    {"name": "klaipeda", "lat": 55.7127, "lon": 21.1351},
    {"name": "riga",     "lat": 56.9460, "lon": 24.1059},
    {"name": "tallinn",  "lat": 59.4370, "lon": 24.7536},
]

_CURRENT_VARS = ",".join([
    "temperature_2m", "relative_humidity_2m", "apparent_temperature",
    "precipitation", "weather_code", "cloud_cover", "pressure_msl",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
])


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


def fetch_current(lat: float, lon: float) -> dict | None:
    url = (
        "{}?latitude={}&longitude={}&current={}&wind_speed_unit=ms&timezone=UTC"
        .format(OPENMETEO_URL, lat, lon, _CURRENT_VARS)
    )
    req = urllib.request.Request(url, headers={"User-Agent": "efdi-openmeteo-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("Open-Meteo fetch error:", exc, flush=True)
        return None


def normalize(loc: dict, raw: dict) -> dict | None:
    cur = raw.get("current")
    if not cur:
        return None
    return {
        "_ts":                    time.time(),
        "_src":                   "openmeteo",
        "place_name":             loc["name"],
        "place_code":             loc["name"],
        "lat_deg":                loc["lat"],
        "lon_deg":                loc["lon"],
        "time_utc":               cur.get("time", ""),
        "temperature_c":          cur.get("temperature_2m"),
        "apparent_temperature_c": cur.get("apparent_temperature"),
        "relative_humidity_pct":  cur.get("relative_humidity_2m"),
        "precipitation_mm":       cur.get("precipitation"),
        "wind_speed_ms":          cur.get("wind_speed_10m"),
        "wind_direction_deg":     cur.get("wind_direction_10m"),
        "wind_gusts_ms":          cur.get("wind_gusts_10m"),
        "weather_code":           cur.get("weather_code"),
        "cloud_cover_pct":        cur.get("cloud_cover"),
        "pressure_hpa":           cur.get("pressure_msl"),
    }


def run(args):
    session = zenoh.open(make_config())
    publishers = {
        loc["name"]: session.declare_publisher(
            "{}/weather/{}/current/v1".format(ORG, loc["name"])
        )
        for loc in args.locations
    }
    print("Locations:", [l["name"] for l in args.locations], flush=True)
    print("Poll interval: {}s".format(args.interval), flush=True)

    try:
        while True:
            for loc in args.locations:
                raw = fetch_current(loc["lat"], loc["lon"])
                if raw is None:
                    continue
                point = normalize(loc, raw)
                if point is None:
                    continue
                payload = json.dumps(point)
                publishers[loc["name"]].put(payload.encode())
                print("PUB openmeteo/{} {}°C wind {}m/s".format(
                    loc["name"],
                    point.get("temperature_c"),
                    point.get("wind_speed_ms"),
                ), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for pub in publishers.values():
            pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Open-Meteo → Zenoh bridge")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 900)")
    ap.add_argument("--locations", type=json.loads, default=DEFAULT_LOCATIONS,
                    help='JSON list of {name,lat,lon} objects')
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
