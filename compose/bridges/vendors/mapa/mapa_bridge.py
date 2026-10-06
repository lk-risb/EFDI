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

mapa.ua splits a group into one object per drone ("308825_u0".."308825_u3", a metre or so
apart); they are published as one marker with a count, at the group's centre. A group that is the
same threat as one NEPTUN reports (see merge_with_neptun) is not drawn a second time: NEPTUN's
marker stays (its report is fresher and declares an error radius) and mapa's trail line stays,
tagged with the NEPTUN threat it matches. Anything ambiguous is left as two markers.

Env:
  MAPA_URL         endpoint (default https://mapa.ua/api/v1/current)
  MAPA_POLL_S      poll interval (default 20)
  MAPA_STALE_S     how long a marker lives between polls (default 120)
  MAPA_MAX_AGE_S   skip an active object whose last_seen is older than this (default 1800; 0 = never,
                   like mapa.ua itself, which keeps an object "active" for hours after its last report)
  MAPA_STALE_REPORT_S  mark an object "(STALE)" once its last report is older than this (default 600)
  MAPA_NEPTUN_MERGE    0 turns the NEPTUN merge off (default on)
  MAPA_NEPTUN_MIN_KM / MAPA_NEPTUN_MAX_KM  clamp on the NEPTUN error radius used as match distance (default 3 / 25)
  MAPA_NEPTUN_MAX_AGE_S  ignore a NEPTUN report not seen on the fabric for this long (default 180)
"""

import argparse
import json
import math
import os
import threading
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
MAX_AGE_S = int(os.environ.get("MAPA_MAX_AGE_S", "1800"))
STALE_REPORT_S = int(os.environ.get("MAPA_STALE_REPORT_S", "600"))
NEPTUN_MERGE = os.environ.get("MAPA_NEPTUN_MERGE", "1") != "0"
NEPTUN_MIN_KM = float(os.environ.get("MAPA_NEPTUN_MIN_KM", "3"))
NEPTUN_MAX_KM = float(os.environ.get("MAPA_NEPTUN_MAX_KM", "25"))
NEPTUN_MAX_AGE_S = float(os.environ.get("MAPA_NEPTUN_MAX_AGE_S", "180"))
NEPTUN_TOPIC = "{}/air/neptun/hostile/**".format(TOPIC_ROOT)
HEADING_TOL_DEG = 60
# NEPTUN threat type -> mapa entity slot. FPV and other types have no mapa counterpart and never merge.
_NEPTUN_SLOT = {"uav": "uav", "recon": "uav", "missile": "missile", "ballistic": "missile", "kab": "bomb"}
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


def _units(data: dict, now: float, max_age_s: int) -> dict:
    """base id -> [(object, entity)] of the active, valid, recent objects; "123_u0", "123_u1" share base "123"."""
    groups: dict = {}
    for o in data.get("objects") or []:
        entity = _entity(o.get("kind"))
        if not entity or o.get("status") != "active" or not o.get("id"):
            continue
        try:
            lat, lon = float(o["lat"]), float(o["lon"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        seen = o.get("last_seen")
        if max_age_s and isinstance(seen, (int, float)) and now - seen > max_age_s:
            continue
        groups.setdefault(str(o["id"]).split("_u")[0], []).append((o, entity))
    return groups


def object_tracks(data: dict, now: float, max_age_s: int = MAX_AGE_S) -> list:
    """(topic prefix, track) per active object group, each followed by its trail line if it has one."""
    out = []
    for base, members in _units(data, now, max_age_s).items():
        o, entity = members[0]
        lat = round(sum(float(m["lat"]) for m, _ in members) / len(members), 5)
        lon = round(sum(float(m["lon"]) for m, _ in members) / len(members), 5)
        seen_all = [m["last_seen"] for m, _ in members if isinstance(m.get("last_seen"), (int, float))]
        seen = max(seen_all) if seen_all else None
        age_s = now - seen if seen is not None else None
        stale = age_s is not None and age_s > STALE_REPORT_S
        slot, label = entity
        amount = sum(int(m.get("amount") or 1) for m, _ in members)
        uid = "MAPA-{}".format(base)
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
            "observed_ts": seen,
            "predicted_lat_deg": o.get("predicted_lat"), "predicted_lon_deg": o.get("predicted_lon"),
            "stale_s": STALE_S, "remarks": " | ".join(remarks),
        }
        out.append(("{}/air/mapa/hostile/{}".format(TOPIC_ROOT, slot), {k: v for k, v in track.items() if v not in ("", None)}))
        trail = _trail(next((m for m, _ in members if m.get("trail")), o), out[-1][1], now)
        if trail:
            out.append((TRAIL_PREFIX, trail))
    return out


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def _heading_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def _same_threat(mapa: dict, neptun: dict) -> bool:
    """Could this mapa marker be that NEPTUN threat? Same class, inside NEPTUN's own error radius
    (clamped), headings compatible when both have one."""
    if _NEPTUN_SLOT.get(str(neptun.get("type") or "").lower()) != mapa.get("target_type"):
        return False
    try:
        radius = min(max((neptun.get("position_uncertainty_m") or 0) / 1000.0, NEPTUN_MIN_KM), NEPTUN_MAX_KM)
        if _km(mapa["lat_deg"], mapa["lon_deg"], neptun["lat_deg"], neptun["lon_deg"]) > radius:
            return False
    except (KeyError, TypeError):
        return False
    heading_a, heading_b = mapa.get("heading_deg"), neptun.get("heading_deg")
    return heading_a is None or heading_b is None or _heading_diff(heading_a, heading_b) <= HEADING_TOL_DEG


def merge_with_neptun(markers: dict, neptun: dict) -> dict:
    """{mapa uid: neptun uid} for the pairs that are certainly the same threat. markers is
    {uid: track} of mapa markers, neptun {uid: track} of fresh NEPTUN threats. One-to-one only:
    a pair counts when each is compatible with no other candidate, so a doubtful pairing leaves
    both markers on the map instead of hiding a threat behind a wrong one."""
    pairs = {(m, n) for m, mt in markers.items() for n, nt in neptun.items() if _same_threat(mt, nt)}
    return {m: n for m, n in pairs
            if sum(1 for x, _ in pairs if x == m) == 1 and sum(1 for _, y in pairs if y == n) == 1}


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
    neptun: dict = {}                       # uid -> (track, time seen) of NEPTUN's threats on the fabric
    neptun_lock = threading.Lock()

    def on_neptun(sample) -> None:
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(track, dict) or not track.get("uid"):
            return
        with neptun_lock:
            if track.get("_delete"):
                neptun.pop(track["uid"], None)
            else:
                neptun[track["uid"]] = (track, time.time())

    neptun_sub = session.declare_subscriber(NEPTUN_TOPIC, on_neptun) if NEPTUN_MERGE else None
    published: dict = {}                    # uid -> (prefix, track) on the fabric now
    try:
        while True:
            data = fetch_current()
            if data is not None:
                now = time.time()
                current = {t["uid"]: (p, t) for p, t in object_tracks(data, now)}
                if NEPTUN_MERGE:
                    with neptun_lock:
                        fresh = {u: t for u, (t, seen) in neptun.items() if now - seen <= NEPTUN_MAX_AGE_S}
                    markers = {u: t for u, (_, t) in current.items() if not u.endswith(TRAIL_SUFFIX)}
                    for mapa_uid, neptun_uid in merge_with_neptun(markers, fresh).items():
                        current.pop(mapa_uid)                 # NEPTUN's marker stays; ours would draw it twice
                        trail = current.get(mapa_uid + TRAIL_SUFFIX)
                        if trail:
                            trail[1]["remarks"] += " | Same threat as {}".format(fresh[neptun_uid].get("callsign") or neptun_uid)
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
        if neptun_sub is not None:
            neptun_sub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
