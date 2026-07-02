#!/usr/bin/env python3
"""n2yo_bridge.py — N2YO satellite tracker → Zenoh bridge.

Polls the N2YO REST API (free, up to 1000 tx/h) for real-time positions of
configured satellites as seen from the observer location, and publishes each
as a SatellitePosition JSON to the EFDI Zenoh fabric.

Free API key: https://www.n2yo.com/api/  (register, key is instant)

Zenoh topic:  <ORG>/space/n2yo/<sat-id>/position/v1
Proto schema: satellite_pass.proto  (message SatellitePosition, package ltu.cis.tracks.v1)

Run:
    N2YO_KEY=<key> venv/bin/python3 n2yo_bridge.py
    N2YO_KEY=<key> venv/bin/python3 n2yo_bridge.py --interval 60 --sats 25544 20580
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

N2YO_BASE    = "https://api.n2yo.com/rest/v1/satellite"
POLL_INTERVAL = 60  # seconds — N2YO free tier: 1000 tx/h total

# Default observer: Vilnius, Lithuania, 100 m ASL
OBS_LAT = 54.6872
OBS_LON = 25.2797
OBS_ALT = 0.1  # km

# Default satellite set: ISS, Hubble, Sentinel-2A, Sentinel-1A, Starlink-1007
DEFAULT_SATS = [25544, 20580, 40697, 39634, 44713]

_SAT_NAMES = {
    25544: "SPACE STATION (ISS)",
    20580: "SPACE TELESCOPE (HST)",
    40697: "SENTINEL (2A)",
    39634: "SENTINEL (1A)",
    44713: "STARLINK (1007)",
}


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


def fetch_position(sat_id: int, api_key: str,
                   lat: float, lon: float, alt: float) -> dict | None:
    # seconds=1 requests a single position snapshot (1 transaction)
    url = "{}/positions/{}/{}/{}/{}/1/&apiKey={}".format(
        N2YO_BASE, sat_id, lat, lon, alt, api_key)
    req = urllib.request.Request(url, headers={"User-Agent": "efdi-n2yo-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("N2YO fetch error for sat {}: {}".format(sat_id, exc), flush=True)
        return None


def normalize(sat_id: int, raw: dict,
              obs_lat: float, obs_lon: float, obs_alt: float) -> dict | None:
    info = raw.get("info", {})
    positions = raw.get("positions", [])
    if not positions:
        return None
    pos = positions[0]
    return {
        "_ts":          time.time(),
        "_src":         "n2yo",
        "sat_id":       sat_id,
        "sat_name":     _SAT_NAMES.get(sat_id, info.get("satname", str(sat_id))),
        "lat_deg":      pos.get("satlatitude"),
        "lon_deg":      pos.get("satlongitude"),
        "alt_km":       pos.get("sataltitude"),
        "azimuth_deg":  pos.get("azimuth"),
        "elevation_deg": pos.get("elevation"),
        "ra":           pos.get("ra"),
        "dec":          pos.get("dec"),
        "eclipsed":     1 if pos.get("eclipsed") else 0,
        "obs_lat_deg":  obs_lat,
        "obs_lon_deg":  obs_lon,
        "obs_alt_km":   obs_alt,
    }


def run(args):
    if not args.key:
        raise SystemExit("N2YO_KEY not set — get a free key at https://www.n2yo.com/api/")

    session = zenoh.open(make_config())
    pub = session.declare_publisher("{}/space/n2yo/satpos/civ/satellite/tracks/v1".format(TOPIC_ROOT))
    print("Satellites:", args.sats, flush=True)
    print("Observer: lat={} lon={} alt={}km".format(args.lat, args.lon, args.alt), flush=True)

    try:
        while True:
            for sat_id in args.sats:
                raw = fetch_position(sat_id, args.key, args.lat, args.lon, args.alt)
                if raw is None:
                    continue
                point = normalize(sat_id, raw, args.lat, args.lon, args.alt)
                if point is None:
                    continue
                payload = json.dumps(point)
                pub.put(payload.encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                print("PUB n2yo/{} {} el={:.1f}° az={:.1f}°".format(
                    sat_id,
                    point.get("sat_name", ""),
                    point.get("elevation_deg", 0),
                    point.get("azimuth_deg", 0),
                ), flush=True)
                time.sleep(1)  # 1 tx per satellite per second max
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="N2YO satellite tracker → Zenoh bridge")
    ap.add_argument("--key", default=os.environ.get("N2YO_KEY", ""),
                    help="N2YO API key (or set N2YO_KEY env var)")
    ap.add_argument("--sats", nargs="+", type=int, default=DEFAULT_SATS,
                    help="NORAD satellite IDs to track")
    ap.add_argument("--lat", type=float, default=OBS_LAT, help="Observer latitude")
    ap.add_argument("--lon", type=float, default=OBS_LON, help="Observer longitude")
    ap.add_argument("--alt", type=float, default=OBS_ALT, help="Observer altitude, km")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 60)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
