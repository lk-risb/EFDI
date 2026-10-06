#!/usr/bin/env python3
"""deepstate_bridge.py — DeepStateMap frontline → Zenoh bridge (open data).

Polls https://deepstatemap.live/api/history/last, the public JSON behind
deepstatemap.live, a Ukrainian volunteer project that maps the frontline from open
sources. It is a CONTEXT layer, not an alert source: the map is updated about once a day,
and the endpoint is undocumented and unofficial. Every track carries "DeepState" as its
credit and the map's own update time. Check DeepState's terms before publishing this to
partners.

What is published (JSON tracks, under <ORG>/land/deepstate/frontline/hostile/unit/...):
  attack-direction arrows and named Russian/Belarusian units (regiments, brigades,
  divisions, armies...), drawn by tak_layer / sitaware_layer as hostile ground units.
DeepState's occupied / liberated territory polygons are NOT published: they are not
region-based and clash with the regional air-alert zones from dangausakis and NEPTUN.

The map is polled every DEEPSTATE_POLL_S, but the tracks are re-published from memory
every DEEPSTATE_REPUBLISH_S (240 s) because the layers expire a track after its stale
time (600 s here, asked for through the track's stale_s field; TAK stores every event, so
re-sending less often keeps its database smaller). Features that leave the map are retracted with a JSON tombstone, and nothing is
published once the map is older than DEEPSTATE_MAX_AGE_S.

Env:
  DEEPSTATE_URL            map endpoint (default the public one above)
  DEEPSTATE_POLL_S         how often to fetch the map (default 3600)
  DEEPSTATE_REPUBLISH_S    how often to re-publish the cached tracks (default 240; they live 600 s)
  DEEPSTATE_MAX_AGE_S      publish nothing once the map is older than this (default 259200)
  DEEPSTATE_MARKERS        set 0 to publish nothing but a log line (default on)
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
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
URL = os.environ.get("DEEPSTATE_URL", "https://deepstatemap.live/api/history/last")
POLL_S = int(os.environ.get("DEEPSTATE_POLL_S", "3600"))
REPUBLISH_S = int(os.environ.get("DEEPSTATE_REPUBLISH_S", "240"))
STALE_S = 600               # markers live this long between re-publishes (the map changes about daily)
MAX_AGE_S = int(os.environ.get("DEEPSTATE_MAX_AGE_S", "259200"))
MARKERS_ENABLED = os.environ.get("DEEPSTATE_MARKERS", "1") != "0"
UNIT_PREFIX = "{}/land/deepstate/frontline/hostile/unit".format(TOPIC_ROOT)
SCHEMA = "efdi:deepstate_feature"
_HEADERS = {"User-Agent": "EFDI-deepstate-bridge (+open data, credit DeepStateMap)"}

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
    """uid -> (topic prefix, track) from a DeepState map document ({"id", "map"}): the
    attack-direction arrows and the named units. `id` is the map's update time as epoch
    seconds. Territory polygons are deliberately skipped."""
    if not markers:
        return {}
    updated = _epoch(data.get("id"))
    stamp = datetime.fromtimestamp(updated, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if updated else "unknown"
    credit = "Source: DeepStateMap (deepstatemap.live), map updated {} - context only, not live".format(stamp)
    out = {}
    for index, feature in enumerate((data.get("map") or {}).get("features") or []):
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not coords or len(coords) < 2:
            continue
        key, english = _key_and_english((feature.get("properties") or {}).get("name"))
        if key.startswith("geoJSON.status.attack_direction"):
            name, kind, uid = "Direction of attack", "attack_direction", "DS-ARROW-{}".format(index)
        elif key.startswith("geoJSON.units."):
            name, kind, uid = english or "Unit", "unit", "DS-UNIT-{}".format(index)
        else:
            continue
        try:
            lat, lon = round(float(coords[1]), 6), round(float(coords[0]), 6)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        out[uid] = (UNIT_PREFIX, {
            "_src": "deepstatemap.live", "_ts": now, "uid": uid, "type": kind, "callsign": name,
            "lat_deg": lat, "lon_deg": lon, "target_type": "unit", "position_uncertainty_m": 5000, "stale_s": STALE_S,
            "remarks": "{} - {}".format(name, credit),
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
                        print("deepstate: {} markers".format(len(cached)), flush=True)
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
