"""Fusion-level duplicate suppression: one aircraft, one marker, whatever feeds it.

An aircraft can reach the fabric twice with different identities: once keyed by its ICAO address
(`icao24`, drawn as an aircraft) and once as a unit whose id or callsign merely looks like that
address ("48A5C6" with no `icao24`, drawn as an unknown ground unit beside it). Fixing that in each
decoder only covers the feeds somebody has seen do it, and a decoder restarted cold has not yet seen the
keyed copy. Track fusion sees every source, so it looks for the pair and announces the loose copy on
<ORG>/air/trackfusion/suppress/twin/<uid>; the output layers (tak_layer, sitaware_layer) hold back a
track that has no icao24 and whose uid is announced, and retract it if it was already drawn.

A loose track is a twin when a fresh keyed track exists for the address read from its uid or callsign, or
carries the same callsign within TWIN_NEAR_KM. Nothing keyed by an address is ever suppressed.
"""

import json
import math
import re
import threading
import time

TWIN_TTL_S = 20.0            # a layer keeps holding a loose copy back this long after the last announcement
TWIN_FRESH_S = 120.0         # a keyed or loose track counts for this long after it was last seen
TWIN_NEAR_KM = 10.0
_HEX = re.compile(r"^(?:[A-Za-z]+[:_-])?([0-9A-Fa-f]{6})$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def twin_topic(topic_root: str, uid: str) -> str:
    return "{}/air/trackfusion/suppress/twin/{}".format(topic_root, _UNSAFE.sub("_", str(uid)))


def _address(text) -> str | None:
    match = _HEX.match(str(text or "").strip())
    return match.group(1).lower() if match else None


def _callsign(track: dict) -> str:
    return str(track.get("callsign") or "").strip().upper()


def _km(lat1, lon1, lat2, lon2) -> float:
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


class TwinIndex:
    """Fusion side: feed every track to observe(); it returns the uids of loose copies to announce."""

    def __init__(self):
        self._keyed: dict = {}      # icao24 -> (seen, lat, lon, callsign)
        self._by_call: dict = {}    # callsign -> (seen, lat, lon, icao24)
        self._loose: dict = {}      # uid -> (seen, address read from uid/callsign, lat, lon, callsign)
        self._lock = threading.Lock()

    def observe(self, track: dict, now: float | None = None) -> list:
        now = time.time() if now is None else now
        lat, lon, uid = track.get("lat_deg"), track.get("lon_deg"), track.get("uid")
        if (track.get("_delete") or lat is None or lon is None or not uid or track.get("geometry")
                or str(uid).endswith("-TRAIL")):
            return []
        callsign = _callsign(track)
        icao = str(track.get("icao24") or "").strip().lower()
        with self._lock:
            if icao:
                self._keyed[icao] = (now, lat, lon, callsign)
                if len(callsign) >= 4:
                    self._by_call[callsign] = (now, lat, lon, icao)
                return [u for u, entry in list(self._loose.items()) if self._is_twin(entry, now)]
            self._loose[str(uid)] = (now, _address(uid) or _address(track.get("callsign")), lat, lon, callsign)
            self._forget(now)
            return [str(uid)] if self._is_twin(self._loose[str(uid)], now) else []

    def _is_twin(self, entry, now) -> bool:
        seen, address, lat, lon, callsign = entry
        if now - seen > TWIN_FRESH_S:
            return False
        keyed = self._keyed.get(address) if address else None
        if keyed and now - keyed[0] <= TWIN_FRESH_S:
            return True
        same = self._by_call.get(callsign) if len(callsign) >= 4 else None
        return bool(same and now - same[0] <= TWIN_FRESH_S and _km(lat, lon, same[1], same[2]) <= TWIN_NEAR_KM)

    def _forget(self, now) -> None:
        for table in (self._keyed, self._by_call, self._loose):
            for key in [k for k, v in table.items() if now - v[0] > 3600]:
                table.pop(key, None)


class TwinSuppressions:
    """Layer side: on_sample() for the announcements, is_suppressed(track) before drawing a track."""

    def __init__(self, ttl_s: float = TWIN_TTL_S):
        self._ttl = ttl_s
        self._until: dict = {}
        self._lock = threading.Lock()

    def on_sample(self, sample) -> None:
        try:
            uid = json.loads(bytes(sample.payload).decode()).get("uid")
        except (ValueError, UnicodeDecodeError, AttributeError):
            return
        if uid:
            with self._lock:
                self._until[str(uid)] = time.time() + self._ttl

    def is_suppressed(self, track: dict, now: float | None = None) -> bool:
        if track.get("icao24") or not track.get("uid"):
            return False
        now = time.time() if now is None else now
        with self._lock:
            return self._until.get(str(track["uid"]), 0.0) > now
