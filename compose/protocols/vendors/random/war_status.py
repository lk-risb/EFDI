"""Which countries are at war, from the open data the pod already receives.

A country at war is drawn orange in all its regions (white at peace), and its threats and alerts are drawn on top:
yellow for level 2, red for level 3. The status is not configured; it follows the data, so a NATO country that is
attacked turns orange by itself:
  - the site draws the country in a standing warning or danger tone (Russia, Belarus today);
  - the country has red alerts in at least RED_REGIONS regions at once (a war, not one stray balloon or drone);
  - Drone Strike Map records strikes on it (it announces them on <ORG>/land/dronestrikemap/status/war);
  - Russian units are inside it (DeepState's hostile unit markers, see Presence): Moldova, with the Russian forces in
    Transnistria, is the case; the region they are in is drawn yellow at least.
A country stays at war for HOLD_S after the last sign of it: a quiet hour is not peace. The state is kept in a file
so a restart does not turn Ukraine white until the next alert.
"""

import json
import os
import threading
import time

HOLD_S = 24 * 3600
RED_REGIONS = 3


def war_topic(topic_root: str) -> str:
    return "{}/land/dronestrikemap/status/war".format(topic_root)


class WarStatus:
    def __init__(self, hold_s: float = HOLD_S, path: str | None = None):
        self._hold = hold_s
        self._path = path
        self._until: dict = {}          # country code -> time its status runs out
        self._lock = threading.Lock()
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    self._until = {str(k).upper(): float(v) for k, v in json.load(handle).items()}
            except (OSError, ValueError, AttributeError):
                self._until = {}

    def note(self, country, now: float) -> None:
        code = str(country or "").strip().upper()
        if not code:
            return
        with self._lock:
            changed = code not in self._until or self._until[code] < now
            self._until[code] = now + self._hold
        if changed:
            self._save()

    def observe_standing(self, standing: dict, now: float) -> None:
        """The countries the site draws in a standing warning or danger tone."""
        for code in standing:
            self.note(code, now)

    def observe_alerts(self, alerts: dict, now: float, min_regions: int = RED_REGIONS) -> None:
        """Red alerts that are not stale in at least `min_regions` regions of one country at once: it is under attack.
        One red alert is not enough, a stray balloon or drone would turn a NATO country orange for a day."""
        red: dict = {}
        for alert_id, alert in alerts.items():
            if alert.get("level") == "red" and not alert.get("stale"):
                red.setdefault(alert.get("country"), set()).add(alert_id)
        for country, ids in red.items():
            if len(ids) >= min_regions:
                self.note(country, now)

    def on_sample(self, sample) -> None:
        """Zenoh callback for the announcement {"countries": ["RU", "UA"]} from dronestrikemap_bridge.py."""
        try:
            countries = json.loads(bytes(sample.payload).decode()).get("countries") or []
        except (ValueError, UnicodeDecodeError, AttributeError):
            return
        now = time.time()
        for code in countries if isinstance(countries, list) else []:
            self.note(code, now)

    def countries(self, now: float) -> frozenset:
        with self._lock:
            return frozenset(code for code, until in self._until.items() if until > now)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with self._lock:
                data = dict(self._until)
            with open(self._path + ".tmp", "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(self._path + ".tmp", self._path)
        except OSError as exc:
            print("war status not saved: {}".format(exc), flush=True)


class Presence:
    """Where the hostile units DeepState draws are now: on_sample() for each unit track, inside(regions) for the ids
    of the regions that hold at least one."""

    def __init__(self, ttl_s: float = 1800.0):
        self._ttl = ttl_s
        self._seen: dict = {}           # uid -> (time, lon, lat)
        self._lock = threading.Lock()

    def on_sample(self, sample) -> None:
        try:
            track = json.loads(bytes(sample.payload).decode())
            uid, lat, lon = track.get("uid"), float(track["lat_deg"]), float(track["lon_deg"])
        except (ValueError, UnicodeDecodeError, AttributeError, KeyError, TypeError):
            return
        with self._lock:
            if track.get("_delete"):
                self._seen.pop(uid, None)
            elif uid:
                self._seen[uid] = (time.time(), lon, lat)

    def inside(self, regions: list, contains, now: float | None = None) -> set:
        """ids of the regions (dicts with "id" and "rings") with a unit in them; `contains(region, lon, lat)`."""
        now = time.time() if now is None else now
        with self._lock:
            points = [(lon, lat) for seen, lon, lat in self._seen.values() if now - seen <= self._ttl]
        return {region["id"] for region in regions if any(contains(region, lon, lat) for lon, lat in points)}
