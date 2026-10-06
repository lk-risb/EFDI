#!/usr/bin/env python3
"""mapa_bridge.py — MAPA.UA air-threat map → Zenoh bridge (open data).

Polls https://mapa.ua/api/v1/current, the public, documented, key-less API behind mapa.ua, a
Ukrainian volunteer project. It aggregates Telegram/OVA/Air Force reports with AI processing;
it is OSINT, not radar and not an official warning. Every track credits MAPA.UA, as the
project asks.

Published as JSON tracks under <ORG>/air/mapa/hostile/<uav|missile|bomb>/..., one per active
object with its heading, speed, predicted position, origin zone and target city, plus a
trail line (<uid>-TRAIL, under air/mapa/trail/line) drawn by tak_layer. Objects that are no
longer active are retracted with a JSON tombstone. Only status "active" is published.

Env:
  MAPA_URL         endpoint (default https://mapa.ua/api/v1/current)
  MAPA_POLL_S      poll interval (default 20)
  MAPA_STALE_S     how long a marker lives between polls (default 120)
  MAPA_MAX_AGE_S   skip an active object whose last_seen is older than this (default 0 = never,
                   like mapa.ua itself, which keeps an object until it is no longer active)
  MAPA_STALE_REPORT_S  mark an object "(STALE)" once its last report is older than this (default 600)
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

from http_json import read_json_response
from namespace_prefix import topic_root
import zenoh
from protocols.vendors.random.gateway import open_session
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
URL = os.environ.get("MAPA_URL", "https://mapa.ua/api/v1/current")
POLL_S = max(5, int(os.environ.get("MAPA_POLL_S", "20")))
STALE_S = int(os.environ.get("MAPA_STALE_S", "120"))
MAX_AGE_S = int(os.environ.get("MAPA_MAX_AGE_S", "0"))
STALE_REPORT_S = int(os.environ.get("MAPA_STALE_REPORT_S", "600"))
TRAIL_MAX_POINTS = 12
TRAIL_PREFIX = "{}/air/mapa/trail/line".format(TOPIC_ROOT)
TRAIL_SUFFIX = "-TRAIL"
SCHEMA = "efdi:mapa_threat"
ATTRIBUTION = "Data: MAPA.UA https://mapa.ua/ - volunteer OSINT aggregator, not radar or an official warning"
_HEADERS = {"User-Agent": "EFDI-mapa-bridge (+open data, credit MAPA.UA)", "Accept": "application/json"}
# object kind prefix -> (entity slot, label)
_KINDS = (("drone", ("uav", "UAV")), ("missile", ("missile", "MISSILE")), ("bomb", ("bomb", "KAB")))


def _entity(kind: str):
    for prefix, value in _KINDS:
        if str(kind or "").startswith(prefix):
            return value
    return None


def _place(slug) -> str:
    return str(slug or "").replace("_", " ").title()


def object_tracks(data: dict, now: float, max_age_s: int = MAX_AGE_S) -> list:
    """(topic prefix, track) per active object, each followed by its trail line if it has one."""
    out = []
    for o in data.get("objects") or []:
        entity = _entity(o.get("kind"))
        if not entity or o.get("status") != "active" or not o.get("id"):
            continue
        try:
            lat, lon = round(float(o["lat"]), 5), round(float(o["lon"]), 5)
        except (TypeError, ValueError, KeyError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        seen = o.get("last_seen")
        age_s = now - seen if isinstance(seen, (int, float)) else None
        if max_age_s and age_s is not None and age_s > max_age_s:
            continue
        stale = age_s is not None and age_s > STALE_REPORT_S
        slot, label = entity
        amount = o.get("amount") or 1
        uid = "MAPA-{}".format(o["id"])
        target, origin = _place(o.get("to_city")), _place(o.get("from_zone"))
        remarks = [ATTRIBUTION, "{} {}".format(o.get("subkind") or o.get("kind"), "x{}".format(amount) if amount > 1 else "").strip()]
        if origin or target:
            remarks.append("from {} to {}".format(origin or "?", target or "?"))
        if o.get("speed_kmh"):
            remarks.append("speed {:g} km/h".format(o["speed_kmh"]))
        if age_s is not None:
            remarks.append("last report {} s ago{}".format(int(age_s), " - STALE" if stale else ""))
        track = {
            "_src": "mapa.ua", "_ts": now, "uid": uid, "type": o.get("kind"), "target_type": slot,
            "callsign": "MAPA {}{} {}{}".format(label, " x{}".format(amount) if amount > 1 else "", target,
                                                " (STALE)" if stale else "").strip(),
            "lat_deg": lat, "lon_deg": lon, "heading_deg": o.get("heading"), "speed_ms": (o["speed_kmh"] / 3.6) if o.get("speed_kmh") else None,
            "count": amount if amount > 1 else None, "origin": origin, "destination": target,
            "observed_ts": seen if isinstance(seen, (int, float)) else None,
            "predicted_lat_deg": o.get("predicted_lat"), "predicted_lon_deg": o.get("predicted_lon"),
            "stale_s": STALE_S, "remarks": " | ".join(remarks),
        }
        out.append(("{}/air/mapa/hostile/{}".format(TOPIC_ROOT, slot), {k: v for k, v in track.items() if v not in ("", None)}))
        trail = _trail(o, track, now)
        if trail:
            out.append((TRAIL_PREFIX, trail))
    return out


def _trail(o: dict, track: dict, now: float):
    """Polyline of the last reported positions ending at the current one (trail points are [lon, lat, t])."""
    points = []
    for p in (o.get("trail") or [])[-TRAIL_MAX_POINTS:]:
        try:
            point = [round(float(p[0]), 5), round(float(p[1]), 5)]
        except (TypeError, ValueError, IndexError):
            continue
        if not points or points[-1] != point:
            points.append(point)
    here = [track["lon_deg"], track["lat_deg"]]
    if not points or points[-1] != here:
        points.append(here)
    if len(points) < 2:
        return None
    return {
        "_src": "mapa.ua", "_ts": now, "uid": track["uid"] + TRAIL_SUFFIX, "type": "trail",
        "callsign": "MAPA {} trail".format(track["callsign"].split()[1] if len(track["callsign"].split()) > 1 else "threat"),
        "lat_deg": track["lat_deg"], "lon_deg": track["lon_deg"], "stale_s": STALE_S,
        "geometry": {"type": "LineString", "coordinates": points}, "shape_color": "orange", "shape_style": "trail",
        "remarks": "Path of the last {} reports (oldest to current) - {}".format(len(points), ATTRIBUTION),
    }


def fetch_current() -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers=_HEADERS), timeout=20) as resp:
            data = read_json_response(resp, max_bytes=8 * 1024 * 1024)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print("mapa fetch failed: {}".format(exc), flush=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("objects"), list):
        print("mapa: unexpected document", flush=True)
        return None
    return data


def _put(session, prefix: str, track: dict) -> None:
    session.put(add_version(semantic_topic(prefix, track)), json.dumps(track).encode(),
                encoding=zenoh.Encoding.APPLICATION_JSON.with_schema(SCHEMA))


def main():
    ap = argparse.ArgumentParser(description="MAPA.UA air-threat map → Zenoh bridge (open data)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("mapa Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    print("mapa bridge starting (poll {}s, stale {}s)".format(POLL_S, STALE_S), flush=True)
    published: dict = {}                    # uid -> (prefix, track) on the fabric now
    try:
        while True:
            data = fetch_current()
            if data is not None:
                now = time.time()
                current = {t["uid"]: (p, t) for p, t in object_tracks(data, now)}
                for prefix, track in current.values():
                    _put(session, prefix, track)
                for uid in set(published) - set(current):
                    prefix, track = published[uid]
                    session.put(add_version(semantic_topic(prefix, track)),
                                json.dumps({"uid": uid, "_delete": True, "_ts": now, "_src": "mapa.ua"}).encode())
                published = current
                print("mapa: {} tracks".format(len(published)), flush=True)
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
