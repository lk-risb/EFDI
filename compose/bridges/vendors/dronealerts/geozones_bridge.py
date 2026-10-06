#!/usr/bin/env python3
"""geozones_bridge.py — national UAS airspace-restriction zones → Zenoh bridge (open data).

Reads https://dronealerts.online/api/drone-geozones, which republishes the official zone sets
of Estonia (EANS) and Poland (PANSA DroneMap) as GeoJSON polygons, and publishes each polygon
as a standing neutral zone under <ORG>/land/geozones/airspace/neutral/zone/... . These are
fixed civil-drone restrictions (airports, military and government sites), NOT live air-raid
alerts. dronealerts.online is a third-party aggregator; each track names the original
authority and the aggregator. Check the reuse terms of dronealerts.online and of each
authority before sharing this with partners.

TAK stores every event, so the zones are sent once, live for an hour and are re-sent only
when they change or every GEOZONES_REFRESH_S (default 3000 s). Zones that disappear from the
source are retracted with a JSON tombstone.

Env:
  GEOZONES_URL        endpoint (default https://dronealerts.online/api/drone-geozones)
  GEOZONES_COUNTRIES  comma list of country codes to publish (default EE,PL)
  GEOZONES_POLL_S     how often to fetch the zones (default 21600)
  GEOZONES_REFRESH_S  re-send an unchanged zone this often (default 3000; zones live 3600 s)
  GEOZONES_MAX_POINTS simplify each polygon to at most this many points (default 60)
"""

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from http_json import read_json_response
from namespace_prefix import topic_root
import zenoh
from protocols.vendors.random.gateway import open_session
from protocols.vendors.random.geo_simplify import simplify_ring
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
URL = os.environ.get("GEOZONES_URL", "https://dronealerts.online/api/drone-geozones")
COUNTRIES = {c.strip().upper() for c in os.environ.get("GEOZONES_COUNTRIES", "EE,PL").split(",") if c.strip()}
POLL_S = int(os.environ.get("GEOZONES_POLL_S", "21600"))
REFRESH_S = int(os.environ.get("GEOZONES_REFRESH_S", "3000"))
MAX_POINTS = int(os.environ.get("GEOZONES_MAX_POINTS", "60"))
STALE_S = 3600
PREFIX = "{}/land/geozones/airspace/neutral/zone".format(TOPIC_ROOT)
SCHEMA = "efdi:geozone"
_HEADERS = {"User-Agent": "EFDI-geozones-bridge (+open data)", "Accept": "application/json"}
_AUTHORITY = {"EE": "EANS Estonia UAS zones", "PL": "PANSA DroneMap zones"}


def zone_tracks(data: dict, now: float, countries=None, max_points: int = MAX_POINTS) -> dict:
    """uid -> track for each polygon of a wanted country."""
    countries = COUNTRIES if countries is None else countries
    out = {}
    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        country = str(props.get("country") or "").upper()
        geometry = feature.get("geometry") or {}
        if country not in countries or geometry.get("type") != "Polygon":
            continue
        try:
            ring = [[round(float(p[0]), 5), round(float(p[1]), 5)] for p in geometry["coordinates"][0]]
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if len(ring) < 4 or not all(-90 <= p[1] <= 90 and -180 <= p[0] <= 180 for p in ring):
            continue
        ring = simplify_ring(ring, max_points)
        title = str(props.get("title") or "zone").strip()
        # The title alone is not unique (and polygons share titles), so the geometry names the zone.
        uid = "GEOZONE-{}-{}-{}".format(country, "".join(c for c in title.upper() if c.isalnum())[:16] or "ZONE",
                                       hashlib.sha1(json.dumps(ring).encode()).hexdigest()[:8].upper())
        out[uid] = {
            "_src": "dronealerts.online", "_ts": now, "uid": uid, "type": "uas_geozone",
            "callsign": "{} UAS zone {}".format(country, title),
            "lat_deg": round(sum(p[1] for p in ring[:-1]) / (len(ring) - 1), 5),
            "lon_deg": round(sum(p[0] for p in ring[:-1]) / (len(ring) - 1), 5),
            "geometry": {"type": "Polygon", "coordinates": [ring]}, "stale_s": STALE_S,
            "shape_color": "blue", "shape_style": "geozone",
            "remarks": "Civil UAS airspace restriction ({}), standing zone, not an air-raid alert. "
                       "Source: {} via dronealerts.online".format(props.get("zoneType") or "geozone",
                                                                  _AUTHORITY.get(country, props.get("source") or country)),
        }
    return out


def fetch_zones() -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers=_HEADERS), timeout=90) as resp:
            data = read_json_response(resp, max_bytes=32 * 1024 * 1024)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print("geozones fetch failed: {}".format(exc), flush=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        print("geozones: unexpected document", flush=True)
        return None
    return data


def main():
    ap = argparse.ArgumentParser(description="UAS geozones → Zenoh bridge (open data)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.parse_args()
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("geozones Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    print("geozones bridge starting (countries {}, poll {}s, refresh {}s)".format(
        ",".join(sorted(COUNTRIES)), POLL_S, REFRESH_S), flush=True)
    zones: dict = {}
    sent: dict = {}                    # uid -> (digest, time sent)
    next_poll = 0.0
    try:
        while True:
            now = time.time()
            if now >= next_poll:
                data = fetch_zones()
                next_poll = now + (POLL_S if data else 600)
                if data is not None:
                    fresh = zone_tracks(data, now)
                    for uid in set(zones) - set(fresh):
                        session.put(add_version(semantic_topic(PREFIX, zones[uid])),
                                    json.dumps({"uid": uid, "_delete": True, "_ts": now, "_src": "dronealerts.online"}).encode())
                        sent.pop(uid, None)
                    zones = fresh
                    print("geozones: {} zones".format(len(zones)), flush=True)
            for uid, track in zones.items():
                digest = json.dumps({k: v for k, v in track.items() if k != "_ts"}, sort_keys=True)
                if sent.get(uid) and sent[uid][0] == digest and now - sent[uid][1] < REFRESH_S:
                    continue
                sent[uid] = (digest, now)
                track["_ts"] = now
                session.put(add_version(semantic_topic(PREFIX, track)), json.dumps(track).encode(),
                            encoding=zenoh.Encoding.APPLICATION_JSON.with_schema(SCHEMA))
                time.sleep(0.02)       # spread a large first send
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
