#!/usr/bin/env python3
"""meteo_lt_bridge.py — meteo.lt → Zenoh bridge.

Polls the Lithuanian Hydrometeorological Service public REST API (api.meteo.lt,
no key required) for long-term forecasts at configured Lithuanian cities and
publishes the nearest upcoming forecast timestamp as a WeatherPoint JSON to the
EFDI Zenoh fabric.

Zenoh topic:  <ORG>/weather/meteo-lt/<place-code>/forecast/v1
Proto schema: weather_point.proto  (message WeatherPoint, package ltu.cis.tracks.v1)

Run:
    venv/bin/python3 meteo_lt_bridge.py
    venv/bin/python3 meteo_lt_bridge.py --places vilnius kaunas klaipeda
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = "LTU/CISB/" + ORG   # organization prefix precedes the pod namespace
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

METEO_LT_BASE = "https://api.meteo.lt/v1/places"
POLL_INTERVAL  = 3600  # 1 h — forecasts update every ~6 h, polling hourly is plenty

DEFAULT_PLACES = ["vilnius", "kaunas", "klaipeda", "siauliai", "panevezys"]


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


def fetch_forecast(place: str) -> dict | None:
    url = "{}/{}/forecasts/long-term".format(METEO_LT_BASE, place)
    req = urllib.request.Request(url, headers={"User-Agent": "efdi-meteo-lt-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("meteo.lt fetch error for {}: {}".format(place, exc), flush=True)
        return None


def nearest_future(timestamps: list) -> dict | None:
    """Return the first timestamp that is at or after the current UTC hour."""
    now_s = time.time()
    for ts in timestamps:
        # format: "2026-06-15 21:00:00" — parse as UTC epoch
        try:
            import datetime
            dt = datetime.datetime.strptime(ts["forecastTimeUtc"], "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            if dt.timestamp() >= now_s - 3600:
                return ts
        except (ValueError, KeyError):
            continue
    return None


def normalize(place_meta: dict, ts: dict) -> dict:
    place = place_meta.get("place", {})
    coords = place.get("coordinates", {})
    return {
        "_ts":                    time.time(),
        "_src":                   "meteo-lt",
        "place_name":             place.get("name", ""),
        "place_code":             place.get("code", ""),
        "lat_deg":                coords.get("latitude", 0.0),
        "lon_deg":                coords.get("longitude", 0.0),
        "time_utc":               ts.get("forecastTimeUtc", ""),
        "temperature_c":          ts.get("airTemperature"),
        "apparent_temperature_c": ts.get("feelsLikeTemperature"),
        "relative_humidity_pct":  ts.get("relativeHumidity"),
        "precipitation_mm":       ts.get("totalPrecipitation"),
        "wind_speed_ms":          ts.get("windSpeed"),
        "wind_direction_deg":     ts.get("windDirection"),
        "wind_gusts_ms":          ts.get("windGust"),
        "cloud_cover_pct":        ts.get("cloudCover"),
        "pressure_hpa":           ts.get("seaLevelPressure"),
        "condition_code":         ts.get("conditionCode", ""),
    }


def run(args):
    session = zenoh.open(make_config())
    publishers = {
        place: session.declare_publisher(
            "{}/env/weather/station/meteolt/forecast/{}".format(TOPIC_ROOT, place)
        )
        for place in args.places
    }
    print("Places:", args.places, flush=True)
    print("Poll interval: {}s".format(args.interval), flush=True)

    try:
        while True:
            for place in args.places:
                raw = fetch_forecast(place)
                if raw is None:
                    continue
                ts = nearest_future(raw.get("forecastTimestamps", []))
                if ts is None:
                    print("meteo.lt: no upcoming timestamps for", place, flush=True)
                    continue
                point = normalize(raw, ts)
                payload = json.dumps(point)
                publishers[place].put(payload.encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                print("PUB meteo-lt/{} {} {}°C {}".format(
                    place,
                    ts.get("forecastTimeUtc", "")[:16],
                    ts.get("airTemperature"),
                    ts.get("conditionCode", ""),
                ), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for pub in publishers.values():
            pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="meteo.lt → Zenoh bridge")
    ap.add_argument("--places", nargs="+", default=DEFAULT_PLACES,
                    help="meteo.lt place codes (default: 5 major LT cities)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 3600)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
