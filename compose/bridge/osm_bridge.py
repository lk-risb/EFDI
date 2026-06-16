#!/usr/bin/env python3
"""osm_bridge.py — OpenStreetMap Overpass API → Zenoh bridge.

Queries the Overpass API for geographic features (aerodromes, seaports, military
areas, railway stations) within the Baltic bounding box and publishes each as a
GeoFeature JSON. No API key required.

Re-queries every POLL_INTERVAL seconds — OSM data is stable, so 12 h is fine.

Zenoh topic:  <ORG>/geo/osm/<feature-type>/v1
Proto schema: geo_feature.proto  (message GeoFeature, package ltu.cis.tracks.v1)

Run:
    venv/bin/python3 osm_bridge.py
    venv/bin/python3 osm_bridge.py --interval 43200
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

OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
POLL_INTERVAL = 43200  # 12 h — OSM features don't change often

# Operational bbox S, W, N, E — Baltic + Scandinavia + E.Europe + Middle East
BBOX = (20.0, 4.0, 73.0, 65.0)

# Features to query: (feature_type, OSM tag key, OSM tag value)
FEATURE_QUERIES = [
    ("aerodrome", "aeroway",  "aerodrome"),
    ("port",      "harbour",  "yes"),
    ("military",  "landuse",  "military"),
    ("station",   "railway",  "station"),
]


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


def overpass_query(tag_key: str, tag_value: str, bbox: tuple) -> list:
    """Return list of OSM elements matching tag within bbox."""
    s, w, n, e = bbox
    q = '[out:json][timeout:30];(node["{}"="{}"]({},{},{},{});way["{}"="{}"]({},{},{},{}););out center;'.format(
        tag_key, tag_value, s, w, n, e,
        tag_key, tag_value, s, w, n, e,
    )
    req = urllib.request.Request(
        OVERPASS_URL,
        data=q.encode(),
        headers={"User-Agent": "efdi-osm-bridge/1.0", "Content-Type": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode()).get("elements", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("Overpass error ({}/{}): {}".format(tag_key, tag_value, exc), flush=True)
        return []


def normalize(elem: dict, feature_type: str) -> dict | None:
    tags = elem.get("tags", {})
    # Node → direct lat/lon; way → center
    if elem["type"] == "node":
        lat, lon = elem.get("lat"), elem.get("lon")
    else:
        center = elem.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None
    return {
        "_ts":          time.time(),
        "_src":         "osm",
        "osm_id":       elem.get("id"),
        "osm_type":     elem.get("type"),
        "lat_deg":      lat,
        "lon_deg":      lon,
        "feature_type": feature_type,
        "name":         tags.get("name", tags.get("name:en", "")),
        "iata":         tags.get("iata", ""),
        "icao":         tags.get("icao", ""),
        "tags_json":    json.dumps(tags, ensure_ascii=False),
    }


def run(args):
    session = zenoh.open(make_config())
    pub = session.declare_publisher("{}/land/geo/v1".format(ORG))

    try:
        while True:
            for feature_type, tag_key, tag_value in FEATURE_QUERIES:
                print("Querying OSM {} ({}/{})…".format(
                    feature_type, tag_key, tag_value), flush=True)
                elements = overpass_query(tag_key, tag_value, args.bbox)
                count = 0
                for elem in elements:
                    point = normalize(elem, feature_type)
                    if point is None:
                        continue
                    pub.put(json.dumps(point).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                    count += 1
                print("PUB osm/{}: {} features".format(feature_type, count), flush=True)
                time.sleep(5)  # polite gap between Overpass queries
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="OSM Overpass → Zenoh bridge")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Re-query interval in seconds (default: 43200 = 12 h)")
    ap.add_argument("--bbox", type=float, nargs=4, default=list(BBOX),
                    metavar=("S", "W", "N", "E"),
                    help="Bounding box south west north east (default: Baltic region)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
