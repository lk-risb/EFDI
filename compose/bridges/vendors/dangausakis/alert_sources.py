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

LV  112.lv/lv/api/alerts (the official alerts JSON the site lists for Latvia; {"alerts": []}
                                 when quiet). Its item schema is unverified, so only items that
                                 name an air raid are used, ADDITIVELY like PL, nationwide.
EE: no machine-readable source exists (the site itself lists it as "not connected").

Pure functions only; all network I/O lives in dangausakis_bridge.py.
"""

import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

NEPTUN_ALERTS_URL = "https://neptun.in.ua/api/v1/alerts"
NEPTUN_RAIONS_URL = "https://neptun.in.ua/raions.geojson"
NEPTUN_OBLASTS_URL = "https://neptun.in.ua/oblasts.geojson"
LT72_FEED_URL = "https://lt72.lt/kategorija/pranesimai/feed/"
LV_URL = "https://www.112.lv/lv/api/alerts"
RSO_URL = "https://komunikaty.tvp.pl/komunikatyxml/wszystkie/wszystkie/0?_format=json"
LT_NATIONWIDE_ID = "LT:ALL"
LV_NATIONWIDE_ID = "LV:ALL"
LEVELS = {"red", "orange", "yellow"}

# Strict phrases only; tests/drills (e.g. "TESTY SYREN ALARMOWYCH") must never alert.
_PL_AIR = re.compile(r"alarm lotnicz|alarm powietrzn|alarm przeciwlotnicz|"
                     r"zagro[żz]eni[ae] (?:atakiem )?(?:z )?powietrz|atak (?:z )?powietrzn", re.IGNORECASE)
_PL_NOT_AN_ALERT = re.compile(r"test|[ćc]wicz|pr[óo]b[ay]|sprawdzen", re.IGNORECASE)
# Latvian (112.lv) alert text naming an air raid; the feed's item schema is unverified
# (it is {"alerts": []} when quiet), so only items that say so are used.
_LV_AIR = re.compile(r"gaisa (?:trauksme|uzbrukum|apdraud)|gaisa uzbruk|air[ -]raid|air alert|airstrike|"
                     r"воздушн\w+ тревог", re.IGNORECASE)
_WARSAW = ZoneInfo("Europe/Warsaw")

# Colour word in the message -> level. Checked in this order; "BALTA" (white) means cleared.
_LT_COLOURS = (("RAUDON", "red"), ("ORANŽ", "orange"), ("ORANZ", "orange"), ("GELTON", "yellow"))


# Ukrainian -> English for NEPTUN's names and reasons. Names are romanised with the
# official Ukrainian national system (Cabinet resolution 55/2010; "Київ" -> "Kyiv");
# the common words are translated. NEPTUN publishes Ukrainian only.
_UA_WORDS = (
    ("Автономна Республіка Крим", "Autonomous Republic of Crimea"),
    ("Ракетна загроза", "Missile threat"), ("Дронова загроза", "Drone threat"),
    ("Балістична загроза", "Ballistic threat"), ("Авіаційна загроза", "Aircraft threat"),
    ("червоний рівень", "red level"), ("помаранчевий рівень", "orange level"),
    ("жовтий рівень", "yellow level"), ("рівень", "level"),
    ("район", "District"), ("область", "Oblast"), ("м. ", "City of "),
)
_UA_INITIAL = {"є": "Ye", "ї": "Yi", "й": "Y", "ю": "Yu", "я": "Ya"}
_UA_INNER = {"є": "ie", "ї": "i", "й": "i", "ю": "iu", "я": "ia"}
_UA_LETTERS = dict(zip("абвгґдезиіклмнопрстуфхцчшщ", [
    "a", "b", "v", "h", "g", "d", "e", "z", "y", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
    "f", "kh", "ts", "ch", "sh", "shch"]))
_UA_LETTERS.update({"ж": "zh", "ь": "", "'": "", "’": "", "ʼ": ""})


def english(text) -> str:
    """Ukrainian text -> English: known words translated, the rest romanised. Text that
    is already Latin (or empty) comes back unchanged."""
    text = str(text or "")
    for ua, en in _UA_WORDS:
        text = re.sub(re.escape(ua), "\0" + en + "\0", text, flags=re.IGNORECASE)
    out, initial = [], True
    for part in text.split("\0"):
        if part and any(en == part for _, en in _UA_WORDS):
            out.append(part)
            initial = not part[-1].isalnum()
            continue
        for ch in part:
            low = ch.lower()
            if low in _UA_INITIAL:
                rep = (_UA_INITIAL if initial else _UA_INNER)[low]
            elif low in _UA_LETTERS:
                rep = _UA_LETTERS[low]
            else:
                out.append(ch)
                initial = not ch.isalnum()
                continue
            out.append(rep.capitalize() if initial and ch != low else rep)
            initial = False
    return "".join(out)


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


def neptun_alerts(data: dict, now: float, source_max_age_s: float) -> dict:
    """alert id -> alert for NEPTUN's district ("UA-R:<key>") and oblast ("UA-O:<key>")
    alerts. Everything NEPTUN lists is active, however old its `since` (a district can
    stay red for a day, occupied regions since 2022), so there is no age cut-off; a
    source whose own updatedAt is old is flagged stale instead. No popups (notify
    False): dozens of districts can be active at once; zones only."""
    updated = epoch(data.get("updatedAt"))
    stale = updated is None or now - updated > source_max_age_s
    out = {}
    for kind, prefix in (("raions", "UA-R"), ("oblasts", "UA-O")):
        for entry in data.get(kind) or []:
            key, since = entry.get("key"), epoch(entry.get("since"))
            if not key or since is None or entry.get("level") not in LEVELS:
                continue
            alert_id = "{}:{}".format(prefix, key)
            out[alert_id] = _alert(alert_id, "UA", entry["level"], english(entry.get("name") or key), entry["since"],
                                   "NEPTUN", now, False, stale, english("; ".join(entry.get("reasons") or [])),
                                   origin="neptun.in.ua")
    return out


def neptun_boundaries(raions: dict, oblasts: dict) -> dict:
    """Boundaries keyed like neptun_alerts() ids, from NEPTUN's own GeoJSON files."""
    out = {}
    for data, prefix, name_prop in ((raions, "UA-R", "rayon"), (oblasts, "UA-O", "region")):
        for feature in data.get("features", []):
            props = feature["properties"]
            out["{}:{}".format(prefix, props["key"])] = {
                "name": english(props.get(name_prop) or props["key"]), "rings": rings_of(feature["geometry"])}
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


def lv_alerts(data: dict, now: float) -> dict:
    """{"LV:ALL": alert} when the 112.lv alerts feed carries an item that names an air
    raid, else {}. Nationwide (red), like LT72; the caller expands it to every region."""
    items = data.get("alerts") if isinstance(data, dict) else None
    for item in items or []:
        if _LV_AIR.search(json.dumps(item, ensure_ascii=False)):
            iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            title = next((str(item[k])[:200] for k in ("title", "name", "message", "text") if item.get(k)), "")
            return {LV_NATIONWIDE_ID: _alert(LV_NATIONWIDE_ID, "LV", "red", "Latvija", iso, "112.lv", now, True,
                                             reason=title, origin="112.lv")}
    return {}


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


# ── dangausakis.lt community data: drone reports and incident markers ──────────────
_ID_32 = re.compile(r"^[a-f0-9]{32}$")
_ID_SLUG = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
_SITE_HOSTS = ("https://dangausakis.lt/", "https://www.dangausakis.lt/")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _point(item: dict):
    try:
        lat, lon = float(item.get("latitude")), float(item.get("longitude"))
    except (TypeError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def drone_report_tracks(data, now: float) -> dict:
    """track uid -> track for the site's community drone reports (Lithuania, unverified,
    about 20 minutes' lifetime, position approximate). Expired or malformed reports are
    skipped, with the same checks the site's own map applies."""
    if not isinstance(data, dict):
        return {}
    server = epoch(data.get("serverTime") or data.get("server_time") or data.get("updatedAt")) or now
    out = {}
    for report in data.get("reports") or []:
        if not isinstance(report, dict):
            continue
        rid, point, expires = str(report.get("id") or ""), _point(report), epoch(report.get("expiresAt"))
        if not _ID_32.match(rid) or point is None or expires is None or expires <= server:
            continue
        kind = str(report.get("reportType") or "report")[:20]
        uid = "DA-REPORT-" + rid[:16]
        out[uid] = {
            "_src": "dangausakis.lt", "_ts": now, "uid": uid, "lat_deg": point[0], "lon_deg": point[1],
            "callsign": "LT drone report ({})".format(kind), "target_type": "uav", "report_type": kind,
            "sapient_class": "uav",       # both layers key the UAV symbol on this field
            "position_uncertainty_m": 1000, "expires": _iso(expires),
            "remarks": "Community report ({}), unverified - via dangausakis.lt - expires {}".format(kind, _iso(expires)),
        }
    return out


def incident_marker_tracks(data, now: float) -> dict:
    """track uid -> track for the site's incident markers (a news article pinned to a
    place, valid 24 h). Same validation as the site's map: slug id, title <= 160 chars,
    https link on dangausakis.lt, not expired."""
    if not isinstance(data, dict):
        return {}
    server = epoch(data.get("server_time") or data.get("serverTime")) or now
    out = {}
    for incident in data.get("incidents") or []:
        if not isinstance(incident, dict):
            continue
        iid, title = str(incident.get("id") or ""), str(incident.get("title") or "").strip()
        url, point, expires = str(incident.get("url") or ""), _point(incident), epoch(incident.get("expires_at"))
        if (not _ID_SLUG.match(iid) or not title or len(title) > 160 or point is None or expires is None
                or expires <= server or not url.startswith(_SITE_HOSTS)):
            continue
        approximate = incident.get("is_approximate") is True
        uid = "DA-INCIDENT-" + iid[:100]
        out[uid] = {
            "_src": "dangausakis.lt", "_ts": now, "uid": uid, "lat_deg": point[0], "lon_deg": point[1],
            "callsign": title, "target_type": "incident", "url": url, "title": title,
            **({"position_uncertainty_m": 2000} if approximate else {}),
            "remarks": "Incident report via dangausakis.lt: {} - {}{}".format(
                title, url, " - approximate location" if approximate else ""),
        }
    return out


# ── status-change and data-issue events (for tak_alert_layer GeoChat) ───────────────
def status_changes(previous: dict, current: dict) -> dict:
    """{country: {"raised": [...], "changed": [...], "cleared": [...]}} between two alert
    sets ({alert_id: alert}); countries without any change are left out. Entries are
    {"area", "level"} (raised/cleared) or {"area", "from", "to"} (changed)."""
    out = {}

    def bucket(country):
        return out.setdefault(country, {"raised": [], "changed": [], "cleared": []})

    for alert_id, alert in current.items():
        old = previous.get(alert_id)
        if old is None:
            bucket(alert["country"])["raised"].append({"area": alert["area"], "level": alert["level"]})
        elif old["level"] != alert["level"]:
            bucket(alert["country"])["changed"].append(
                {"area": alert["area"], "from": old["level"], "to": alert["level"]})
    for alert_id, old in previous.items():
        if alert_id not in current:
            bucket(old["country"])["cleared"].append({"area": old["area"], "level": old["level"]})
    return out


def source_observations(now: float, raw: dict, enabled, max_age_s: float) -> dict:
    """{source label: (ok, detail)} for every enabled source. `raw` maps the label to
    what the fetch returned (None = failed). dangausakis.lt and NEPTUN also report their
    own updatedAt; one older than max_age_s counts as not ok (a frozen feed)."""
    out = {}
    for label, value in raw.items():
        if label not in enabled:
            continue
        if value is None:
            out[label] = (False, "unreachable")
            continue
        updated = epoch(value.get("updatedAt")) if isinstance(value, dict) else None
        if label in ("dangausakis.lt", "NEPTUN") and (updated is None or now - updated > max_age_s):
            hours = "unknown time" if updated is None else "{:.0f} h".format((now - updated) / 3600)
            out[label] = (False, "not updated for {}".format(hours))
        else:
            out[label] = (True, "")
    return out


class SourceHealth:
    """Turns repeated source failures into one warning, and the recovery into one more,
    so a flapping or permanently dead feed does not message every poll."""

    def __init__(self, fail_cycles: int = 3):
        self._need = fail_cycles
        self._fails, self._warned = {}, set()

    def observe(self, source: str, ok: bool, detail: str, now: float):
        """A source_warning payload when the state changes, else None."""
        if ok:
            self._fails[source] = 0
            if source in self._warned:
                self._warned.discard(source)
                return _warning(source, "recovered", "", now)
            return None
        self._fails[source] = self._fails.get(source, 0) + 1
        if self._fails[source] >= self._need and source not in self._warned:
            self._warned.add(source)
            return _warning(source, "down", detail, now)
        return None


def _warning(source: str, state: str, detail: str, now: float) -> dict:
    return {"_src": "dangausakis.lt", "_ts": now, "alert_type": "source_warning", "source": source,
            "state": state, **({"detail": detail} if detail else {})}
