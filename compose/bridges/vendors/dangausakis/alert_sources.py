#!/usr/bin/env python3
"""Direct alert sources, hidden under the dangausakis bridge (one service).

dangausakis.lt's own /region-alerts aggregates LT, LV, PL, EE and UA, but it can lag
badly (on 2026-10-05 it was ~34 h behind NEPTUN's live data while claiming to be
fresh). Where an upstream has a real machine-readable feed, the bridge reads it
directly and lets it replace the site's data for that country:

  UA  NEPTUN /api/v1/alerts      live district (raion) and oblast alerts, with
                                 NEPTUN's own boundary files (raions/oblasts.geojson).
                                 Free, read-only, keyless; poll >= 5 s; credit NEPTUN.
  LT  LT72 warnings RSS          the Armed Forces' air-alert messages ("TIKĖTINAS ORO
                                 PAVOJUS (GELTONA)" ... "ORO PAVOJAUS NĖRA (BALTA)");
                                 nationwide, so one popup plus every county coloured.
  PL  RSO notices (komunikaty.tvp.pl, public JSON)   regional notices, nearly all air
                                 quality / river levels / siren tests. Only notices that
                                 name an air-raid alarm are used, and ADDITIVELY: they add
                                 to the site's Polish alerts and never replace them,
                                 because no real air-raid notice was available to check
                                 the matching against.

LV: 112.lv is a shelter map (an ArcGIS layer of shelters) and VUGD's page is informational,
so there is no machine-readable alert source; LV and EE stay on the site's feed.

Pure functions only; all network I/O lives in dangausakis_bridge.py.
"""

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

NEPTUN_ALERTS_URL = "https://neptun.in.ua/api/v1/alerts"
NEPTUN_RAIONS_URL = "https://neptun.in.ua/raions.geojson"
NEPTUN_OBLASTS_URL = "https://neptun.in.ua/oblasts.geojson"
LT72_FEED_URL = "https://lt72.lt/kategorija/pranesimai/feed/"
RSO_URL = "https://komunikaty.tvp.pl/komunikatyxml/wszystkie/wszystkie/0?_format=json"
LT_NATIONWIDE_ID = "LT:ALL"
LEVELS = {"red", "orange", "yellow"}

# Strict phrases only; tests/drills (e.g. "TESTY SYREN ALARMOWYCH") must never alert.
_PL_AIR = re.compile(r"alarm lotnicz|alarm powietrzn|alarm przeciwlotnicz|"
                     r"zagro[żz]eni[ae] (?:atakiem )?(?:z )?powietrz|atak (?:z )?powietrzn", re.IGNORECASE)
_PL_NOT_AN_ALERT = re.compile(r"test|[ćc]wicz|pr[óo]b[ay]|sprawdzen", re.IGNORECASE)
_WARSAW = ZoneInfo("Europe/Warsaw")

# Colour word in the message -> level. Checked in this order; "BALTA" (white) means cleared.
_LT_COLOURS = (("RAUDON", "red"), ("ORANŽ", "orange"), ("ORANZ", "orange"), ("GELTON", "yellow"))


def epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


def rings_of(geometry: dict) -> list:
    """Outer rings of a Polygon / MultiPolygon."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [p[0] for p in polygons if p and p[0]]


def _alert(alert_id, country, level, area, since, source, now, notify, stale=False, reason="",
           origin="dangausakis.lt"):
    return {
        "_src": origin, "_ts": now, "alert_type": "region_alert", "alert_id": alert_id,
        "country": country, "level": level, "area": area, "since": since, "source": source,
        "stale": bool(stale), "notify": notify, **({"reason": reason} if reason else {}),
    }


def neptun_alerts(data: dict, now: float, max_age_s: float, source_max_age_s: float) -> dict:
    """alert id -> alert for NEPTUN's district ("UA-R:<key>") and oblast ("UA-O:<key>")
    alerts. Standing occupied-region alerts (since 2022) fall to the age filter. No
    popups (notify False): dozens of districts can be active at once; zones only."""
    updated = epoch(data.get("updatedAt"))
    stale = updated is None or now - updated > source_max_age_s
    out = {}
    for kind, prefix in (("raions", "UA-R"), ("oblasts", "UA-O")):
        for entry in data.get(kind) or []:
            key, since = entry.get("key"), epoch(entry.get("since"))
            if not key or since is None or now - since > max_age_s or entry.get("level") not in LEVELS:
                continue
            alert_id = "{}:{}".format(prefix, key)
            out[alert_id] = _alert(alert_id, "UA", entry["level"], entry.get("name") or key, entry["since"],
                                   "NEPTUN", now, False, stale, "; ".join(entry.get("reasons") or []),
                                   origin="neptun.in.ua")
    return out


def neptun_boundaries(raions: dict, oblasts: dict) -> dict:
    """Boundaries keyed like neptun_alerts() ids, from NEPTUN's own GeoJSON files."""
    out = {}
    for data, prefix, name_prop in ((raions, "UA-R", "rayon"), (oblasts, "UA-O", "region")):
        for feature in data.get("features", []):
            props = feature["properties"]
            out["{}:{}".format(prefix, props["key"])] = {
                "name": props.get(name_prop) or props["key"], "rings": rings_of(feature["geometry"])}
    return out


def lt72_alert(feed_xml: str, now: float, max_age_s: float) -> dict:
    """{"LT:ALL": alert} while the NEWEST air-alert message in the LT72 warnings feed is
    a coloured one, else {} (white message = cleared, or none recent enough).

    The messages are nationwide. An unrecognised colour is shown as orange rather than
    dropped: a missed alert is worse than a mislabelled level."""
    for item in re.findall(r"<item>(.*?)</item>", feed_xml, flags=re.S):
        text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<!\[CDATA\[|\]\]>", "", item)))
        text = re.sub(r"\s+", " ", text).upper()
        if "ORO PAVOJ" not in text:       # system tests and other notices are not alerts
            continue
        published = re.search(r"<pubDate>(.*?)</pubDate>", item)
        try:
            since = parsedate_to_datetime(published.group(1)).timestamp()
        except (AttributeError, TypeError, ValueError):
            return {}
        if "BALTA" in text or "ATŠAUK" in text or now - since > max_age_s:
            return {}
        level = next((lvl for word, lvl in _LT_COLOURS if word in text), "orange")
        iso = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {LT_NATIONWIDE_ID: _alert(LT_NATIONWIDE_ID, "LT", level, "Lietuva", iso, "LT72", now, True,
                                         origin="lt72.lt")}
    return {}


def _warsaw_epoch(stamp):
    """RSO timestamps are naive local (Europe/Warsaw) 'YYYY-MM-DD HH:MM:SS'."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_WARSAW).timestamp()
    except (TypeError, ValueError):
        return None


def rso_alerts(data: dict, now: float, max_age_s: float) -> dict:
    """alert id ("PL:<province slug>", matching the site's boundaries) -> alert, for RSO
    notices that name an air-raid alarm, are still valid and recent, and are not a test.
    "alarm" wording is red, "zagrożenie" (threat) wording orange."""
    out = {}
    for notice in data.get("newses") or []:
        text = "{} {}".format(notice.get("title") or "", notice.get("shortcut") or "")
        if not _PL_AIR.search(text) or _PL_NOT_AN_ALERT.search(text):
            continue
        since = _warsaw_epoch(notice.get("valid_from") or notice.get("created_at"))
        valid_to = _warsaw_epoch(notice.get("valid_to"))
        if since is None or now - since > max_age_s or (valid_to is not None and valid_to < now):
            continue
        level = "red" if re.search(r"alarm", text, re.IGNORECASE) else "orange"
        iso = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        provinces = notice.get("provinces") or {}
        for province in (provinces.values() if isinstance(provinces, dict) else provinces):
            slug = province.get("slug_name")
            if slug:
                alert_id = "PL:{}".format(slug)
                out[alert_id] = _alert(alert_id, "PL", level, province.get("name") or slug, iso, "RSO", now,
                                       True, reason=(notice.get("title") or "")[:200], origin="komunikaty.tvp.pl")
    return out


def expand_nationwide(alerts: dict, boundaries: dict, country: str) -> dict:
    """Add one zone-only entry per region of `country` for each nationwide alert."""
    out = dict(alerts)
    for alert in alerts.values():
        if alert["country"] != country:
            continue
        for region_id, region in boundaries.items():
            if region_id.startswith(country + ":") and region_id != alert["alert_id"]:
                out[region_id] = dict(alert, alert_id=region_id, area=region["name"], notify=False)
    return out


def merge(site_alerts: dict, direct: dict, additive=frozenset()) -> dict:
    """`direct` is {country: alerts or None}. A healthy direct source (not None, even if
    empty) is authoritative for its country and replaces the site's alerts for it —
    except countries in `additive`, whose direct alerts are only added to the site's."""
    out = {k: v for k, v in site_alerts.items()
           if direct.get(v["country"]) is None or v["country"] in additive}
    for alerts in direct.values():
        out.update(alerts or {})
    return out


class SourceCache:
    """Last good value per source, so one failed poll does not retract every alert."""

    def __init__(self, max_age_s: float):
        self._max_age = max_age_s
        self._value, self._at = None, 0.0

    def update(self, value, now: float):
        """Store a fresh value (ignored when None) and return the best available one."""
        if value is not None:
            self._value, self._at = value, now
        return self._value if now - self._at <= self._max_age else None
