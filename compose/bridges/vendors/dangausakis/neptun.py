#!/usr/bin/env python3
"""NEPTUN (neptun.in.ua) live air threats -> Zenoh tracks. Part of the dangausakis bridge.

Used by dangausakis_bridge.py (one service, one poll loop); not a launcher entry
point. Polls NEPTUN's open REST API (GET /api/v1/threats; free, read-only, no key,
CORS open) for active drones, missiles and glide bombs over Ukraine and publishes
each as a track via publish_dual() so tak_layer and sitaware_layer draw it:
  <ORG>/air/neptun/hostile/uav/<type>/<uid>/...       uav, fpv, recon
  <ORG>/air/neptun/hostile/missile/<type>/<uid>/...   missile, ballistic
  <ORG>/air/neptun/hostile/bomb/<type>/<uid>/...      kab (guided / glide bomb)
  <ORG>/air/neptun/hostile/aircraft/<type>/<uid>/...  mig31k
  <ORG>/air/neptun/unknown/aircraft/<type>/<uid>/...  unknown or any type not listed above

Fusion handshake: when protocols/vendors/random/fusion.py matches a threat to a
non-cooperative EFDI radar track it announces
<ORG>/air/trackfusion/suppress/neptun/<uid>; this bridge then holds that threat
back (one _delete tombstone retracts the marker already drawn) for as long as
the announcements keep coming (SUPPRESS_TTL_S), so the radar marker is the only
one. If fusion is not running nothing is ever suppressed.

Dedupe: NEPTUN's own stable per-track `id` becomes uid "NEPTUN-<id>", so every
poll updates the same marker. NEPTUN already merges multiple reports into one
track (its `sourceCount`). Records NEPTUN flags `areaOnly` have only a region
centroid, not a position, so they are not published. NEPTUN ids never match
EFDI's own sensor ids (no ICAO/MMSI); the radar match above is what removes
that duplicate, and only for radar tracks close to and heading with the threat.

NEPTUN's terms require a visible link to NEPTUN next to the data and ask REST
pollers to stay at or above 5 s. Every track carries the link in `remarks`
(rendered in the TAK info card and the SitaWare attribute card) and "NEPTUN"
in the callsign; keep a link wherever else this data is displayed.
NEPTUN is an information aggregator, not an official warning system.

Freshness: NEPTUN keeps listing a threat for minutes after its last report. A
track's `_ts` is the time we received it (so TAK/SitaWare keep it on screen while
NEPTUN still lists it), and the report's real age is carried as position_age_s and
shown in the remarks. Past NEPTUN_STALE_S the callsign gains "(STALE)"; past
NEPTUN_MAX_AGE_S the threat is no longer published and expires.

Env:
  NEPTUN_POLL_S      poll interval, seconds (default 10, minimum 5)
  NEPTUN_STALE_S     report age at which a threat is marked STALE (default 300; live
                     reports are typically 100-450 s old, so a lower value flags almost all)
  NEPTUN_MAX_AGE_S   report age after which a threat is dropped (default 900)
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from http_json import read_json_response
from namespace_prefix import topic_root
from protocols.vendors.random.gateway import publish_dual
from protocols.vendors.random.proto.normalized_track_pb2 import NormalizedTrack
from protocols.vendors.random.track_views import add_version, semantic_topic

TOPIC_ROOT = topic_root()
URL = "https://neptun.in.ua/api/v1/threats"
SUPPRESS_TTL_S = 30
SUPPRESS_TOPIC = "{}/air/trackfusion/suppress/neptun/*".format(TOPIC_ROOT)
STALE_S = int(os.environ.get("NEPTUN_STALE_S", "300"))
MAX_AGE_S = int(os.environ.get("NEPTUN_MAX_AGE_S", "900"))
POLL_S = max(5, int(os.environ.get("NEPTUN_POLL_S", "10")))
# Shown in the TAK info card and the SitaWare NVG attribute card (both render `remarks`).
ATTRIBUTION = "Data: NEPTUN https://neptun.in.ua/ - OSINT aggregator, not radar or an official warning"
# NEPTUN's documented types: uav | recon | missile | ballistic | kab | mig31k |
# unknown (+ fpv in its SDK) -> (affiliation slot, entity slot). Anything not
# known is published as an unknown-affiliation aircraft rather than guessed as a drone.
_KIND = {
    "uav": ("hostile", "uav"), "fpv": ("hostile", "uav"), "recon": ("hostile", "uav"),
    "missile": ("hostile", "missile"), "ballistic": ("hostile", "missile"), "kab": ("hostile", "bomb"),
    "mig31k": ("hostile", "aircraft"),
}
_DEFAULT_KIND = ("unknown", "aircraft")


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


def _remarks(t: dict, age_s) -> str:
    parts = [ATTRIBUTION]
    if t.get("confidenceLevel"):
        parts.append("confidence: {}".format(t["confidenceLevel"]))
    if t.get("sourceCount"):
        parts.append("sources: {}".format(t["sourceCount"]))
    if t.get("uncertaintyKm"):
        parts.append("position +/-{:g} km".format(t["uncertaintyKm"]))
    if age_s is not None:
        parts.append("last report {} s ago{}".format(
            int(age_s), " - STALE" if age_s > STALE_S else ""))
    return " | ".join(parts)


def threat_tracks(data: dict, now: float = None) -> list:
    """(topic prefix, track) per publishable threat."""
    now = time.time() if now is None else now
    out = []
    for t in data.get("threats") or []:
        kind = (t.get("type") or "").lower()
        if (not t.get("id") or t.get("lat") is None or t.get("lon") is None
                or t.get("areaOnly") or t.get("status") != "active"):
            continue
        observed = _epoch(t.get("updatedAt"))
        age_s = max(0.0, now - observed) if observed is not None else None
        if age_s is not None and age_s > MAX_AGE_S:
            continue
        stale = age_s is not None and age_s > STALE_S
        affiliation, entity = _KIND.get(kind, _DEFAULT_KIND)
        track = {
            "_src": "neptun.in.ua",
            "_ts": now,
            "observed_ts": observed,
            "position_age_s": None if age_s is None else int(age_s),
            "report_stale": True if stale else None,
            "uid": "NEPTUN-{}".format(t["id"]),
            "type": kind or "unknown",
            "target_type": entity,
            "callsign": "NEPTUN {} {}{}".format(
                kind.upper() or "THREAT", t.get("locality") or t.get("region") or "",
                " (STALE)" if stale else "").replace("  ", " ").strip(),
            "lat_deg": t["lat"],
            "lon_deg": t["lon"],
            "region": t.get("region"),
            "locality": t.get("locality"),
            "confidence": t.get("confidenceLevel"),
            "source_count": t.get("sourceCount"),
            "position_quality": t.get("positionQuality"),
            "remarks": _remarks(t, age_s),
        }
        if t.get("heading") is not None:
            track["heading_deg"] = t["heading"]
        if t.get("uncertaintyKm"):
            track["position_uncertainty_m"] = t["uncertaintyKm"] * 1000
        out.append(("{}/air/neptun/{}/{}".format(TOPIC_ROOT, affiliation, entity),
                    {k: v for k, v in track.items() if v not in ("", None)}))
    return out


class Suppression:
    """uid -> expiry, fed by fusion's announcements."""

    def __init__(self, ttl_s: float = SUPPRESS_TTL_S):
        self._ttl = ttl_s
        self._until: dict = {}
        self._lock = threading.Lock()

    def on_sample(self, sample):
        try:
            uid = json.loads(bytes(sample.payload).decode()).get("uid")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, AttributeError):
            return
        if uid:
            with self._lock:
                self._until[uid] = time.time() + self._ttl

    def active(self, uid: str, now: float = None) -> bool:
        with self._lock:
            return self._until.get(uid, 0) > (time.time() if now is None else now)


def publish_threats(session, tracks, suppression: Suppression, retracted: set, verbose=False):
    """Publish each track unless fusion has matched it to a radar track; a newly
    suppressed one is retracted once with a tombstone, and re-published normally
    as soon as the suppression lapses."""
    for prefix, track in tracks:
        uid = track["uid"]
        if suppression.active(uid):
            if uid not in retracted:
                retracted.add(uid)
                session.put(add_version(semantic_topic(prefix, track)),
                            json.dumps({"uid": uid, "_delete": True, "_ts": time.time(),
                                        "_src": "neptun.in.ua"}).encode())
                if verbose:
                    print("SUPPRESS", uid, "(matched to a radar track)", flush=True)
            continue
        retracted.discard(uid)
        publish_dual(session, prefix, track, NormalizedTrack)
        if verbose:
            print("THREAT", uid, track["type"], track["lat_deg"], track["lon_deg"], flush=True)


def fetch_threats() -> dict | None:
    req = urllib.request.Request(URL, headers={"User-Agent": "EFDI-dangausakis-bridge", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return read_json_response(resp)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print("neptun fetch error: {}".format(exc), flush=True)
        return None
