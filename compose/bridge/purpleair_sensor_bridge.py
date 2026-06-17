#!/usr/bin/env python3
"""purpleair_bridge.py — PurpleAir → Zenoh bridge.

Queries the PurpleAir REST API for outdoor air quality sensors within a
bounding box and publishes each sensor reading as an AirQualityReading JSON.

Free read API key: https://develop.purpleair.com/ (register, instant approval)
Rate limit: 100 requests/minute on free tier.

Zenoh topic:  <ORG>/env/air_quality/v1
Proto schema: air_quality.proto  (message AirQualityReading, package ltu.cis.tracks.v1)

Run:
    PURPLEAIR_KEY=<key> venv/bin/python3 purpleair_bridge.py
    PURPLEAIR_KEY=<key> venv/bin/python3 purpleair_bridge.py --interval 300
"""

import argparse
import json
import math
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

PURPLEAIR_URL = "https://api.purpleair.com/v1/sensors"
POLL_INTERVAL = 300  # 5 min — sensors update ~2 min, free tier rate-friendly

# Operational bbox: Baltic + Scandinavia + E.Europe + Middle East
DEFAULT_BBOX = {"nwlat": 62.0, "nwlng": 14.0, "selat": 41.0, "selng": 45.0}

_FIELDS = "sensor_index,name,latitude,longitude,pm1.0,pm2.5,pm10.0,pm2.5_cf_1,temperature,humidity,pressure,confidence"

_AQI_BREAKPOINTS = [
    (0,   12.0,  0,   50,  "Good"),
    (12.1, 35.4, 51,  100, "Moderate"),
    (35.5, 55.4, 101, 150, "Unhealthy for Sensitive Groups"),
    (55.5, 150.4, 151, 200, "Unhealthy"),
    (150.5, 250.4, 201, 300, "Very Unhealthy"),
    (250.5, 500.4, 301, 500, "Hazardous"),
]


def _pm25_to_aqi(pm25: float) -> tuple[int, str]:
    for c_lo, c_hi, i_lo, i_hi, label in _AQI_BREAKPOINTS:
        if c_lo <= pm25 <= c_hi:
            aqi = round((i_hi - i_lo) / (c_hi - c_lo) * (pm25 - c_lo) + i_lo)
            return aqi, label
    return 0, "Unknown"


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


def fetch_sensors(api_key: str, bbox: dict) -> list:
    params = "&".join("{}={}".format(k, v) for k, v in bbox.items())
    url = "{}?fields={}&location_type=0&{}".format(PURPLEAIR_URL, _FIELDS, params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "efdi-purpleair-bridge/1.0",
        "X-API-Key":  api_key,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        field_names = data.get("fields", [])
        sensors = []
        for row in data.get("data", []):
            sensors.append(dict(zip(field_names, row)))
        return sensors
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("PurpleAir fetch error:", exc, flush=True)
        return []


def normalize(sensor: dict) -> dict | None:
    lat = sensor.get("latitude")
    lon = sensor.get("longitude")
    if lat is None or lon is None:
        return None

    pm25 = sensor.get("pm2.5", 0.0) or 0.0
    aqi, category = _pm25_to_aqi(float(pm25))

    # PurpleAir temperature is in °F
    temp_f = sensor.get("temperature")
    temp_c = round((float(temp_f) - 32) * 5 / 9, 1) if temp_f is not None else None

    return {
        "_ts":                   time.time(),
        "_src":                  "purpleair",
        "sensor_id":             sensor.get("sensor_index"),
        "sensor_name":           sensor.get("name", ""),
        "lat_deg":               lat,
        "lon_deg":               lon,
        "pm1_ugm3":              sensor.get("pm1.0"),
        "pm25_ugm3":             pm25,
        "pm10_ugm3":             sensor.get("pm10.0"),
        "pm25_cf1_ugm3":         sensor.get("pm2.5_cf_1"),
        "temperature_c":         temp_c,
        "relative_humidity_pct": sensor.get("humidity"),
        "pressure_hpa":          sensor.get("pressure"),
        "aqi":                   aqi,
        "aqi_category":          category,
        "confidence":            sensor.get("confidence", 0),
    }


def run(args):
    if not args.key:
        raise SystemExit("PURPLEAIR_KEY not set — get a free key at https://develop.purpleair.com/")

    session = zenoh.open(make_config())
    pub = session.declare_publisher("{}/env/purpleair/rest/neutral/sensor/sensors/v1".format(ORG))

    print("Bounding box:", args.bbox, flush=True)

    try:
        while True:
            sensors = fetch_sensors(args.key, args.bbox)
            print("Fetched {} PurpleAir sensors".format(len(sensors)), flush=True)
            for s in sensors:
                point = normalize(s)
                if point is None:
                    continue
                sid = point["sensor_id"]
                if sid is None:
                    continue
                pub.put(json.dumps(point).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="PurpleAir → Zenoh bridge")
    ap.add_argument("--key", default=os.environ.get("PURPLEAIR_KEY", ""),
                    help="PurpleAir read API key (or set PURPLEAIR_KEY env var)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 300)")
    ap.add_argument("--bbox", type=json.loads,
                    default=DEFAULT_BBOX,
                    help='JSON object with nwlat,nwlng,selat,selng keys')
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
