#!/usr/bin/env python3
"""lt_surveillance_bridge.py — Lithuania surveillance camera locations → Zenoh bridge.

Queries the Overpass API for OpenStreetMap-tagged surveillance cameras
(`man_made=surveillance`) within a bounding box (default: Lithuania) and
publishes each as a GeoFeature JSON. Same live-query approach as
osm_overpass_bridge.py — no manual export needed.

Originally scoped to DeFlock's ALPR-specific tag (`surveillance:type=ALPR`),
but that subtype is heavily US-centric — a Lithuania-sized bbox returns only
a handful of hits, mostly Belarus-adjacent, not real coverage. Dropped that
filter down to the general `man_made=surveillance` tag, which OSM contributors
use for CCTV, traffic, and ALPR cameras alike — confirmed 1,629 hits in
Lithuania's bbox on a live Overpass query (vs. 6 for ALPR-only).

OSM tagging convention (confirmed via a live Overpass lookup against a known
DeFlock-cataloged camera, OSM node 31431226 — still useful as a schema
reference even though the ALPR-only filter itself is dropped):
  man_made=surveillance          <- filter tag
  surveillance:type=<type>       e.g. "ALPR", "camera", "webcam" — informational
  manufacturer=<brand>           e.g. "Flock Safety"
  operator=<agency>              e.g. "Amarillo Police Department"
  direction=<degrees>            camera heading, string, not always numeric
  surveillance:zone=<zone>       e.g. "traffic", "parking"
  camera:mount=<mount>           e.g. "pole", "wall"

A rectangular bbox around a country inevitably catches some border-area hits
just across the line (Belarus/Latvia/Poland near Lithuania's borders) — same
limitation osm_overpass_bridge.py already accepts with its much larger Baltic
region bbox. Not filtered further; not worth an area/polygon Overpass query
for this.

Re-queries every POLL_INTERVAL seconds — OSM data is stable, so 12 h is fine.

Zenoh topic:  <ORG>/land/lt-surveillance/overpass/neutral/geo/features/v1
CoT type:     a-n-G-E-S  (neutral ground sensor — same icon as acoustic sensors)

Run:
    venv/bin/python3 lt_surveillance_bridge.py
    venv/bin/python3 lt_surveillance_bridge.py --bbox 53.85 20.9 56.45 26.85
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

RETRY_DELAYS = (30, 60, 120)   # retry 429/504/timeout up to 3 times

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = "LTU/CISB/" + ORG   # organization prefix precedes the pod namespace
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",  # fallback mirror — main instance rate-limits hard
)
POLL_INTERVAL = 43200  # 12 h — OSM features don't change often

# Operational bbox S, W, N, E — Lithuania (rectangular, slight border bleed expected)
BBOX = (53.85, 20.9, 56.45, 26.85)

SURVEILLANCE_TAG = ("man_made", "surveillance")


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


def overpass_query(tag_key: str, tag_value: str, bbox: tuple) -> list:
    """Return OSM nodes matching tag within bbox; retries on 429/504/timeout,
    falling back to a mirror instance since the main overpass-api.de server
    rate-limits aggressively on repeated queries."""
    s, w, n, e = bbox
    q = '[out:json][timeout:60];node["{}"="{}"]({},{},{},{});out;'.format(
        tag_key, tag_value, s, w, n, e)
    data = q.encode()
    headers = {"User-Agent": "efdi-lt-surveillance-bridge/1.0", "Content-Type": "text/plain"}

    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        url = OVERPASS_URLS[(attempt - 1) % len(OVERPASS_URLS)]
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=65) as resp:
                return json.loads(resp.read().decode()).get("elements", [])
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 504) and delay is not None:
                print("Overpass {} on {} — retry in {}s (attempt {})".format(
                    exc.code, url, delay, attempt), flush=True)
                time.sleep(delay)
            else:
                print("Overpass HTTP error ({}):".format(url), exc, flush=True)
                return []
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            print("Overpass error ({}):".format(url), exc, flush=True)
            return []
    return []


def normalize(elem: dict) -> dict | None:
    tags = elem.get("tags", {})
    lat, lon = elem.get("lat"), elem.get("lon")
    if lat is None or lon is None:
        return None
    country_code = (
        tags.get("addr:country") or
        tags.get("is_in:country_code") or
        tags.get("country_code") or
        ""
    ).strip().upper()[:2]

    surv_type = tags.get("surveillance:type", "")
    feature_type = "alpr_camera" if surv_type.upper() == "ALPR" else "cctv_camera"

    point = {
        "_ts":               time.time(),
        "_src":              "osm-surveillance",
        "osm_id":            elem.get("id"),
        "osm_type":          elem.get("type", "node"),
        "lat_deg":           lat,
        "lon_deg":           lon,
        "feature_type":      feature_type,
        "surveillance_type": surv_type,
        "name":              tags.get("name", ""),
        "brand":             tags.get("manufacturer", ""),
        "operator":          tags.get("operator", ""),
        "heading_raw":       tags.get("direction", ""),
        "zone":              tags.get("surveillance:zone", ""),
        "mount":             tags.get("camera:mount", ""),
        "country_code":      country_code,
        "tags_json":         json.dumps(tags, ensure_ascii=False),
    }
    try:
        point["heading_deg"] = float(tags.get("direction", ""))
    except ValueError:
        pass
    return point


def run(args):
    session = zenoh.open(make_config())
    pub = session.declare_publisher(
        "{}/land/lt-surveillance/overpass/neutral/geo/features/v1".format(TOPIC_ROOT))

    try:
        while True:
            print("Querying Lithuania surveillance cameras…", flush=True)
            elements = overpass_query(*SURVEILLANCE_TAG, args.bbox)
            count = 0
            for elem in elements:
                point = normalize(elem)
                if point is None:
                    continue
                pub.put(json.dumps(point).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                count += 1
            print("PUB lt-surveillance: {} cameras".format(count), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Lithuania surveillance camera → Zenoh bridge")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Re-query interval in seconds (default: 43200 = 12 h)")
    ap.add_argument("--bbox", type=float, nargs=4, default=list(BBOX),
                    metavar=("S", "W", "N", "E"),
                    help="Bounding box south west north east (default: Lithuania)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
