#!/usr/bin/env python3
"""dronestrikemap_bridge.py — Drone Strike Map (dronestrikemap.com) → Zenoh bridge (open data).

Drone Strike Map is a one-person record of the drone and missile attacks on Ukraine and Russia, built from
published reporting (Russian MoD briefings, TASS, ASTRA, Ukrainska Pravda, Ukrainian Air Force, Kyiv
Independent...). Its record is CC BY 4.0: it may be reused for anything, commercial use included, as long as
the source is credited. The reporting itself belongs to the outlets it cites. The bridge uses the site's
public JSON routes (found in its page source; they are not documented, so they may change):
  GET /api/day/<YYYY-MM-DD>   the day's places hit: lat/lon (a place, not an impact point), place, region,
                              country (UA | RU), confidence (confirmed | reported | claimed), weapons, summary,
                              and the articles that report it
  GET /api/alerts             air-raid alerts per region, Russian regions included (unofficial for Russia)
The operator runs one small server: the bridge asks for two day files and one alert file every DSM_POLL_S
(10 minutes) and says who it is in its User-Agent.

Published (JSON tracks):
  <ORG>/land/dronestrikemap/strike/hostile/incident/...   a place hit in Russia, or in Russian-held Ukraine
  <ORG>/land/dronestrikemap/strike/friendly/incident/...  a place hit in Ukraine
      drawn by tak_layer / sitaware_layer as the 2525B incident "Explosion" (a-h-G-I-E / SHGPIE----).
      Confidence is in the name; the remarks carry the summary, weapons, outlets and the credit.
  <ORG>/land/dronestrikemap/airzone/neutral/zone/...      a Russian region under an air-raid alert, red,
      drawn on top of the faint standing-status regions that dangausakis_bridge.py publishes for Russia.
Strikes of today and yesterday (Kyiv dates) are shown; one marker per place per day. Anything that leaves
the record is retracted with a JSON tombstone.

Env:
  DSM_URL            site root (default https://dronestrikemap.com)
  DSM_POLL_S         poll interval (default 600, at least 120)
  DSM_STALE_S        how long a marker lives between polls (default 1800)
  DSM_DAYS           days shown, today first (default 2)
  DSM_COUNTRIES      places to draw, comma separated (default RU,UA)
  DSM_ALERT_ZONES    0 turns the Russian alert zones off (default on)
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from http_json import read_json_response
from namespace_prefix import topic_root
import zenoh
from protocols.vendors.random import region_geometry
from protocols.vendors.random.gateway import open_session
from protocols.vendors.random.geo_simplify import ring_area, simplify_ring
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
URL = os.environ.get("DSM_URL", "https://dronestrikemap.com").rstrip("/")
POLL_S = max(120, int(os.environ.get("DSM_POLL_S", "600")))
STALE_S = int(os.environ.get("DSM_STALE_S", "1800"))
DAYS = max(1, int(os.environ.get("DSM_DAYS", "2")))
COUNTRIES = {c.strip().upper() for c in os.environ.get("DSM_COUNTRIES", "RU,UA").split(",") if c.strip()}
ALERT_ZONES = os.environ.get("DSM_ALERT_ZONES", "1") not in {"0", "false", "no"}
STRIKE_PREFIX = "{}/land/dronestrikemap/strike".format(TOPIC_ROOT)
ZONE_PREFIX = "{}/land/dronestrikemap/airzone/neutral/zone".format(TOPIC_ROOT)
ZONE_MAX_PARTS = 3
ZONE_MAX_VERTICES = 600
ZONE_THEATRE = (10.0, 40.0, 70.0, 72.0)     # lon min, lat min, lon max, lat max
SCHEMA = "efdi:dronestrikemap_feature"
CREDIT = "Source: Drone Strike Map (dronestrikemap.com, CC BY 4.0); the reporting belongs to the outlets named"
_HEADERS = {"User-Agent": "EFDI-dronestrikemap-bridge (+open data, CC BY 4.0, credit Drone Strike Map)",
            "Accept": "application/json"}
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")
_ZONE_RINGS: dict = {}      # region id -> simplified rings: simplifying a Russian region is slow and it never changes


def _slug(text) -> str:
    return _UNSAFE.sub("-", str(text or "")).strip("-").lower() or "unknown"


def _kyiv_days(now: float, count: int) -> list:
    """The last `count` calendar dates in Kyiv (the site's own day), today first."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Kyiv")
    except Exception:                       # no tz database: UTC+3 is Kyiv's offset in the months of the war
        tz = timezone(timedelta(hours=3))
    today = datetime.fromtimestamp(now, tz).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(count)]


def _short_date(date: str) -> str:
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return date


def _outlets(strike: dict) -> str:
    names = []
    for article in strike.get("articles") or []:
        name = str(article.get("source") or "").strip()
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or "unnamed outlet"


def strike_tracks(day: dict, now: float, countries=COUNTRIES) -> dict:
    """uid -> (topic prefix, track) for one day document ({"date", "strikes": [...]}): a marker per place hit."""
    date = str(day.get("date") or "")
    out = {}
    for strike in day.get("strikes") or []:
        country = str(strike.get("country") or "").upper()
        place = str(strike.get("place") or "").strip()
        try:
            lat, lon = round(float(strike["lat"]), 5), round(float(strike["lon"]), 5)
        except (TypeError, ValueError, KeyError):
            continue
        if country not in countries or not place or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        confidence = str(strike.get("confidence") or "claimed")
        occupied = "occupied" in str(strike.get("region") or "").lower()
        uid = "DSM-{}-{}-{}".format(date, country, _slug(place)).upper()
        weapons = [str(w) for w in strike.get("weapons") or []]
        summary = str(strike.get("summary") or "").strip()
        out[uid] = (STRIKE_PREFIX + ("/hostile/incident" if country == "RU" else "/friendly/incident"), {
            "_src": "dronestrikemap.com", "_ts": now, "uid": uid, "type": "strike",
            "callsign": "{} hit {} ({}){}".format(place, _short_date(date), confidence,
                                                  " - occupied" if occupied else ""),
            "lat_deg": lat, "lon_deg": lon, "target_type": "strike", "position_uncertainty_m": 5000,
            "stale_s": STALE_S, "country": country, "region": strike.get("region") or "",
            "confidence": confidence, "weapons": weapons,
            "remarks": "{}{} - reported by {}. Position is the place, not the impact point. {}".format(
                summary or "strike reported", " ({})".format(", ".join(weapons)) if weapons else "",
                _outlets(strike), CREDIT),
        })
    return out


def _zone_rings(region: dict) -> list:
    if region["id"] not in _ZONE_RINGS:
        rings = [r for r in region["rings"] if region_geometry.overlaps(r, ZONE_THEATRE)][:ZONE_MAX_PARTS]
        rings = [r for r in rings if ring_area(r) >= 0.02 * ring_area(rings[0])]
        _ZONE_RINGS[region["id"]] = [simplify_ring(r, ZONE_MAX_VERTICES) for r in rings]
    return _ZONE_RINGS[region["id"]]


def alert_zone_tracks(alerts: dict, index: dict, now: float) -> dict:
    """uid -> (topic prefix, track): a red polygon per Russian region (not occupied Ukraine: dangausakis draws
    those) with an active, not stale air-raid alert."""
    out = {}
    for alert in alerts.get("active") or []:
        if alert.get("country") != "RU" or alert.get("occupied") or alert.get("stale") or alert.get("kind") != "air_raid":
            continue
        region = region_geometry.match_region(index, alert.get("region"))
        if region is None:
            print("dronestrikemap: no outline for the alert region {!r}".format(alert.get("region")), flush=True)
            continue
        for part, ring in enumerate(_zone_rings(region)):
            uid = "DSM-ALERT-{}{}".format(_slug(alert.get("region_key") or alert.get("region")).upper(),
                                          "-p{}".format(part) if part else "")
            out[uid] = (ZONE_PREFIX, {
                "_src": "dronestrikemap.com", "_ts": now, "uid": uid, "type": "air_alert_zone",
                "callsign": "RU {} RED".format(region["name"]),
                "lat_deg": round(sum(p[1] for p in ring[:-1]) / (len(ring) - 1), 5),
                "lon_deg": round(sum(p[0] for p in ring[:-1]) / (len(ring) - 1), 5),
                "geometry": {"type": "Polygon", "coordinates": [ring]}, "stale_s": 1800,
                "shape_color": "red", "level": "red", "country": "RU", "alert_id": "DSM:" + str(region["id"]),
                "remarks": "Air-raid alert since {} - source: {} via Drone Strike Map - not an official warning. {} - {}".format(
                    alert.get("started_at") or "?", alert.get("source") or "?", CREDIT, region_geometry.CREDIT),
            })
    return out


def _get_json(path: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + path, headers=_HEADERS), timeout=30) as resp:
            return read_json_response(resp, max_bytes=8 * 1024 * 1024)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print("dronestrikemap fetch {} failed: {}".format(path, exc), flush=True)
        return None


def fetch_day(date: str) -> dict | None:
    data = _get_json("/api/day/" + date)
    return data if isinstance(data, dict) and isinstance(data.get("strikes"), list) else None


def fetch_alerts() -> dict | None:
    data = _get_json("/api/alerts")
    return data if isinstance(data, dict) and isinstance(data.get("active"), list) else None


def _put(session, prefix: str, track: dict) -> None:
    session.put(add_version(semantic_topic(prefix, track)), json.dumps(track).encode(),
                encoding=zenoh.Encoding.APPLICATION_JSON.with_schema(SCHEMA))


def main():
    ap = argparse.ArgumentParser(description="Drone Strike Map → Zenoh bridge (open data, CC BY 4.0)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("dronestrikemap Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    print("dronestrikemap bridge starting (poll {}s, {} day(s), countries {}, alert zones {})".format(
        POLL_S, DAYS, ",".join(sorted(COUNTRIES)), "on" if ALERT_ZONES else "off"), flush=True)

    index = region_geometry.region_index(region_geometry.regions("RUS")) if ALERT_ZONES else {}
    published: dict = {}        # uid -> (prefix, track) now on the fabric
    try:
        while True:
            now = time.time()
            current: dict = {}
            fetched_days = 0
            for date in _kyiv_days(now, DAYS):
                day = fetch_day(date)
                if day is not None:
                    fetched_days += 1
                    current.update(strike_tracks(day, now))
                else:                               # keep that day's markers: a failed fetch is not "nothing was hit"
                    for uid, (prefix, track) in published.items():
                        if uid.startswith("DSM-{}-".format(date)):
                            current[uid] = (prefix, dict(track, _ts=now))
            alerts = fetch_alerts() if ALERT_ZONES else None
            if alerts is not None and not index:
                index = region_geometry.region_index(region_geometry.regions("RUS"))
            if alerts is not None:
                current.update(alert_zone_tracks(alerts, index, now))
            if fetched_days == 0 and alerts is None:
                current = dict(published)           # the site is down: keep what is drawn, it expires by itself
                for _, track in current.values():
                    track["_ts"] = now
            for uid, (prefix, track) in current.items():
                _put(session, prefix, track)
            for uid in set(published) - set(current):
                prefix, track = published[uid]
                session.put(add_version(semantic_topic(prefix, track)),
                            json.dumps({"uid": uid, "type": track.get("type"), "_delete": True, "_ts": now,
                                        "_src": "dronestrikemap.com"}).encode())
            published = current
            print("dronestrikemap: {} markers/zones ({} day files)".format(len(published), fetched_days), flush=True)
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
