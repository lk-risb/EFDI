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
  <ORG>/land/dronestrikemap/strike/friendly/incident/...  a place hit in Russia, or in Russian-held Ukraine: a
                                                          Ukrainian strike
  <ORG>/land/dronestrikemap/strike/hostile/incident/...   a place hit in Ukraine: a Russian strike
      drawn by tak_layer / sitaware_layer as the 2525B incident "Explosion" (a-f-G-I-E / SFGPIE---- blue,
      a-h-G-I-E / SHGPIE---- red). The colour says who struck, as Ukraine is the friendly side here; the site
      records where something landed, so the striker is inferred from the place. Confidence is in the name;
      the remarks carry the summary, weapons, outlets and the credit.
  <ORG>/land/dronestrikemap/airzone/neutral/zone/...      a Russian region that is more than "at war": red with an
      active air-raid alert, yellow with a place hit today or yesterday. Drawn on top of the orange regions that
      dangausakis_bridge.py publishes for all of Russia (a country at war: nowhere is safe), as the Ukrainian oblasts
      are coloured all over with the alert districts on top.
It also announces, on <ORG>/land/dronestrikemap/status/incursions, where drones were shot down, crashed or struck in the
last three days in NATO-border countries (from /api/incidents, only those with a position better than the country's
centre); dangausakis_bridge.py draws the region that holds one yellow, at peace as well as at war. And on
<ORG>/land/dronestrikemap/status/war, the countries it records strikes on (the two above, and
a NATO country with a recent incident of kind "strike"); dangausakis_bridge.py draws an announced country orange.
Strikes of today and yesterday (Kyiv dates) are shown; one marker per place per day. Anything that leaves
the record is retracted with a JSON tombstone.

Env:
  DSM_URL            site root (default https://dronestrikemap.com)
  DSM_POLL_S         poll interval (default 600, at least 120)
  DSM_STALE_S        how long a marker lives between polls (default 1800)
  DSM_DAYS           days shown, today first (default 2)
  DSM_INCURSION_DAYS an incident (a drone shot down, crashed, struck) keeps its region yellow this many days (default 3)
  DSM_COUNTRIES      places to draw, comma separated (default RU,UA)
  DSM_ALERT_ZONES    0 turns the yellow and red Russian regions off (default on)
  DSM_DROP_REGEX     skip a strike whose place, region, summary or article titles match this regex (default none):
                     the site's own extraction sometimes reads an opinion piece as an attack
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
from protocols.vendors.random.war_status import incursion_topic, war_topic

TOPIC_ROOT = topic_root()
URL = os.environ.get("DSM_URL", "https://dronestrikemap.com").rstrip("/")
POLL_S = max(120, int(os.environ.get("DSM_POLL_S", "600")))
STALE_S = int(os.environ.get("DSM_STALE_S", "1800"))
DAYS = max(1, int(os.environ.get("DSM_DAYS", "2")))
COUNTRIES = {c.strip().upper() for c in os.environ.get("DSM_COUNTRIES", "RU,UA").split(",") if c.strip()}
ALERT_ZONES = os.environ.get("DSM_ALERT_ZONES", "1") not in {"0", "false", "no"}
_DROP = re.compile(os.environ["DSM_DROP_REGEX"], re.I) if os.environ.get("DSM_DROP_REGEX") else None
STRIKE_PREFIX = "{}/land/dronestrikemap/strike".format(TOPIC_ROOT)
ZONE_PREFIX = "{}/land/dronestrikemap/airzone/neutral/zone".format(TOPIC_ROOT)
ZONE_MAX_PARTS = 3
ZONE_MAX_VERTICES = 600
ZONE_THEATRE = (10.0, 40.0, 70.0, 72.0)     # lon min, lat min, lon max, lat max
SCHEMA = "efdi:dronestrikemap_feature"
CREDIT = "Source: Drone Strike Map (dronestrikemap.com, CC BY 4.0); the reporting belongs to the outlets named"
_HEADERS = {"User-Agent": "EFDI-dronestrikemap-bridge (+open data, CC BY 4.0, credit Drone Strike Map)",
            "Accept": "application/json"}
INCIDENT_POLL_S = 3600           # /api/incidents is 120 KB and changes slowly
INCIDENT_DAYS = 3
INCURSION_DAYS = max(1, int(os.environ.get("DSM_INCURSION_DAYS", "3")))   # an incident this recent keeps its region yellow
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


def _titles(strike: dict, limit: int = 2) -> str:
    """The first article titles, so a reader can judge the evidence: the site's extraction can misread a story."""
    return "; ".join('"{}"'.format(str(a.get("title") or "").strip()[:110]) for a in (strike.get("articles") or [])[:limit]
                     if a.get("title"))


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
        if _DROP is not None and _DROP.search(" ".join(
                [place, str(strike.get("region") or ""), str(strike.get("summary") or "")]
                + [str(a.get("title") or "") for a in strike.get("articles") or []])):
            continue
        confidence = str(strike.get("confidence") or "claimed")
        occupied = "occupied" in str(strike.get("region") or "").lower()
        uid = "DSM-{}-{}-{}".format(date, country, _slug(place)).upper()
        weapons = [str(w) for w in strike.get("weapons") or []]
        summary = str(strike.get("summary") or "").strip()
        out[uid] = (STRIKE_PREFIX + ("/friendly/incident" if country == "RU" else "/hostile/incident"), {
            "_src": "dronestrikemap.com", "_ts": now, "uid": uid, "type": "strike",
            "callsign": "{} hit {} ({}){}".format(place, _short_date(date), confidence,
                                                  " - occupied" if occupied else ""),
            "lat_deg": lat, "lon_deg": lon, "target_type": "strike", "position_uncertainty_m": 5000, "place": place,
            "stale_s": STALE_S, "country": country, "region": strike.get("region") or "",
            "confidence": confidence, "weapons": weapons, "side_basis": "place",
            "remarks": "{}{} - reported by {}. {} strike, inferred from where it landed; the position is the place, "
                       "not the impact point. {}".format(
                summary or "strike reported", " ({})".format(", ".join(weapons)) if weapons else "",
                _outlets(strike) + (" (" + _titles(strike) + ")" if _titles(strike) else ""),
                "Ukrainian" if country == "RU" else "Russian", CREDIT),
        })
    return out


def _zone_rings(region: dict) -> list:
    if region["id"] not in _ZONE_RINGS:
        rings = [r for r in region["rings"] if region_geometry.overlaps(r, ZONE_THEATRE)][:ZONE_MAX_PARTS]
        rings = [r for r in rings if ring_area(r) >= 0.02 * ring_area(rings[0])]
        _ZONE_RINGS[region["id"]] = [simplify_ring(r, ZONE_MAX_VERTICES) for r in rings]
    return _ZONE_RINGS[region["id"]]


def region_zone_tracks(alerts: dict, strikes: dict, index: dict, now: float) -> dict:
    """uid -> (topic prefix, track): a polygon per Russian region that is more than "at war" (the orange every
    region starts from, drawn by dangausakis_bridge.py): red when it has an active air-raid alert, yellow when a place
    in it was hit today or yesterday and it has no alert. Occupied Ukrainian regions are left to the Ukrainian
    zones. `strikes` is the uid -> (prefix, track) of strike_tracks()."""
    levels: dict = {}                   # region id -> [level, region, places hit, alert]
    for _, strike in strikes.values():
        if strike.get("country") != "RU" or "occupied" in str(strike.get("region") or "").lower():
            continue
        region = region_geometry.match_region(index, strike.get("region"))
        if region is None:
            _warn_unmatched(strike.get("region"))
            continue
        entry = levels.setdefault(region["id"], ["yellow", region, [], None])
        if strike.get("place") and strike["place"] not in entry[2]:
            entry[2].append(strike["place"])
    for alert in alerts.get("active") or []:
        if alert.get("country") != "RU" or alert.get("occupied") or alert.get("stale") or alert.get("kind") != "air_raid":
            continue
        region = region_geometry.match_region(index, alert.get("region"))
        if region is None:
            _warn_unmatched(alert.get("region"))
            continue
        entry = levels.setdefault(region["id"], ["red", region, [], None])
        entry[0], entry[3] = "red", alert
    out = {}
    for level, region, places, alert in levels.values():
        notes = []
        if alert:
            notes.append("Air-raid alert since {} - source: {} via Drone Strike Map".format(
                alert.get("started_at") or "?", alert.get("source") or "?"))
        if places:
            notes.append("Places hit today or yesterday: {}".format(", ".join(places)))
        for part, ring in enumerate(_zone_rings(region)):
            uid = "DSM-REGION-{}{}".format(_slug(region["id"]).upper(), "-p{}".format(part) if part else "")
            out[uid] = (ZONE_PREFIX, {
                "_src": "dronestrikemap.com", "_ts": now, "uid": uid, "type": "air_alert_zone",
                "callsign": "RU {} {}".format(region["name"], level.upper()),
                "lat_deg": round(sum(p[1] for p in ring[:-1]) / (len(ring) - 1), 5),
                "lon_deg": round(sum(p[0] for p in ring[:-1]) / (len(ring) - 1), 5),
                "geometry": {"type": "Polygon", "coordinates": [ring]}, "stale_s": 1800,
                "shape_color": level, "level": level, "country": "RU", "alert_id": "DSM:" + str(region["id"]),
                "remarks": "{} - not an official warning. {} - {}".format(
                    ". ".join(notes), CREDIT, region_geometry.CREDIT),
            })
    return out


_UNMATCHED: set = set()


def _warn_unmatched(name) -> None:
    if name not in _UNMATCHED:
        _UNMATCHED.add(name)
        print("dronestrikemap: no outline for the Russian region {!r}".format(name), flush=True)


def war_countries(strikes: dict, incidents: list, today: str) -> list:
    """Countries to announce as under attack: every country with a place hit in `strikes` (strike_tracks() output) and
    every country of a NATO-border incident of kind "strike" in the last INCIDENT_DAYS days. A shot-down drone or a crash
    is not an attack on that country. dangausakis_bridge.py turns the announced countries orange (war_status.py)."""
    countries = {track.get("country") for _, track in strikes.values()}
    try:
        since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=INCIDENT_DAYS)).strftime("%Y-%m-%d")
    except ValueError:
        since = "0000-00-00"
    countries |= {i.get("country") for i in incidents if i.get("kind") == "strike" and str(i.get("date") or "") >= since}
    return sorted(c for c in countries if c)


def incursion_points(incidents: list, today: str) -> list:
    """[{"country", "lat", "lon", "kind", "place"}] for the incidents of the last INCURSION_DAYS days that have a position
    better than the country's centre: where a drone was shot down, crashed or struck. dangausakis_bridge.py draws the
    region that holds one yellow."""
    try:
        since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=INCURSION_DAYS)).strftime("%Y-%m-%d")
    except ValueError:
        since = "0000-00-00"
    out = []
    for i in incidents:
        if str(i.get("date") or "") < since or i.get("geo_precision") == "country":
            continue
        try:
            out.append({"country": str(i["country"]).upper(), "lat": float(i["lat"]), "lon": float(i["lon"]),
                        "kind": i.get("kind"), "place": i.get("place")})
        except (KeyError, TypeError, ValueError):
            continue
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


def fetch_incidents() -> list | None:
    data = _get_json("/api/incidents")
    return data["incidents"] if isinstance(data, dict) and isinstance(data.get("incidents"), list) else None


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
    print("dronestrikemap bridge starting (poll {}s, {} day(s), countries {}, region zones {})".format(
        POLL_S, DAYS, ",".join(sorted(COUNTRIES)), "on" if ALERT_ZONES else "off"), flush=True)

    index = region_geometry.region_index(region_geometry.regions("RUS")) if ALERT_ZONES else {}
    published: dict = {}        # uid -> (prefix, track) now on the fabric
    incidents, next_incidents = [], 0.0
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
            if ALERT_ZONES:
                if not index:
                    index = region_geometry.region_index(region_geometry.regions("RUS"))
                strikes = {u: v for u, v in current.items() if v[1].get("type") == "strike"}
                current.update(region_zone_tracks(alerts or {}, strikes, index, now))
            if fetched_days == 0 and alerts is None:
                current = dict(published)           # the site is down: keep what is drawn, it expires by itself
                for _, track in current.values():
                    track["_ts"] = now
            if now >= next_incidents:
                fetched = fetch_incidents()
                if fetched is not None:
                    incidents = fetched
                next_incidents = now + (INCIDENT_POLL_S if fetched is not None else POLL_S)
            countries = war_countries({u: v for u, v in current.items() if v[1].get("type") == "strike"}, incidents,
                                      _kyiv_days(now, 1)[0])
            if countries:
                session.put(war_topic(TOPIC_ROOT), json.dumps({"countries": countries, "ts": now}).encode())
            session.put(incursion_topic(TOPIC_ROOT), json.dumps(
                {"incidents": incursion_points(incidents, _kyiv_days(now, 1)[0]), "ts": now}).encode())
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
