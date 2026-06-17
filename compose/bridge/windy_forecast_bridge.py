#!/usr/bin/env python3
"""windy_bridge.py — Windy Point Forecast API → Zenoh bridge.

Polls the Windy.com Point Forecast API v2 for surface and upper-level wind,
temperature and humidity at configured locations and publishes one WindyForecast
JSON per location per forecast hour.

Free API key: https://api.windy.com/  (instant, 1000 calls/day on free plan)
Model "gfs" is available on all plans; "ecmwf" requires higher tiers.

Zenoh topic:  <ORG>/weather/windy/<place>/forecast/v1
Proto schema: windy_forecast.proto  (message WindyForecast, package ltu.cis.tracks.v1)

Run:
    WINDY_KEY=<key> venv/bin/python3 windy_bridge.py
    WINDY_KEY=<key> venv/bin/python3 windy_bridge.py --model icon --hours 12
"""

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = "1851281db70ccc0409dad4ecfc874cf5"
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

WINDY_URL     = "https://api.windy.com/api/point-forecast/v2"
POLL_INTERVAL = 3600  # 1 h — free plan: 1000 calls/day, don't burn them

DEFAULT_LOCATIONS = [
    # Baltic states
    {"name": "vilnius",    "lat": 54.6872, "lon": 25.2797},
    {"name": "kaunas",     "lat": 54.8982, "lon": 23.9045},
    {"name": "klaipeda",   "lat": 55.7127, "lon": 21.1351},
    {"name": "riga",       "lat": 56.9460, "lon": 24.1059},
    {"name": "tallinn",    "lat": 59.4370, "lon": 24.7536},
    {"name": "kaliningrad","lat": 54.7104, "lon": 20.4522},
    # Scandinavia
    {"name": "helsinki",   "lat": 60.1699, "lon": 24.9384},
    {"name": "stockholm",  "lat": 59.3293, "lon": 18.0686},
    {"name": "oslo",       "lat": 59.9139, "lon": 10.7522},
    # Eastern Europe / conflict zone
    {"name": "warsaw",     "lat": 52.2297, "lon": 21.0122},
    {"name": "kyiv",       "lat": 50.4501, "lon": 30.5234},
    {"name": "minsk",      "lat": 53.9045, "lon": 27.5615},
    {"name": "moscow",     "lat": 55.7558, "lon": 37.6173},
    # Middle East / conflict zone
    {"name": "istanbul",   "lat": 41.0082, "lon": 28.9784},
    {"name": "beirut",     "lat": 33.8886, "lon": 35.4955},
    {"name": "tel_aviv",   "lat": 32.0853, "lon": 34.7818},
    {"name": "baghdad",    "lat": 33.3152, "lon": 44.3661},
    {"name": "tehran",     "lat": 35.6892, "lon": 51.3890},
]

_PARAMS   = ["wind", "temp", "pressure", "rh", "dewpoint"]
_LEVELS   = ["surface", "850h", "300h"]


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


def fetch_forecast(api_key: str, lat: float, lon: float, model: str) -> dict | None:
    body = json.dumps({
        "lat": lat, "lon": lon,
        "model": model,
        "parameters": _PARAMS,
        "levels": _LEVELS,
        "key": api_key,
    }).encode()
    req = urllib.request.Request(WINDY_URL, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent":   "efdi-windy-bridge/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("Windy fetch error:", exc, flush=True)
        return None


def _k_to_c(k) -> float | None:
    return round(k - 273.15, 2) if k is not None else None

def _pa_to_hpa(pa) -> float | None:
    return round(pa / 100.0, 1) if pa is not None else None

def _wind_speed(u, v) -> float | None:
    if u is None or v is None:
        return None
    return round(math.hypot(u, v), 2)

def _wind_dir(u, v) -> float | None:
    if u is None or v is None:
        return None
    return round((270 - math.degrees(math.atan2(v, u))) % 360, 1)

def _get(d: dict, key: str, idx: int):
    arr = d.get(key, [])
    return arr[idx] if idx < len(arr) else None


def normalize_step(loc: dict, raw: dict, model: str, idx: int, ts_ms: int) -> dict:
    time_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    u_surf = _get(raw, "wind_u-surface", idx)
    v_surf = _get(raw, "wind_v-surface", idx)
    u_850  = _get(raw, "wind_u-850h",   idx)
    v_850  = _get(raw, "wind_v-850h",   idx)
    u_300  = _get(raw, "wind_u-300h",   idx)
    v_300  = _get(raw, "wind_v-300h",   idx)

    point = {
        "_ts":          time.time(),
        "_src":         "windy",
        "place_name":   loc["name"],
        "model":        model,
        "lat_deg":      loc["lat"],
        "lon_deg":      loc["lon"],
        "time_utc":     time_utc,
        "wind_u_ms":    u_surf,
        "wind_v_ms":    v_surf,
        "wind_speed_ms": _wind_speed(u_surf, v_surf),
        "wind_dir_deg":  _wind_dir(u_surf, v_surf),
        "temp_c":        _k_to_c(_get(raw, "temp-surface", idx)),
        "pressure_hpa":  _pa_to_hpa(_get(raw, "pressure-surface", idx)),
        "rh_pct":        _get(raw, "rh-surface", idx),
        "dewpoint_c":    _k_to_c(_get(raw, "dewpoint-surface", idx)),
        "wind_u_850h_ms": u_850,
        "wind_v_850h_ms": v_850,
        "temp_850h_c":    _k_to_c(_get(raw, "temp-850h", idx)),
        "wind_u_300h_ms": u_300,
        "wind_v_300h_ms": v_300,
    }
    return {k: v for k, v in point.items() if v is not None}


def run(args):
    if not args.key:
        raise SystemExit(
            "WINDY_KEY not set — get a free key at https://api.windy.com/"
        )

    session = zenoh.open(make_config())
    publishers = {
        loc["name"]: session.declare_publisher(
            "{}/env/windy/rest/neutral/weather/forecast/v1/{}".format(ORG, loc["name"])
        )
        for loc in args.locations
    }
    print("Windy model:", args.model, "  hours:", args.hours, flush=True)

    try:
        while True:
            for loc in args.locations:
                raw = fetch_forecast(args.key, loc["lat"], loc["lon"], args.model)
                if raw is None:
                    continue
                timestamps = raw.get("ts", [])
                pub = publishers[loc["name"]]
                count = 0
                for idx, ts_ms in enumerate(timestamps[:args.hours]):
                    point = normalize_step(loc, raw, args.model, idx, ts_ms)
                    pub.put(json.dumps(point).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                    count += 1
                print("PUB windy/{} {} steps ({})".format(
                    loc["name"], count,
                    raw.get("warning", "ok")), flush=True)
                time.sleep(0.3)  # polite gap
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for pub in publishers.values():
            pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Windy Point Forecast → Zenoh bridge")
    ap.add_argument("--key", default=os.environ.get("WINDY_KEY", ""),
                    help="Windy API key (or set WINDY_KEY env var)")
    ap.add_argument("--model", default="gfs",
                    help="NWP model: gfs (free), ecmwf/icon (paid tiers). Default: gfs")
    ap.add_argument("--hours", type=int, default=24,
                    help="Number of forecast hours to publish per location (default: 24)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 3600)")
    ap.add_argument("--locations", type=json.loads, default=DEFAULT_LOCATIONS,
                    help='JSON list of {name,lat,lon} objects')
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
