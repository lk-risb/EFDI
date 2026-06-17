#!/usr/bin/env python3
"""yr_no_bridge.py — MET Norway (yr.no) Locationforecast 2.0 → Zenoh bridge.

Polls the Norwegian Meteorological Institute's free forecast API for current
conditions at configured Baltic/Nordic locations and publishes each as a
WeatherPoint JSON. No API key — requires a descriptive User-Agent per their ToS.

Zenoh topic:  <ORG>/env/weather/forecast/v1/<place>
Proto schema: weather_point.proto  (message WeatherPoint, package ltu.cis.tracks.v1)

Run:
    venv/bin/python3 yr_no_bridge.py
    venv/bin/python3 yr_no_bridge.py --interval 1800
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
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

YR_URL       = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# MET Norway requires a User-Agent that identifies your app and contact.
YR_UA        = "efdi-yr-bridge/1.0 gabrielius.ndukve@gmail.com"
POLL_INTERVAL = 1800  # 30 min — forecasts update ~1h but caching keeps load low

DEFAULT_LOCATIONS = [
    # Baltic states
    {"name": "vilnius",     "lat": 54.6872, "lon": 25.2797},
    {"name": "kaunas",      "lat": 54.8982, "lon": 23.9045},
    {"name": "klaipeda",    "lat": 55.7127, "lon": 21.1351},
    {"name": "kaliningrad", "lat": 54.7104, "lon": 20.4522},
    {"name": "riga",        "lat": 56.9460, "lon": 24.1059},
    {"name": "tallinn",     "lat": 59.4370, "lon": 24.7536},
    # Scandinavia
    {"name": "helsinki",    "lat": 60.1699, "lon": 24.9384},
    {"name": "stockholm",   "lat": 59.3293, "lon": 18.0686},
    {"name": "oslo",        "lat": 59.9139, "lon": 10.7522},
    # Eastern Europe / conflict zone
    {"name": "warsaw",      "lat": 52.2297, "lon": 21.0122},
    {"name": "kyiv",        "lat": 50.4501, "lon": 30.5234},
    {"name": "minsk",       "lat": 53.9045, "lon": 27.5615},
    {"name": "moscow",      "lat": 55.7558, "lon": 37.6173},
    # Middle East / conflict zone
    {"name": "istanbul",    "lat": 41.0082, "lon": 28.9784},
    {"name": "beirut",      "lat": 33.8886, "lon": 35.4955},
    {"name": "tel_aviv",    "lat": 32.0853, "lon": 34.7818},
    {"name": "baghdad",     "lat": 33.3152, "lon": 44.3661},
    {"name": "tehran",      "lat": 35.6892, "lon": 51.3890},
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


def fetch_forecast(lat: float, lon: float) -> dict | None:
    url = "{}?lat={}&lon={}".format(YR_URL, lat, lon)
    req = urllib.request.Request(url, headers={"User-Agent": YR_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("yr.no fetch error:", exc, flush=True)
        return None


def normalize(loc: dict, raw: dict) -> dict | None:
    series = raw.get("properties", {}).get("timeseries", [])
    if not series:
        return None
    # First entry is the nearest forecast time (usually current or next hour)
    entry   = series[0]
    instant = entry.get("data", {}).get("instant", {}).get("details", {})
    next1h  = entry.get("data", {}).get("next_1_hours", {})
    symbol  = next1h.get("summary", {}).get("symbol_code", "")
    precip  = next1h.get("details", {}).get("precipitation_amount", 0.0)

    return {
        "_ts":                    time.time(),
        "_src":                   "yr-no",
        "place_name":             loc["name"],
        "place_code":             loc["name"],
        "lat_deg":                loc["lat"],
        "lon_deg":                loc["lon"],
        "time_utc":               entry.get("time", ""),
        "temperature_c":          instant.get("air_temperature"),
        "apparent_temperature_c": None,
        "relative_humidity_pct":  instant.get("relative_humidity"),
        "precipitation_mm":       precip,
        "wind_speed_ms":          instant.get("wind_speed"),
        "wind_direction_deg":     instant.get("wind_from_direction"),
        "wind_gusts_ms":          instant.get("wind_speed_of_gust", 0.0),
        "weather_code":           0,  # yr.no uses symbol codes, not WMO ints
        "cloud_cover_pct":        instant.get("cloud_area_fraction"),
        "pressure_hpa":           instant.get("air_pressure_at_sea_level"),
        "condition_code":         symbol,
    }


def run(args):
    session = zenoh.open(make_config())
    publishers = {
        loc["name"]: session.declare_publisher(
            "{}/env/weather/station/yrno/forecast/{}".format(ORG, loc["name"])
        )
        for loc in args.locations
    }
    print("Locations:", [l["name"] for l in args.locations], flush=True)

    try:
        while True:
            for loc in args.locations:
                raw = fetch_forecast(loc["lat"], loc["lon"])
                if raw is None:
                    continue
                point = normalize(loc, raw)
                if point is None:
                    continue
                # Strip None values — proto zero-value convention
                point = {k: v for k, v in point.items() if v is not None}
                payload = json.dumps(point)
                publishers[loc["name"]].put(payload.encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                print("PUB yr-no/{} {}°C {} {}m/s".format(
                    loc["name"],
                    point.get("temperature_c"),
                    point.get("condition_code", ""),
                    point.get("wind_speed_ms"),
                ), flush=True)
                time.sleep(0.5)  # be polite between requests
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for pub in publishers.values():
            pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="yr.no → Zenoh bridge")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 1800)")
    ap.add_argument("--locations", type=json.loads, default=DEFAULT_LOCATIONS,
                    help='JSON list of {name,lat,lon} objects')
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
