#!/usr/bin/env python3
"""dangausakis_bridge.py — dangausakis.lt regional air-raid alerts → Zenoh.

Polls https://dangausakis.lt/wp-json/dangaus-akis/v1/region-alerts, the public
JSON the site's own map reads. It aggregates official/aggregator alerts for
LT (lt72.lt), LV (112.lv), PL (komunikaty.tvp.pl), EE and UA (neptun.in.ua).
No API key. Not an official warning system — the site says so itself.

Alerts have no coordinates (only an area name), so they are published as
status messages, not tracks: tak_alert_layer.py turns each one into a TAK
GeoChat broadcast. Topic:
  <ORG>/land/dangausakis/alert/neutral/zone/status

Active alerts are also drawn as coloured region polygons (red / orange / yellow by
level) for every country the site covers (LT counties, LV municipalities, PL
voivodeships, EE parishes, UA oblasts), published JSON-only as zone tracks under
  <ORG>/land/dangausakis/airzone/neutral/zone/...
so tak_layer (and sitaware_layer) draw them. Boundaries come from the site's own
/region-geometry route (fallback: the adminGeoData.<hash>.js map bundle found via
the map page; Natural Earth / national mapping agencies / NEPTUN as credited in the
data), loaded lazily,
simplified, and refreshed every 6 h; if they cannot be loaded only the GeoChat
alerts run. JSON-only on purpose: an alert zone is not a detected object, so no
SAPIENT detection is emitted for it.

Where an upstream has a real feed the bridge reads it directly and that replaces the
site's data for the country (alert_sources.py): NEPTUN's district alerts for UA and the
LT72 warnings RSS for LT. Poland's RSO notices are only ADDED to the site's Polish alerts.
LV and EE have no machine-readable source and come from the site's feed. If a direct
source fails its last good data is kept for DANGAUSAKIS_SOURCE_MAX_AGE_S, then dropped.

It also runs the NEPTUN threats feed (neptun.py in this directory): live drones,
missiles and glide bombs over Ukraine as hostile tracks, with the radar-match
suppression handshake with fusion. Set NEPTUN_ENABLED=0 to turn that part off.

It also polls .../aircraft (adsb.lol data, ODbL-1.0) and publishes each
aircraft as a normal track via publish_dual() under
  <ORG>/air/dangausakis/adsb|mlat/civ/aircraft/...
keyed by icao24, so tak_layer/sitaware_layer merge it with the same aircraft
from any other ADS-B source into one marker. A feed flagged stale, or a
position older than 60 s, is not published.

An alert that disappears from the feed is re-published once with _delete=true
so the layer can re-arm. Alerts older than DANGAUSAKIS_ALERT_MAX_AGE_S are
dropped: occupied Ukrainian regions sit in the feed as standing alerts since
2022 and must not pop up on every operator's screen.

Env:
  DANGAUSAKIS_POLL_S            poll interval, seconds (default 60)
  DANGAUSAKIS_ALERT_MAX_AGE_S   ignore alerts whose `since` is older (default 21600)
  DANGAUSAKIS_COUNTRIES         comma list e.g. LT,LV,PL; empty = all
  DANGAUSAKIS_SOURCE_MAX_AGE_S  treat a country's alerts as stale if its source has not updated
                                for this long, or reports an error (default 1800)
  DANGAUSAKIS_AIRCRAFT_POLL_S   aircraft poll interval, seconds (default 15)
  DANGAUSAKIS_ZONES             set 0 to disable the coloured region polygons
  DANGAUSAKIS_COMMUNITY         set 0 to disable the community drone reports and incident
                                markers (tracks under air/dangausakis/community and
                                land/dangausakis/incident); default on
  DANGAUSAKIS_COMMUNITY_POLL_S  their poll interval, seconds (default 30)
  DANGAUSAKIS_EVENTS            set 0 to disable the status-change / data-issue / new-report
                                events that tak_alert_layer turns into GeoChat (default on)
  DANGAUSAKIS_DIRECT_SOURCES    comma list of direct alert sources to use: neptun,lt72,rso,lv
                                (default all three; empty = the site's feed only)
  NEPTUN_ENABLED                set 0 to disable the NEPTUN threats feed (default on)
  NEPTUN_POLL_S / NEPTUN_STALE_S / NEPTUN_MAX_AGE_S   see neptun.py
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
from datetime import datetime, timezone

from http_json import read_json_response
from namespace_prefix import topic_root
import zenoh
from bridges.vendors.dangausakis import alert_sources, neptun
from protocols.vendors.random.gateway import open_session, publish_dual, subscribe
from protocols.vendors.random.proto.normalized_track_pb2 import NormalizedTrack
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
API = "https://dangausakis.lt/wp-json/dangaus-akis/v1/"
_MAX_BODY_BYTES = 16 * 1024 * 1024    # the JS-bundle fallback is ~400 KB
_HEADERS = {
    "Referer": "https://dangausakis.lt/zemelapis/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
POLL_S = int(os.environ.get("DANGAUSAKIS_POLL_S", "60"))
MAX_AGE_S = int(os.environ.get("DANGAUSAKIS_ALERT_MAX_AGE_S", "21600"))
# The site's own `stale` flag proved unreliable (feed ~34 h behind NEPTUN's live data with
# stale=false), so a country's data is also treated as stale when its source stopped
# updating. The limit is generous (30 min) because `updatedAt` looks like the last poll
# (it differs from `upstreamUpdatedAt`), but that is inferred, not documented — a quiet
# period must not be mistaken for a dead feed.
SOURCE_MAX_AGE_S = int(os.environ.get("DANGAUSAKIS_SOURCE_MAX_AGE_S", "1800"))
DIRECT_SOURCES = {s.strip().lower() for s in os.environ.get("DANGAUSAKIS_DIRECT_SOURCES", "neptun,lt72,rso,lv").split(",")
                  if s.strip()}
NEPTUN_ENABLED = os.environ.get("NEPTUN_ENABLED", "1") not in {"0", "false", "no"}
ZONES_ENABLED = os.environ.get("DANGAUSAKIS_ZONES", "1") not in {"0", "false", "no"}
MAP_PAGE = "https://dangausakis.lt/zemelapis/"
_BOUNDARY_JS_RE = re.compile(r"/wp-content/plugins/dangaus-akis-map-assets/assets/adminGeoData\.[0-9a-f]+\.js")
BOUNDARY_REFRESH_S = 6 * 3600
BOUNDARY_RETRY_S = 300
ZONE_MAX_VERTICES = 150      # layers draw at most 256 points per ring
ZONE_MAX_PARTS = 8           # largest parts of a multi-part region
ZONE_COLORS = {"red", "orange", "yellow"}
ZONE_PREFIX = "{}/land/dangausakis/airzone/neutral/zone".format(TOPIC_ROOT)
AIRCRAFT_POLL_S = int(os.environ.get("DANGAUSAKIS_AIRCRAFT_POLL_S", "15"))
AIRCRAFT_MAX_POSITION_AGE_S = 60
COMMUNITY_ENABLED = os.environ.get("DANGAUSAKIS_COMMUNITY", "1") != "0"
COMMUNITY_POLL_S = int(os.environ.get("DANGAUSAKIS_COMMUNITY_POLL_S", "30"))
EVENTS_ENABLED = os.environ.get("DANGAUSAKIS_EVENTS", "1") != "0"
COMMUNITY_REPORT_PREFIX = "{}/air/dangausakis/community/unknown/uav".format(TOPIC_ROOT)
COMMUNITY_INCIDENT_PREFIX = "{}/land/dangausakis/incident/unknown/unit".format(TOPIC_ROOT)
_DIRECT_LABELS = (("neptun", "NEPTUN"), ("lt72", "LT72"), ("rso", "RSO"), ("lv", "112.lv"))
_FT_M, _KT_MS = 0.3048, 0.514444
COUNTRIES = {c.strip().upper() for c in os.environ.get("DANGAUSAKIS_COUNTRIES", "").split(",") if c.strip()}
TOPIC = "{}/land/dangausakis/alert/neutral/zone/status".format(TOPIC_ROOT)


def _epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


def active_alerts(data: dict, now: float, max_age_s: int = MAX_AGE_S, countries=frozenset()) -> dict:
    """alert id -> payload, for alerts that are recent and in the wanted countries."""
    sources = data.get("sources") or {}
    out = {}
    for alert in data.get("alerts") or []:
        alert_id = alert.get("id")
        since = _epoch(alert.get("since"))
        country = (alert.get("country") or "").upper()
        if not alert_id or since is None or now - since > max_age_s:
            continue
        if countries and country not in countries:
            continue
        source = sources.get(country) or {}
        updated = _epoch(source.get("updatedAt"))
        source_dead = (source.get("status") not in (None, "ok") or source.get("stale")
                       or updated is None or now - updated > SOURCE_MAX_AGE_S)
        out[alert_id] = {
            "_src": "dangausakis.lt",
            "_ts": now,
            "alert_type": "region_alert",
            "alert_id": alert_id,
            "country": country,
            "level": alert.get("level", ""),
            "area": alert.get("area") or alert.get("reason") or "",
            "since": alert.get("since"),
            "source": alert.get("source", ""),
            "stale": bool(source_dead),
            "notify": True,
        }
    return out


def parse_boundaries(js: str) -> dict:
    """alert id -> {"name", "rings"} from the site's `adminGeoData=<GeoJSON>` bundle."""
    marker = "adminGeoData="
    return boundaries_from_geojson(json.loads(js[js.index(marker) + len(marker):].rstrip().rstrip(";")))


def boundaries_from_geojson(data: dict) -> dict:
    """alert id -> {"name", "rings"} from a GeoJSON FeatureCollection of regions."""
    out = {}
    for feature in data["features"]:
        props = feature["properties"]
        out[props["id"]] = {"name": props.get("name") or props["id"],
                            "rings": alert_sources.rings_of(feature["geometry"])}
    return out


def _perp(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / (dx * dx + dy * dy) ** 0.5


def _douglas_peucker(points: list, eps: float) -> list:
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        far, far_d = None, eps
        for i in range(lo + 1, hi):
            d = _perp(points[i], points[lo], points[hi])
            if d > far_d:
                far, far_d = i, d
        if far is not None:
            keep[far] = True
            stack.extend([(lo, far), (far, hi)])
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring: list, max_vertices: int = ZONE_MAX_VERTICES) -> list:
    """Closed ring of at most max_vertices points, shape preserved by Douglas-Peucker."""
    points = [list(p[:2]) for p in (ring[:-1] if ring[0] == ring[-1] else ring)]
    eps, simplified = 0.0005, points
    while len(simplified) > max_vertices - 1 and eps < 5:
        simplified = _douglas_peucker(points, eps)
        eps *= 1.6
    return simplified + [simplified[0]]


def _ring_area(ring: list) -> float:
    """Shoelace area in square degrees — only used to rank and filter parts."""
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2


def zone_tracks(alerts: dict, boundaries: dict, now: float) -> dict:
    """uid -> (topic prefix, track): one Polygon track per part of each coloured alert."""
    out = {}
    for alert_id, alert in alerts.items():
        region = boundaries.get(alert_id)
        if not region or alert.get("stale") or alert.get("level") not in ZONE_COLORS:
            continue
        rings = sorted(region["rings"], key=_ring_area, reverse=True)[:ZONE_MAX_PARTS]
        # Skip islets (under 2% of the largest part): each part is a separate marker.
        rings = [r for r in rings if _ring_area(r) >= 0.02 * _ring_area(rings[0])]
        for index, ring in enumerate(rings):
            ring = simplify_ring(ring)
            uid = "DA-ZONE-{}{}".format(alert_id, "-p{}".format(index) if index else "")
            out[uid] = (ZONE_PREFIX, {
                "_src": "dangausakis.lt",
                "_ts": now,
                "uid": uid,
                "type": "air_alert_zone",
                "callsign": "{} {} {}".format(alert["country"], region["name"], alert["level"].upper()),
                "lat_deg": round(sum(p[1] for p in ring[:-1]) / (len(ring) - 1), 5),
                "lon_deg": round(sum(p[0] for p in ring[:-1]) / (len(ring) - 1), 5),
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "shape_color": alert["level"],
                "alert_id": alert_id,
                "country": alert["country"],
                "level": alert["level"],
                "remarks": "Air alert ({}) since {} - source: {}{} - not an official warning".format(
                    alert["level"], alert.get("since") or "?", alert.get("source") or "?",
                    " via dangausakis.lt" if alert.get("_src", "dangausakis.lt") == "dangausakis.lt" else ""),
            })
    return out


def _get(url: str, timeout: int = 15) -> str | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=timeout) as resp:
            body = resp.read(_MAX_BODY_BYTES + 1)
            if resp.headers.get("Content-Encoding") == "gzip":      # some hosts (112.lv) send it unasked
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(_MAX_BODY_BYTES + 1)
            return body[:_MAX_BODY_BYTES].decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print("dangausakis fetch failed {}: {}".format(url, exc), flush=True)
        return None


def _get_json(url: str):
    text = _get(url)
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError as exc:
        print("dangausakis bad JSON from {}: {}".format(url, exc), flush=True)
        return None


def load_boundaries() -> dict | None:
    """Site boundaries (LT/LV/PL/EE/UA oblasts) plus, when NEPTUN is a direct source,
    NEPTUN's own district and oblast polygons. None only if the site's boundaries fail."""
    boundaries = load_site_boundaries()
    if boundaries is not None and "neptun" in DIRECT_SOURCES:
        raions = _get_json(alert_sources.NEPTUN_RAIONS_URL)
        oblasts = _get_json(alert_sources.NEPTUN_OBLASTS_URL)
        if raions and oblasts:
            boundaries.update(alert_sources.neptun_boundaries(raions, oblasts))
    return boundaries


def load_site_boundaries() -> dict | None:
    """Boundaries from the site's /region-geometry REST route; if that fails, from the
    hashed adminGeoData bundle found via the map page."""
    try:
        with urllib.request.urlopen(urllib.request.Request(API + "region-geometry", headers=_HEADERS),
                                    timeout=30) as resp:
            return boundaries_from_geojson(json.loads(resp.read().decode("utf-8")))
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        print("dangausakis region-geometry failed ({}), trying the map bundle".format(exc), flush=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(MAP_PAGE, headers=_HEADERS), timeout=15) as resp:
            match = _BOUNDARY_JS_RE.search(resp.read().decode("utf-8", "replace"))
        if not match:
            print("dangausakis: adminGeoData bundle not found on the map page", flush=True)
            return None
        url = "https://dangausakis.lt" + match.group(0)
        with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=30) as resp:
            return parse_boundaries(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        print("dangausakis boundaries load failed: {}".format(exc), flush=True)
        return None


def aircraft_tracks(data: dict) -> list:
    """(topic prefix, track) per fresh aircraft; nothing when the feed is stale."""
    if data.get("stale"):
        return []
    out = []
    for ac in data.get("aircraft") or []:
        hexid = (ac.get("hex") or "").strip().lower()
        if not hexid or ac.get("lat") is None or ac.get("lon") is None:
            continue
        if (ac.get("seenPositionSec") or 0) > AIRCRAFT_MAX_POSITION_AGE_S:
            continue
        track = {
            "_src": "dangausakis.lt/adsb.lol",
            "_ts": (ac.get("positionTimestamp") or time.time() * 1000) / 1000.0,
            "icao24": hexid,
            "callsign": (ac.get("flight") or "").strip(),
            "registration": ac.get("registration") or "",
            "aircraft_type": ac.get("aircraftType") or "",
            "lat_deg": ac["lat"],
            "lon_deg": ac["lon"],
            "squawk": ac.get("squawk") or "",
            "target_type": "aircraft",
            "remarks": "Data: adsb.lol (ODbL-1.0) via dangausakis.lt",
        }
        if ac.get("altitudeFt") is not None:
            track["baro_alt_m"] = round(ac["altitudeFt"] * _FT_M, 1)
        if ac.get("groundSpeedKt") is not None:
            track["speed_ms"] = round(ac["groundSpeedKt"] * _KT_MS, 2)
        if ac.get("trackDeg") is not None:
            track["heading_deg"] = ac["trackDeg"]
        if ac.get("verticalRateFpm") is not None:
            track["vertical_rate_ms"] = round(ac["verticalRateFpm"] * _FT_M / 60, 2)
        modality = "mlat" if ac.get("signalType") == "mlat" else "adsb"
        out.append(("{}/air/dangausakis/{}/civ/aircraft".format(TOPIC_ROOT, modality),
                    {k: v for k, v in track.items() if v not in ("", None)}))
    return out


def _fetch(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(API + path, headers=_HEADERS), timeout=10) as resp:
            return read_json_response(resp)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print("dangausakis fetch error: {}".format(exc), flush=True)
        return None


def compose_alerts(now: float, site_data, neptun_data, lt_feed, rso_data, boundaries, lv_data=None) -> dict | None:
    """One cycle's merged alerts from whatever sources are available (a failed or disabled
    source is None). Direct sources replace the site's data for UA and LT, are added to it
    for PL and LV; a country whose direct source is None falls back to the site's data. Returns
    None when nothing at all is available, so the caller keeps the current picture."""
    direct = {
        "UA": alert_sources.neptun_alerts(neptun_data, now, SOURCE_MAX_AGE_S) if neptun_data else None,
        "LT": (alert_sources.expand_nationwide(alert_sources.lt72_alert(lt_feed, now, MAX_AGE_S),
                                               boundaries or {}, "LT") if lt_feed else None),
        "PL": alert_sources.rso_alerts(rso_data, now, MAX_AGE_S) if rso_data else None,
        "LV": (alert_sources.expand_nationwide(alert_sources.lv_alerts(lv_data, now), boundaries or {}, "LV")
               if lv_data is not None else None),
    }
    if site_data is None and all(v is None for v in direct.values()):
        return None
    site = active_alerts(site_data, now, MAX_AGE_S, COUNTRIES) if site_data else {}
    current = alert_sources.merge(site, direct, additive=frozenset({"PL", "LV"}))
    if COUNTRIES:
        current = {k: v for k, v in current.items() if v["country"] in COUNTRIES}
    return current


def publish_community(session, put_event, now: float, state: dict, verbose: bool) -> None:
    """Drone reports and incident markers as tracks, a JSON tombstone for each one that
    left the feed, and one event per report/incident that is new since the last poll
    (none on the first poll, so a restart does not re-announce what is already shown).
    A failed fetch keeps that kind's previous entries instead of retracting them.
    state: {"tracks": {uid: (kind, prefix, track)}, "seen": set | None}."""
    fetched = (("report", COMMUNITY_REPORT_PREFIX, _fetch("drone-reports"), alert_sources.drone_report_tracks),
               ("incident", COMMUNITY_INCIDENT_PREFIX, _fetch("incident-markers"),
                alert_sources.incident_marker_tracks))
    current = {}
    for kind, prefix, data, build in fetched:
        if data is None:
            current.update({u: v for u, v in state["tracks"].items() if v[0] == kind})
            continue
        for uid, track in build(data, now).items():
            current[uid] = (kind, prefix, track)
    for uid, (kind, prefix, track) in current.items():
        publish_dual(session, prefix, track, NormalizedTrack)
        if state["seen"] is not None and uid not in state["seen"]:
            event = {"_src": "dangausakis.lt", "_ts": now, "lat": track["lat_deg"], "lon": track["lon_deg"]}
            if kind == "report":
                event.update(alert_type="drone_report", report_type=track["report_type"], uid=uid)
            else:
                event.update(alert_type="incident", title=track["title"], url=track["url"], uid=uid)
            put_event(event)
        if verbose:
            print("COMMUNITY", kind, uid, track["callsign"], flush=True)
    for uid in set(state["tracks"]) - set(current):
        _, prefix, track = state["tracks"][uid]
        session.put(add_version(semantic_topic(prefix, track)),
                    json.dumps({"uid": uid, "_delete": True, "_ts": now, "_src": "dangausakis.lt"}).encode())
    state["tracks"] = current
    if all(data is not None for _, _, data, _ in fetched):
        state["seen"] = set(current)


def main():
    ap = argparse.ArgumentParser(description="dangausakis.lt alerts, alert zones, aircraft + NEPTUN threats → Zenoh bridge")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("dangausakis Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    pub = session.declare_publisher(TOPIC)
    print("dangausakis bridge starting → {} (poll {}s, max age {}s, countries {})".format(
        TOPIC, POLL_S, MAX_AGE_S, ",".join(sorted(COUNTRIES)) or "all"), flush=True)

    suppression, retracted = neptun.Suppression(), set()
    suppress_sub = subscribe(session, neptun.SUPPRESS_TOPIC, suppression.on_sample) if NEPTUN_ENABLED else None
    next_threats = 0.0
    site_cache, neptun_cache, lt_cache, rso_cache, lv_cache = (
        alert_sources.SourceCache(SOURCE_MAX_AGE_S) for _ in range(5))
    known: dict = {}
    boundaries, next_boundaries = None, 0.0
    known_zones: dict = {}
    next_alerts = next_aircraft = next_community = 0.0
    health = alert_sources.SourceHealth()
    previous_alerts = None                    # None until the first cycle: nothing to diff against
    community_state = {"tracks": {}, "seen": None}
    enabled_sources = {"dangausakis.lt"} | {label for key, label in _DIRECT_LABELS if key in DIRECT_SOURCES}

    def put_event(payload: dict) -> None:
        if EVENTS_ENABLED:
            pub.put(json.dumps(payload).encode(),
                    encoding=zenoh.Encoding.APPLICATION_JSON.with_schema("efdi:dangausakis_alert_event"))

    try:
        while True:
            if time.time() >= next_aircraft:
                next_aircraft = time.time() + AIRCRAFT_POLL_S
                data = _fetch("aircraft")
                for prefix, track in aircraft_tracks(data or {}):
                    publish_dual(session, prefix, track, NormalizedTrack)
                    if args.verbose:
                        print("AIRCRAFT", track["icao24"], track.get("callsign", ""), flush=True)
            if COMMUNITY_ENABLED and time.time() >= next_community:
                next_community = time.time() + COMMUNITY_POLL_S
                publish_community(session, put_event, time.time(), community_state, args.verbose)
            if NEPTUN_ENABLED and time.time() >= next_threats:
                next_threats = time.time() + neptun.POLL_S
                neptun.publish_threats(session, neptun.threat_tracks(neptun.fetch_threats() or {}),
                                       suppression, retracted, args.verbose)
            if time.time() < next_alerts:
                time.sleep(1)
                continue
            next_alerts = time.time() + POLL_S
            now = time.time()
            if ZONES_ENABLED and time.time() >= next_boundaries:
                loaded = load_boundaries()
                boundaries = loaded or boundaries
                next_boundaries = time.time() + (BOUNDARY_REFRESH_S if loaded else BOUNDARY_RETRY_S)

            raw = {"dangausakis.lt": _fetch("region-alerts"),
                   "NEPTUN": _get_json(alert_sources.NEPTUN_ALERTS_URL) if "neptun" in DIRECT_SOURCES else None,
                   "LT72": _get(alert_sources.LT72_FEED_URL) if "lt72" in DIRECT_SOURCES else None,
                   "RSO": _get_json(alert_sources.RSO_URL) if "rso" in DIRECT_SOURCES else None,
                   "112.lv": _get_json(alert_sources.LV_URL) if "lv" in DIRECT_SOURCES else None}
            for label, (ok, detail) in alert_sources.source_observations(
                    now, raw, enabled_sources, SOURCE_MAX_AGE_S).items():
                warning = health.observe(label, ok, detail, now)
                if warning:
                    put_event(warning)
                    print("SOURCE", label, warning["state"], detail, flush=True)
            site_data = site_cache.update(raw["dangausakis.lt"], now)
            neptun_data = neptun_cache.update(raw["NEPTUN"], now) if "neptun" in DIRECT_SOURCES else None
            lt_feed = lt_cache.update(raw["LT72"], now) if "lt72" in DIRECT_SOURCES else None
            rso_data = rso_cache.update(raw["RSO"], now) if "rso" in DIRECT_SOURCES else None
            lv_data = lv_cache.update(raw["112.lv"], now) if "lv" in DIRECT_SOURCES else None
            current = compose_alerts(now, site_data, neptun_data, lt_feed, rso_data, boundaries, lv_data)
            if current is None:
                continue                      # every source down: keep what is on screen

            if previous_alerts is not None:
                for country, change in alert_sources.status_changes(previous_alerts, current).items():
                    put_event({"_src": "dangausakis.lt", "_ts": now, "alert_type": "region_status_change",
                               "country": country, **change})
            previous_alerts = current

            popups = {k: v for k, v in current.items() if v.get("notify", True)}
            gone = {i: dict(p, _delete=True, _ts=now) for i, p in known.items() if i not in popups}
            for payload in list(popups.values()) + list(gone.values()):
                pub.put(json.dumps(payload).encode(),
                        encoding=zenoh.Encoding.APPLICATION_JSON.with_schema("efdi:dangausakis_region_alert"))
                if args.verbose:
                    print("ALERT", payload["alert_id"], payload["level"],
                          "(cleared)" if payload.get("_delete") else "", flush=True)
            known = popups

            if ZONES_ENABLED:
                zones = zone_tracks(current, boundaries or {}, now)
                for uid, (prefix, track) in zones.items():
                    session.put(add_version(semantic_topic(prefix, track)), json.dumps(track).encode(),
                                encoding=zenoh.Encoding.APPLICATION_JSON.with_schema("efdi:dangausakis_air_zone"))
                for uid in set(known_zones) - set(zones):
                    prefix, track = known_zones[uid]
                    session.put(add_version(semantic_topic(prefix, track)),
                                json.dumps({"uid": uid, "_delete": True, "_ts": now,
                                            "_src": "dangausakis.lt"}).encode())
                known_zones = zones
    except KeyboardInterrupt:
        pass
    finally:
        if suppress_sub is not None:
            suppress_sub.undeclare()
        pub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
