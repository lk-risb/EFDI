#!/usr/bin/env python3
"""geoconfirmed_bridge.py — GeoConfirmed (geoconfirmed.org) → Zenoh bridge (open data).

GeoConfirmed is a volunteer project that geolocates and verifies conflict footage: every placemark is a place a
video or photo was shown to come from, with the side, the kind of event and the date. Its changelog lists public
endpoints; the bridge uses two (neither needs a key):
  GET /api/placemark/geojson   every conflict's placemarks, 20 MB, answers 304 when nothing changed (ETag)
  GET /api/placemark/icons     the names of the event kinds, fetched once a day
Only the Ukraine conflict (code UKR) of the last GC_DAYS days is drawn. The service asks for the file with
If-None-Match every GC_POLL_S, so an unchanged map costs one empty reply, and says who it is in its User-Agent.
No licence or terms for the API were found: every marker credits GeoConfirmed, and the verified evidence stays on
its website. Positions are geolocated places, much finer than a city.

Published (JSON tracks): <ORG>/land/geoconfirmed/event/<side>/incident/event/<uid>, drawn by tak_layer and
sitaware_layer as the 2525B incident "Explosion" (SxGPIE----). The side is the striker's, as in dronestrikemap_bridge.py:
friendly (Ukraine, NATO) or hostile (Russia, Belarus, North Korea). GeoConfirmed colours a placemark by its faction, and
what the colour means depends on the kind of event, which the bridge reads from the kind's name:
  - a weapon in use or a shelling, a drone, missile or bomb, a civilian killed or wounded, a video or picture of an
    attack: the colour is whoever attacked, so the side is the faction's;
  - something destroyed or damaged (a tank, a truck, a radar, a bunker, a soldier killed or wounded): the colour is
    the owner, who was struck, so the side is the opposite faction's;
  - anything else (a tank driving, a prisoner, a bridge, an infrastructure fire of unknown origin): not a strike, so
    the side is the faction's, or neutral for a civilian or neutral placemark, and the remarks say so.
The name is the kind of event and its date; the remarks carry the full kind, who struck and the credit. A placemark that
leaves the window is retracted with a JSON tombstone.

Env:
  GC_URL        site root (default https://geoconfirmed.org)
  GC_POLL_S     poll interval (default 900, at least 300)
  GC_STALE_S    how long a marker lives between polls (default 2400)
  GC_DAYS       days shown, today first (default 3)
  GC_MAX        most markers drawn, newest first (default 400)
"""

import argparse
import gzip
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from namespace_prefix import topic_root
import zenoh
from protocols.vendors.random.gateway import open_session
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
URL = os.environ.get("GC_URL", "https://geoconfirmed.org").rstrip("/")
POLL_S = max(300, int(os.environ.get("GC_POLL_S", "900")))
STALE_S = min(3600, int(os.environ.get("GC_STALE_S", "2400")))
DAYS = max(1, int(os.environ.get("GC_DAYS", "3")))
MAX_MARKERS = max(1, int(os.environ.get("GC_MAX", "400")))
PREFIX = "{}/land/geoconfirmed/event".format(TOPIC_ROOT)
CONFLICT = "UKR"
MAX_BYTES = 64 * 1024 * 1024
SCHEMA = "efdi:geoconfirmed_event"
CREDIT = "Source: GeoConfirmed (geoconfirmed.org), volunteer geolocation of conflict footage; the evidence is on their site"
_HEADERS = {"User-Agent": "EFDI-geoconfirmed-bridge (+open data, credit GeoConfirmed)",
            "Accept": "application/json", "Accept-Encoding": "gzip"}
# the side a faction is drawn as: friendly (Ukraine, NATO), hostile (Russia, Belarus, North Korea), neutral, unknown
_SIDES = {"ukraine": "friendly", "nato": "friendly", "russia": "hostile", "belarus": "hostile", "north korea": "hostile",
          "neutral": "neutral", "special": "unknown", "unknown": "unknown"}


def _side(faction_name: str) -> str:
    base = re.sub(r"\s*\([A-Za-z0-9-]+\)\s*$", "", str(faction_name or "")).strip().lower()
    if base.endswith(" civilian"):
        return "neutral"                # a civilian is nobody's unit, whichever side's territory it is on
    return _SIDES.get(base, "unknown")


_FACTION_LABEL = {"ukraine": "Ukrainian", "russia": "Russian", "belarus": "Belarusian", "nato": "NATO", "north korea": "North Korean"}
_OPPOSITE = {"friendly": "hostile", "hostile": "friendly"}
# kinds whose colour is whoever attacked: weapons in use or lost in an attack, shelling, civilian casualties, footage of an attack
_STRIKER = re.compile(r"^shelling by artillery|^killed or wounded civilian|^(picture|video) -|owad|loitering|bomber drone|"
                      r"cruise missile|suicide ugv", re.I)
# kinds whose colour is the owner of what was struck
_VICTIM = re.compile(r"destroyed|damaged|^wia/kia|^older kia", re.I)


def _base_faction(faction_name: str) -> str:
    base = re.sub(r"\s*\([A-Za-z0-9-]+\)\s*$", "", str(faction_name or "")).strip().lower()
    return base[:-len(" civilian")] if base.endswith(" civilian") else base


def striker(kind: str, faction_name: str):
    """(side of whoever struck, role) for a placemark: role is "striker" (the colour is the attacker), "victim" (the colour
    is the owner of what was hit, so the striker is the other side) or "other" (not a strike; the side is the faction's,
    neutral for civilians). The side is friendly, hostile, neutral or unknown."""
    side = _side(faction_name)
    base = _base_faction(faction_name)
    if re.search(r"unknown origin", kind or "", re.I):
        return "neutral", "other"                   # an explosion nobody is credited with
    if _STRIKER.search(kind or ""):
        role = "striker"
    elif _VICTIM.search(kind or ""):
        role = "victim"
    else:
        return side, "other"
    side = _SIDES.get(base, "unknown")
    return (_OPPOSITE.get(side, side) if role == "victim" else side), role


def _template(icon_path: str) -> str:
    return str(icon_path or "").rsplit("/", 1)[-1]


def _short(name: str, limit: int = 44) -> str:
    """The first clause of an event kind, bracketed notes dropped: "Recently Destroyed/Damaged[Visible in footage]
    Logistical Trucks (Soft..." -> "Recently Destroyed/Damaged Logistical Trucks"."""
    plain = re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", " ", str(name or ""))).strip()
    head = re.split(r"\s+[-–]\s+|\(|\.\s|,", plain, maxsplit=1)[0].strip() or plain
    return head if len(head) <= limit else head[:limit - 1].rstrip() + "…"


def kind_names(icons_doc) -> dict:
    """icon template (the file name in the icon path) -> the event kind's name, from /api/placemark/icons."""
    out = {}
    for conflict in icons_doc if isinstance(icons_doc, list) else []:
        if conflict.get("conflictCode") != CONFLICT:
            continue
        for faction in conflict.get("factions") or []:
            for icon in faction.get("icons") or []:
                if icon.get("name"):
                    out.setdefault(_template(icon.get("icon")), str(icon["name"]))
    return out


def _kyiv_days(now: float, count: int) -> list:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Kyiv")
    except Exception:                       # no tz database: UTC+3 is Kyiv's offset in the months of the war
        tz = timezone(timedelta(hours=3))
    today = datetime.fromtimestamp(now, tz).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(count)]


def event_tracks(world: dict, kinds: dict, now: float, days: int = DAYS, limit: int = MAX_MARKERS) -> dict:
    """uid -> (topic prefix, track) for the Ukraine-conflict placemarks of the last `days` Kyiv dates, newest first, at most `limit`."""
    factions = {m["id"]: m["name"] for m in world.get("factionMeta") or [] if m.get("conflictCode") == CONFLICT}
    window = set(_kyiv_days(now, days))
    picked = []
    for feature in (world.get("geojson") or {}).get("features") or []:
        props, geometry = feature.get("properties") or {}, feature.get("geometry") or {}
        faction = factions.get(props.get("factionId"))
        date = str(props.get("date") or "")[:10]
        coords = geometry.get("coordinates") or []
        if faction is None or date not in window or geometry.get("type") != "Point" or len(coords) < 2 or not props.get("id"):
            continue
        try:
            lon, lat = round(float(coords[0]), 6), round(float(coords[1]), 6)
        except (TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            picked.append((str(props.get("date")), props, faction, lat, lon))
    out = {}
    for stamp, props, faction, lat, lon in sorted(picked, key=lambda p: p[0], reverse=True)[:limit]:
        full = kinds.get(_template(props.get("icon"))) or "event"
        side, role = striker(full, faction)
        who = _FACTION_LABEL.get(_base_faction(faction)) if role == "striker" else \
            _FACTION_LABEL.get({"friendly": "ukraine", "hostile": "russia"}.get(side, ""), "") if role == "victim" else ""
        note = ("{} strike{}".format(who, " (the owner was struck, inferred)" if role == "victim" else "") if who
                else "not a strike" if role == "other" else "striker unknown")
        uid = "GC-{}".format(str(props["id"]).replace("-", "")[:12]).upper()
        out[uid] = ("{}/{}/incident".format(PREFIX, side), {
            "_src": "geoconfirmed.org", "_ts": now, "uid": uid, "type": "event",
            "callsign": "{} {}".format(_short(full), datetime.strptime(stamp[:10], "%Y-%m-%d").strftime("%d %b")),
            "lat_deg": lat, "lon_deg": lon, "target_type": "event", "position_uncertainty_m": 100,
            "stale_s": STALE_S, "faction": faction, "event_date": stamp[:10], "role": role,
            "remarks": "{} - {} - {} - geolocated placemark {}. {}".format(
                full[:200], faction, note, props["id"], CREDIT),
        })
    return out


def _fetch(path: str, etag: str | None = None, timeout: int = 120):
    """(status, etag, body bytes): status 304 with no body when the ETag still matches, 0 on a failure."""
    headers = dict(_HEADERS, **({"If-None-Match": etag} if etag else {}))
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + path, headers=headers), timeout=timeout) as resp:
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("response over {} bytes".format(MAX_BYTES))
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(MAX_BYTES + 1)
            return resp.status, resp.headers.get("ETag"), body
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, etag, None
        print("geoconfirmed fetch {} failed: HTTP {}".format(path, exc.code), flush=True)
    except (OSError, ValueError, EOFError) as exc:
        print("geoconfirmed fetch {} failed: {}".format(path, exc), flush=True)
    return 0, etag, None


def _put(session, prefix: str, track: dict) -> None:
    session.put(add_version(semantic_topic(prefix, track)), json.dumps(track).encode(),
                encoding=zenoh.Encoding.APPLICATION_JSON.with_schema(SCHEMA))


def main():
    ap = argparse.ArgumentParser(description="GeoConfirmed → Zenoh bridge (open data)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("geoconfirmed Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    print("geoconfirmed bridge starting (poll {}s, {} day(s), at most {} markers)".format(POLL_S, DAYS, MAX_MARKERS), flush=True)

    world, etag, kinds, next_kinds = None, None, {}, 0.0
    published: dict = {}
    try:
        while True:
            now = time.time()
            status, new_etag, body = _fetch("/api/placemark/geojson", etag)
            if status == 200 and body:
                try:
                    world, etag = json.loads(body.decode("utf-8")), new_etag
                except ValueError as exc:
                    print("geoconfirmed bad JSON: {}".format(exc), flush=True)
            if now >= next_kinds or not kinds:
                code, _, raw = _fetch("/api/placemark/icons")
                if code == 200 and raw:
                    try:
                        kinds = kind_names(json.loads(raw.decode("utf-8"))) or kinds
                        next_kinds = now + 86400
                    except ValueError as exc:
                        print("geoconfirmed bad icons JSON: {}".format(exc), flush=True)
            if world is not None:
                current = event_tracks(world, kinds, now)
                for prefix, track in current.values():
                    _put(session, prefix, track)
                for uid in set(published) - set(current):
                    prefix, track = published[uid]
                    session.put(add_version(semantic_topic(prefix, track)),
                                json.dumps({"uid": uid, "type": "event", "_delete": True, "_ts": now,
                                            "_src": "geoconfirmed.org"}).encode())
                published = current
                print("geoconfirmed: {} placemarks of the last {} day(s) ({})".format(
                    len(published), DAYS, "unchanged" if status == 304 else "map fetched" if status == 200 else "site down"), flush=True)
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
