#!/usr/bin/env python3
"""deepstate_bridge.py — DeepStateMap frontline → Zenoh bridge (open data).

Polls https://deepstatemap.live/api/history/last, the public JSON behind
deepstatemap.live, a Ukrainian volunteer project that maps the frontline from open
sources. It is a CONTEXT layer, not an alert source: the map is updated about once a day,
and the endpoint is undocumented and unofficial. Every track carries "DeepState" as its
credit and the map's own update time. Check DeepState's terms before publishing this to
partners.

What is published (JSON tracks, under <ORG>/land/deepstate/...):
  frontline/neutral/zone/...   faint area fills (shape_style "wash"): occupied (red),
                               liberated (green) and status-unknown (grey) territory
  frontline/hostile/unit/...   attack-direction arrows and named Russian/Belarusian units
                               (tak_layer / sitaware_layer draw these as hostile ground units)

The map is polled every DEEPSTATE_POLL_S, but the tracks are re-published from memory
every DEEPSTATE_REPUBLISH_S because the layers expire a track two minutes after its
_ts. Features that leave the map are retracted with a JSON tombstone, and nothing is
published once the map is older than DEEPSTATE_MAX_AGE_S.

Env:
  DEEPSTATE_URL            map endpoint (default the public one above)
  DEEPSTATE_POLL_S         how often to fetch the map (default 3600)
  DEEPSTATE_REPUBLISH_S    how often to re-publish the cached tracks (default 60)
  DEEPSTATE_MAX_AGE_S      publish nothing once the map is older than this (default 259200)
  DEEPSTATE_MARKERS        set 0 to publish only the territory fills, no unit/attack markers
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from http_json import read_json_response
from namespace_prefix import topic_root
import zenoh
from protocols.vendors.random.gateway import open_session
from protocols.vendors.random.geo_simplify import ring_area, simplify_ring
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
URL = os.environ.get("DEEPSTATE_URL", "https://deepstatemap.live/api/history/last")
POLL_S = int(os.environ.get("DEEPSTATE_POLL_S", "3600"))
REPUBLISH_S = int(os.environ.get("DEEPSTATE_REPUBLISH_S", "60"))
MAX_AGE_S = int(os.environ.get("DEEPSTATE_MAX_AGE_S", "259200"))
MARKERS_ENABLED = os.environ.get("DEEPSTATE_MARKERS", "1") != "0"
ZONE_PREFIX = "{}/land/deepstate/frontline/neutral/zone".format(TOPIC_ROOT)
UNIT_PREFIX = "{}/land/deepstate/frontline/hostile/unit".format(TOPIC_ROOT)
SCHEMA = "efdi:deepstate_feature"
MAX_VERTICES = 120           # the layers draw at most 256 points per ring
MIN_AREA_DEG2 = 0.005        # smaller polygons are noise at map scale (about 50 km2)
MAX_ZONES = 80               # the largest ones, so one republish stays a bounded burst
_HEADERS = {"User-Agent": "EFDI-deepstate-bridge (+open data, credit DeepStateMap)"}

# The last part of a feature's "UA /// EN /// key" name says what it is.
_STATUS_COLORS = (("status.occupied", "red", "Occupied"), ("status.dismissed", "green", "Liberated"),
                  ("status.unknown", "grey", "Status unknown"))


def _key_and_english(name: str):
    """(key, English text) from a DeepState "UA /// EN /// key" name."""
    parts = [p.strip() for p in str(name or "").split("///")]
    key = parts[-1] if len(parts) >= 3 else ""
    english = parts[1] if len(parts) >= 3 else (parts[0] if parts else "")
    return key, english


def _epoch(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_tracks(data: dict, now: float, markers: bool = True) -> dict:
    """uid -> (topic prefix, track) from a DeepState map document ({"id", "map"}).
    `id` is the map's update time as epoch seconds."""
    updated = _epoch(data.get("id"))
    stamp = datetime.fromtimestamp(updated, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if updated else "unknown"
    credit = "Source: DeepStateMap (deepstatemap.live), map updated {} - context only, not live".format(stamp)
    zones, units = [], []
    for index, feature in enumerate((data.get("map") or {}).get("features") or []):
        geometry = feature.get("geometry") or {}
        key, english = _key_and_english((feature.get("properties") or {}).get("name"))
        coords = geometry.get("coordinates")
        if geometry.get("type") == "Polygon" and coords:
            for suffix, color, label in _STATUS_COLORS:
                if key.startswith("geoJSON." + suffix):
                    ring = coords[0]
                    if ring and ring_area(ring) >= MIN_AREA_DEG2:
                        zones.append((ring_area(ring), index, ring, color, english or label, label))
                    break
        elif markers and geometry.get("type") == "Point" and coords and len(coords) >= 2:
            if key.startswith("geoJSON.status.attack_direction"):
                units.append((index, coords, "Direction of attack", "attack_direction"))
            elif key.startswith("geoJSON.units."):
                units.append((index, coords, english or "Unit", "unit"))
    out = {}
    for _, index, ring, color, name, label in sorted(zones, reverse=True)[:MAX_ZONES]:
        ring = simplify_ring([[round(p[0], 5), round(p[1], 5)] for p in ring], MAX_VERTICES)
        uid = "DS-ZONE-{}".format(index)
        out[uid] = (ZONE_PREFIX, {
            "_src": "deepstatemap.live", "_ts": now, "uid": uid, "type": "frontline_zone",
            "callsign": "DS {}".format(label), "shape_color": color, "shape_style": "wash",
            "lat_deg": round(sum(p[1] for p in ring[:-1]) / (len(ring) - 1), 5),
            "lon_deg": round(sum(p[0] for p in ring[:-1]) / (len(ring) - 1), 5),
            "geometry": {"type": "Polygon", "coordinates": [ring]}, "status": label,
            "alert_id": "DEEPSTATE", "country": "UA", "level": color,
            "remarks": "{} - {}".format(label if name == label else "{}: {}".format(label, name), credit),
        })
    for index, coords, name, kind in units:
        uid = "DS-{}-{}".format("ARROW" if kind == "attack_direction" else "UNIT", index)
        out[uid] = (UNIT_PREFIX, {
            "_src": "deepstatemap.live", "_ts": now, "uid": uid, "type": kind, "callsign": name,
            "lat_deg": round(float(coords[1]), 6), "lon_deg": round(float(coords[0]), 6),
            "target_type": "unit", "position_uncertainty_m": 5000, "remarks": "{} - {}".format(name, credit),
        })
    return out


def fetch_map() -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers=_HEADERS), timeout=60) as resp:
            data = read_json_response(resp, max_bytes=8 * 1024 * 1024)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print("deepstate fetch failed: {}".format(exc), flush=True)
        return None
    if not isinstance(data, dict) or not isinstance((data.get("map") or {}).get("features"), list):
        print("deepstate: unexpected map document", flush=True)
        return None
    return data


def _put(session, prefix: str, track: dict) -> None:
    session.put(add_version(semantic_topic(prefix, track)), json.dumps(track).encode(),
                encoding=zenoh.Encoding.APPLICATION_JSON.with_schema(SCHEMA))


def main():
    ap = argparse.ArgumentParser(description="DeepStateMap frontline → Zenoh bridge (open data)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("deepstate Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    print("deepstate bridge starting (poll {}s, republish {}s, max age {}s, markers {})".format(
        POLL_S, REPUBLISH_S, MAX_AGE_S, "on" if MARKERS_ENABLED else "off"), flush=True)

    cached: dict = {}          # uid -> (prefix, track), as last built
    published: dict = {}       # what is on the fabric now
    next_poll = 0.0
    try:
        while True:
            now = time.time()
            if now >= next_poll:
                data = fetch_map()
                next_poll = now + (POLL_S if data else min(POLL_S, 300))
                if data is not None:
                    updated = _epoch(data.get("id"))
                    if updated is not None and now - updated > MAX_AGE_S:
                        print("deepstate: map is {:.0f} h old, publishing nothing".format((now - updated) / 3600),
                              flush=True)
                        cached = {}
                    else:
                        cached = map_tracks(data, now, MARKERS_ENABLED)
                        print("deepstate: {} features ({} zones, {} markers)".format(
                            len(cached), sum(1 for u in cached if u.startswith("DS-ZONE")),
                            sum(1 for u in cached if not u.startswith("DS-ZONE"))), flush=True)
            for uid, (prefix, track) in cached.items():
                track["_ts"] = now                       # the layers expire a track 2 minutes after this
                _put(session, prefix, track)
            for uid in set(published) - set(cached):
                prefix, track = published[uid]
                session.put(add_version(semantic_topic(prefix, track)),
                            json.dumps({"uid": uid, "_delete": True, "_ts": now,
                                        "_src": "deepstatemap.live"}).encode())
            published = dict(cached)
            if args.verbose:
                print("DEEPSTATE published {}".format(len(published)), flush=True)
            time.sleep(REPUBLISH_S)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
