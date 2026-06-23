#!/usr/bin/env python3
"""here_traffic_bridge.py — HERE Traffic Flow API → Zenoh bridge.

Polls the HERE Traffic Flow v7 API for real-time road segment speeds and
congestion within the Baltic region and publishes each flow segment as JSON.

Free API key: https://developer.here.com/  (250 000 transactions/month free)

Zenoh topic:  <ORG>/land/traffic/v1
Proto schema: here_traffic.proto  (message HereTrafficFlow)

Run:
    HERE_KEY=<key> venv/bin/python3 here_traffic_bridge.py
    HERE_KEY=<key> venv/bin/python3 here_traffic_bridge.py --interval 300
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
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

HERE_FLOW_URL = "https://data.traffic.hereapi.com/v7/flow"
POLL_INTERVAL = 300  # 5 min — traffic updates ~5 min on HERE free tier

# Operational bbox: west,south,east,north — Baltic + Scandinavia + E.Europe + Middle East
DEFAULT_BBOX = "14.0,41.0,45.0,62.0"


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


def fetch_flow(api_key: str, bbox: str) -> list:
    url = "{}?in=bbox:{}&apiKey={}".format(HERE_FLOW_URL, bbox, api_key)
    req = urllib.request.Request(url, headers={
        "User-Agent": "efdi-here-traffic-bridge/1.0",
        "Accept":     "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return data.get("results", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("HERE Traffic fetch error:", exc, flush=True)
        return []


def normalize(result: dict) -> dict | None:
    flow = result.get("currentFlow", {})
    speed     = flow.get("speed")
    free_flow = flow.get("freeFlow")
    jam       = flow.get("jamFactor")
    if speed is None and jam is None:
        return None

    # Use first matched point as representative lat/lon
    lat, lon = None, None
    for link in result.get("matchedLinks", []):
        pts = link.get("matchedPoints", [])
        if pts:
            lat = pts[0].get("lat")
            lon = pts[0].get("lng")
            break

    if lat is None or lon is None:
        return None

    point = {
        "_ts":                  time.time(),
        "_src":                 "here-traffic",
        "lat_deg":              lat,
        "lon_deg":              lon,
        "speed_kmh":            round(speed, 1) if speed is not None else None,
        "free_flow_speed_kmh":  round(free_flow, 1) if free_flow is not None else None,
        "jam_factor":           round(jam, 2) if jam is not None else None,
        "confidence":           flow.get("confidence"),
        "traversability":       flow.get("traversability", "open"),
    }
    return {k: v for k, v in point.items() if v is not None}


def run(args):
    if not args.key:
        raise SystemExit("HERE_KEY not set — get a free key at https://developer.here.com/")

    session = zenoh.open(make_config())
    pub = session.declare_publisher("{}/land/here/rest/civ/vehicle/tracks/v1".format(ORG))
    print("HERE Traffic bbox:", args.bbox, "  interval:", args.interval, "s", flush=True)

    try:
        while True:
            results = fetch_flow(args.key, args.bbox)
            count = 0
            for result in results:
                point = normalize(result)
                if point is None:
                    continue
                pub.put(json.dumps(point).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                count += 1
            print("HERE Traffic: {} segments published".format(count), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="HERE Traffic Flow → Zenoh bridge")
    ap.add_argument("--key", default=os.environ.get("HERE_KEY", ""),
                    help="HERE API key (or set HERE_KEY env var)")
    ap.add_argument("--bbox", default=DEFAULT_BBOX,
                    help="west,south,east,north (default: Baltic region)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 300)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
