#!/usr/bin/env python3
"""fusion.py — Multi-source track correlation and enrichment protocol.

Subscribes to all air track topics in the EFDI Zenoh fabric, correlates
tracks from multiple sensors, and publishes enriched fused tracks.

Fusion strategy (in priority order):
  1. ICAO 24-bit key match (exact)
       Radar (CAT-48 Mode-S) already decodes ICAO hex from SSR responses.
       Partner ADS-B and CAT-21 sources provide the same key.
       When both have the same ICAO hex, the fused track takes:
         • Position, speed, heading → from the radar (higher accuracy, lower latency)
         • callsign, registration, aircraft_type, squawk → from ADS-B (richer metadata)

  2. Squawk + altitude match
       For Mode-C transponders (no Mode-S): radar gets squawk + baro altitude.
       Match ADS-B tracks with same squawk code within 500 ft vertical tolerance.

  3. Spatial proximity match (fallback for PSR-only / non-cooperative targets)
       Radar track with no transponder: compare position against all ADS-B tracks.
       If the nearest ADS-B track is within SPATIAL_THRESHOLD_NM nautical miles
       AND the age difference is within 30 s, merge identities.
       Marks the track as "probable" rather than confirmed.

NEPTUN corroboration (non-cooperative radar tracks vs the NEPTUN feed in dangausakis_bridge.py threats):
  A radar track WITHOUT an ICAO address that lies within the NEPTUN threat's own
  uncertainty radius (clamped to FUSION_NEPTUN_MIN_NM..FUSION_NEPTUN_MAX_NM),
  agrees in heading (60 deg, when both have one) and in speed class (drone-class
  tracks <= 220 m/s, missile/ballistic/kab >= 100 m/s, when the radar reports a
  speed) is taken to be the same object — but only one-to-one: if the track fits
  more than one threat, or the threat fits more than one radar object (tracks
  sharing a cross-radar handoff primary count as one), nothing is merged. The radar track stays the marker — its remarks gain "Also
  reported by NEPTUN" — and the NEPTUN marker is suppressed by publishing
    <ORG>/air/trackfusion/suppress/neptun/<uid>
  which the dangausakis bridge's NEPTUN feed honours for FUSION_NEPTUN_SUPPRESS_S (30 s) after the
  last refresh, retracting its marker with a tombstone. Fusion never suppresses
  a radar track, and when the radar match stops (track lost, drifted apart) the
  suppression lapses and the NEPTUN marker returns. With fusion not running,
  NEPTUN simply publishes normally.

Cross-radar handoff (same-protocol, PSR-only targets):
  When multiple radars of the same type (e.g., two CAT-48 sites) cover overlapping
  areas, a PSR-only target crossing the boundary would otherwise create two separate
  ATAK markers. The fusion bridge prevents this by:

    Overlap zone (both radars tracking):
      Both tracks are within FUSION_HANDOFF_NM (default 2 NM) after dead-reckoning
      each to a common time, AND their headings agree within 45°.
      → Both share the primary radar's radar_id.  Single ATAK marker updated by
        whichever radar sent the most recent report.

    Handoff (target leaves primary radar's coverage):
      Primary track ages out of the cache.  The surviving secondary is promoted:
      its radar_id becomes the new stable ID.  One ATAK UID change occurs at this
      moment; the marker then stays stable under the new radar.

    Mode-S targets are unaffected — ICAO24 is a global stable key across all radars.

Config:
  FUSION_HANDOFF_NM=2.0   Max distance for cross-radar PSR association

Fused tracks are published on the object key, in every view:
  <ORG>/air/trackfusion/fused/<affiliation>/aircraft/<type>/<id>/{sapient,json,proto,raw}
→ tak_layer.py picks these up and shows them in ATAK with full identity.

Non-correlated radar tracks (truly non-cooperative, no ID possible) are
re-published as-is under:
  <ORG>/air/trackfusion/fused/unknown/aircraft/<type>/<id>/{sapient,json,proto,raw}
so they still appear in ATAK but without enrichment.

`source` is `trackfusion` (this bridge) and `modality` is `fused`; the pair is
also what keeps our own output out of the radar-source subscription — see
_OWN_PREFIX below.

Config (compose/.env):
  FUSION_SPATIAL_NM=2.0    Max distance for spatial match (default 2 NM)
  FUSION_MAX_AGE_S=60      Drop tracks older than this from the cache
  FUSION_RADAR_PREF=1      Set 0 to prefer ADS-B position (e.g. for GPS accuracy)
  FUSION_NEPTUN_MIN_NM=1.0 Smallest radius for a radar<->NEPTUN match
  FUSION_NEPTUN_MAX_NM=5.0 Largest radius (caps NEPTUN's own uncertainty)
  FUSION_NEPTUN_UAV_MAX_MS=220     Fastest radar speed still accepted for a drone-class threat
  FUSION_NEPTUN_MISSILE_MIN_MS=100 Slowest radar speed accepted for a missile / glide bomb

Run:
  venv/bin/python3 protocols/fusion.py
  venv/bin/python3 protocols/fusion.py --verbose
"""

import argparse
import json
import math
import os
import threading
import time
from protocols.vendors.random.proto.normalized_track_pb2 import NormalizedTrack

from google.protobuf.message import DecodeError
from namespace_prefix import topic_root
from protocols.vendors.random.gateway import open_session, publish_collection, subscribe
from protocols.vendors.random.track_views import strip_version
from protocols.vendors.random.twin_suppress import TwinIndex, twin_topic

TOPIC_ROOT = topic_root()

_SPATIAL_NM      = float(os.environ.get("FUSION_SPATIAL_NM",  "2.0"))
_MAX_AGE_S       = float(os.environ.get("FUSION_MAX_AGE_S",   "60"))
_RADAR_PREF      = os.environ.get("FUSION_RADAR_PREF", "1") != "0"

# Cross-radar handoff — PSR-only targets seen by multiple radars simultaneously
_HANDOFF_NM      = float(os.environ.get("FUSION_HANDOFF_NM",  "2.0"))  # spatial tolerance
_HANDOFF_HDG_TOL = 45.0   # heading difference tolerance in degrees

# NEPTUN corroboration (see module docstring)
_NEPTUN_MIN_NM   = float(os.environ.get("FUSION_NEPTUN_MIN_NM", "1.0"))
_NEPTUN_MAX_NM   = float(os.environ.get("FUSION_NEPTUN_MAX_NM", "5.0"))
_NEPTUN_HDG_TOL  = 60.0
_NEPTUN_TOPIC    = "{}/air/neptun/hostile/**".format(TOPIC_ROOT)
_SUPPRESS_TOPIC  = "{}/air/trackfusion/suppress/neptun/{{}}".format(TOPIC_ROOT)
_SUPPRESS_REFRESH_S = 5.0   # re-announce a standing match at most this often

TOPIC_FUSED = "{}/air/trackfusion/fused/{}/aircraft"

# Everything this bridge publishes lives under here — used to reject our own
# output when re-subscribing by modality.
_OWN_PREFIX = "{}/air/trackfusion/".format(TOPIC_ROOT)

# Radar and ADS-B are separated by MODALITY, not by source name. The source
# segment is a wildcard because a radar names itself by SAC/SIC, so its topic
# is not knowable at startup — and two radars must both be picked up.
#
# This split is the whole point of the bridge: ASTERIX CAT-048 (radar) is the
# positional authority, CAT-021 (ADS-B relayed by a ground station) only
# enriches identity. While both published under the literal `asterix` these
# two lists were identical and every ASTERIX track was fed in as both.

# Positional authority
_RADAR_TOPICS = [
    "{}/air/*/radar/**".format(TOPIC_ROOT),
    "{}/air/*/mlat/**".format(TOPIC_ROOT),
    "{}/air/*/fused/**".format(TOPIC_ROOT),
    "{}/air/stanag_4586/telemetry/**".format(TOPIC_ROOT),
]

# Identity enrichment only
_ADSB_TOPICS = [
    "{}/air/*/adsb/**".format(TOPIC_ROOT),
]

# Fields that carry identity (we prefer ADS-B values for these)
_ID_FIELDS = ("callsign", "registration", "aircraft_type", "icao24",
              "squawk", "origin", "destination", "operator",
              "route", "rssi_db", "emitter_category_str", "on_ground")

# ADS-B fields taken as supplement only when radar cannot provide them
_ADSB_SUPPLEMENT = ("vertical_rate_ms",)

# Fields where radar is the authority (position, kinematics)
_RADAR_FIELDS = ("lat_deg", "lon_deg", "alt_m", "alt_baro_ft", "alt_3d_ft",
                 "speed_ms", "heading_deg", "range_nm", "azimuth_deg",
                 "range_nm", "sac", "sic", "track_num", "radar_id", "tod_s")


def _extrapolate_pos(lat, lon, speed_ms, heading_deg, dt_s):
    """Dead-reckon lat/lon forward by dt_s seconds. Returns (lat, lon) unchanged if no speed."""
    if lat is None or lon is None or not speed_ms:
        return lat, lon
    d  = speed_ms * dt_s
    R  = 6_371_000.0
    az = math.radians(heading_deg or 0)
    la = math.radians(lat)
    lo = math.radians(lon)
    la2 = math.asin(math.sin(la) * math.cos(d / R) +
                    math.cos(la) * math.sin(d / R) * math.cos(az))
    lo2 = lo + math.atan2(math.sin(az) * math.sin(d / R) * math.cos(la),
                          math.cos(d / R) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3440.065   # Earth radius in NM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _heading_diff(a, b) -> float:
    diff = abs(a - b) % 360
    return 360 - diff if diff > 180 else diff


_NEPTUN_MISSILE_TYPES = frozenset({"missile", "ballistic", "kab"})
_NEPTUN_UAV_TYPES = frozenset({"uav", "fpv", "recon"})
# Jet-powered drones reach 600-700 km/h (167-194 m/s), so the drone ceiling sits
# above that (220 m/s ~ 790 km/h). The two ranges overlap on purpose: each is only
# a plausibility check against the threat's own declared type, not a classifier.
_NEPTUN_UAV_MAX_MS = float(os.environ.get("FUSION_NEPTUN_UAV_MAX_MS", "220"))
_NEPTUN_MISSILE_MIN_MS = float(os.environ.get("FUSION_NEPTUN_MISSILE_MIN_MS", "100"))


def _neptun_pair_ok(radar: dict, threat: dict) -> bool:
    """Could this radar track be that NEPTUN threat? Position inside the threat's
    own uncertainty radius, heading and speed class compatible. Transponder
    (ICAO) tracks never qualify. A missing heading or speed is not held against
    the pair."""
    if radar.get("icao24") or radar.get("lat_deg") is None or radar.get("lon_deg") is None:
        return False
    if threat.get("lat_deg") is None or threat.get("lon_deg") is None:
        return False
    radius = min(max((threat.get("position_uncertainty_m") or 0) / 1852.0, _NEPTUN_MIN_NM), _NEPTUN_MAX_NM)
    if _haversine_nm(radar["lat_deg"], radar["lon_deg"], threat["lat_deg"], threat["lon_deg"]) > radius:
        return False
    r_hdg, n_hdg = radar.get("heading_deg"), threat.get("heading_deg")
    if r_hdg is not None and n_hdg is not None and _heading_diff(r_hdg, n_hdg) > _NEPTUN_HDG_TOL:
        return False
    speed = radar.get("speed_ms")
    kind = (threat.get("type") or "").lower()
    if speed and kind in _NEPTUN_MISSILE_TYPES:
        return speed >= _NEPTUN_MISSILE_MIN_MS
    if speed and kind in _NEPTUN_UAV_TYPES:
        return speed <= _NEPTUN_UAV_MAX_MS
    return True   # mig31k / unknown: no speed class to check against


def match_neptun(radar: dict, radar_uid: str, radar_cache: dict, neptun: dict, now: float,
                 ident=lambda uid: uid, max_age_s: float = None):
    """uid of the NEPTUN threat this radar track corroborates, or None.

    One-to-one only: the radar track must be compatible with exactly ONE fresh
    threat, and that threat with exactly ONE radar object (cache entries that
    `ident` maps to the same cross-radar primary count as one object). Anything
    ambiguous returns None, so NEPTUN stays visible rather than risk hiding a
    threat behind a wrong pairing.

    `radar_cache` and `neptun` are {uid: {"track": dict, "ts": float}}.
    """
    max_age_s = _MAX_AGE_S if max_age_s is None else max_age_s
    fresh = [(uid, e["track"]) for uid, e in neptun.items() if now - e["ts"] <= max_age_s]
    candidates = [(uid, t) for uid, t in fresh if _neptun_pair_ok(radar, t)]
    if len(candidates) != 1:
        return None
    threat_uid, threat = candidates[0]
    me = ident(radar_uid)
    for other_uid, entry in radar_cache.items():
        if now - entry["ts"] > max_age_s or ident(other_uid) == me:
            continue
        if _neptun_pair_ok(entry["track"], threat):
            return None
    return threat_uid


def _uid_of(track: dict) -> str:
    for f in ("icao24", "uid", "track_num", "radar_id", "mmsi"):
        v = track.get(f)
        if v:
            return str(v)
    return "unknown"


def _aff_of(track: dict, topic: str) -> str:
    parts = topic.split("/")
    for p in parts:
        if p in ("friendly", "hostile", "neutral", "unknown", "civ", "mil"):
            return p if p in ("friendly", "hostile", "neutral") else "unknown"
    return "unknown"


class TrackFuser:
    def __init__(self, session: "zenoh.Session", verbose: bool):
        self._session = session
        self._verbose = verbose
        self._lock    = threading.Lock()
        # uid → {track, topic, ts}
        self._radar_tracks: dict[str, dict] = {}
        self._adsb_tracks:  dict[str, dict] = {}
        # icao24 → uid in adsb_tracks
        self._adsb_by_icao:   dict[str, str] = {}
        # icao24 → uid in radar_tracks (for fast coverage check in on_adsb)
        self._radar_by_icao:  dict[str, str] = {}
        # squawk → list of adsb uids
        self._adsb_by_squawk: dict[str, list] = {}
        # PSR cross-radar handoff: uid → primary_uid (stable ID across radar boundaries)
        # Key = any radar uid; value = whichever radar uid was first to own this target.
        # When the primary ages out, the surviving secondary is promoted automatically.
        self._radar_primary: dict[str, str] = {}
        # NEPTUN threats seen on the fabric, and when each suppression was last announced
        self._neptun_tracks: dict[str, dict] = {}
        self._suppress_sent: dict[str, float] = {}
        # Loose copies of an ICAO-keyed aircraft (see twin_suppress.py) and when each was last announced
        self._twins = TwinIndex()
        self._twin_sent: dict[str, float] = {}
        # Periodic age-out: ensures _radar_by_icao is cleaned even when radar goes
        # silent (no on_radar() calls), so ADS-B fallback kicks in automatically.
        self._start_age_timer()

    # ------------------------------------------------------------------
    # Ingest handlers
    # ------------------------------------------------------------------

    def on_radar(self, sample):
        topic = str(sample.key_expr)
        # A track is published in four views; the fusion model is dict-based, so
        # only the JSON view is consumed. The `**` subscription matches all four,
        # so the other three are skipped here rather than left to fail json.loads.
        if not topic.endswith("/tracks/v1"):
            return
        base = strip_version(topic)
        if base.rsplit("/", 1)[-1] in {"sapient", "proto", "raw"}:
            return
        # _RADAR_TOPICS matches `/air/*/fused/**` to pick up ASTERIX CAT-062
        # system tracks — which also matches THIS bridge's own output, so a
        # fused track would be re-ingested as a radar source and fused with
        # itself. Drop our own publications before anything else.
        if topic.startswith(_OWN_PREFIX):
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, DecodeError):
            return
        self._age_out()
        now = time.time()
        with self._lock:
            uid = _uid_of(track)
            self._radar_tracks[uid] = {"track": track, "topic": topic, "ts": now}
            icao = track.get("icao24", "").strip().lower()
            if icao:
                self._radar_by_icao[icao] = uid

            # Cross-radar handoff for PSR-only targets
            primary_uid = self._cross_radar_associate(track, uid, now)
            if primary_uid != uid:
                # Borrow the primary radar's radar_id so tak_layer produces the
                # same ATAK UID throughout the overlap and across the handoff.
                primary_entry = self._radar_tracks.get(primary_uid)
                if primary_entry and primary_entry["track"].get("radar_id"):
                    track = dict(track)
                    track["radar_id"] = primary_entry["track"]["radar_id"]
                    if self._verbose:
                        print("HANDOFF {} → {}".format(
                            uid, primary_uid), flush=True)

        self._fuse_and_publish(track, topic)

    def _start_age_timer(self):
        self._age_out()
        t = threading.Timer(_MAX_AGE_S / 2, self._start_age_timer)
        t.daemon = True
        t.start()

    # ------------------------------------------------------------------
    # Cross-radar handoff (PSR-only targets)
    # ------------------------------------------------------------------

    def _cross_radar_associate(self, track: dict, uid: str, now: float) -> str:
        """Called under self._lock.

        For PSR-only targets (no ICAO24): search all other-radar tracks for a
        spatially + kinematically consistent match.  If found, both tracks share
        the same primary uid → same radar_id in the fused output → single ATAK
        marker throughout the overlap zone and across the handoff boundary.

        Returns the stable primary uid to use for fused publication.
        """
        # Mode-S: already stable by ICAO — no cross-radar association needed
        if track.get("icao24"):
            return uid

        r_lat = track.get("lat_deg")
        r_lon = track.get("lon_deg")
        if r_lat is None or r_lon is None:
            return self._radar_primary.setdefault(uid, uid)

        r_sac = track.get("sac")
        r_sic = track.get("sic")
        r_hdg = track.get("heading_deg")

        best_d       = _HANDOFF_NM
        best_primary = None

        for other_uid, entry in self._radar_tracks.items():
            if other_uid == uid:
                continue
            other = entry["track"]
            # Same radar — skip (different tracks on the same sensor are not handoffs)
            if other.get("sac") == r_sac and other.get("sic") == r_sic:
                continue
            # Mode-S tracks have their own stable ID — don't pull them into PSR handoff
            if other.get("icao24"):
                continue
            dt = now - entry["ts"]
            if dt > _MAX_AGE_S:
                continue

            # Extrapolate the other track forward to align timestamps
            o_lat, o_lon = _extrapolate_pos(
                other.get("lat_deg"), other.get("lon_deg"),
                other.get("speed_ms", 0) or 0,
                other.get("heading_deg", 0) or 0,
                dt)
            if o_lat is None:
                continue

            d = _haversine_nm(r_lat, r_lon, o_lat, o_lon)
            if d >= best_d:
                continue

            # Heading consistency gate (skip if headings diverge > tolerance)
            o_hdg = other.get("heading_deg")
            if r_hdg is not None and o_hdg is not None:
                diff = abs(r_hdg - o_hdg) % 360
                if diff > 180:
                    diff = 360 - diff
                if diff > _HANDOFF_HDG_TOL:
                    continue

            best_d       = d
            best_primary = self._radar_primary.get(other_uid, other_uid)

        if best_primary is not None:
            self._radar_primary[uid] = best_primary
        else:
            self._radar_primary.setdefault(uid, uid)

        return self._radar_primary[uid]

    def on_neptun(self, sample):
        topic = str(sample.key_expr)
        if not topic.endswith("/tracks/v1"):
            return
        if strip_version(topic).rsplit("/", 1)[-1] in {"sapient", "proto", "raw"}:
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, DecodeError):
            return
        uid = track.get("uid")
        if not uid:
            return
        with self._lock:
            if track.get("_delete"):
                self._neptun_tracks.pop(uid, None)
            else:
                self._neptun_tracks[uid] = {"track": track, "ts": time.time()}

    def _corroborate_with_neptun(self, fused: dict, radar: dict) -> None:
        """If a NEPTUN threat matches this radar track one-to-one, annotate `fused`
        and tell the dangausakis bridge to hold back that threat's marker."""
        now = time.time()
        radar_uid = _uid_of(radar)
        with self._lock:
            for uid in [u for u, e in self._neptun_tracks.items() if now - e["ts"] > _MAX_AGE_S]:
                del self._neptun_tracks[uid]
            uid = match_neptun(radar, radar_uid, self._radar_tracks, self._neptun_tracks, now,
                               ident=lambda u: self._radar_primary.get(u, u))
            threat = self._neptun_tracks[uid]["track"] if uid else None
            announce = bool(uid) and now - self._suppress_sent.get(uid, 0) >= _SUPPRESS_REFRESH_S
            if announce:
                self._suppress_sent[uid] = now
        if not uid:
            return
        label = "Also reported by NEPTUN ({} {})".format(
            threat.get("type", "threat"), threat.get("locality") or threat.get("region") or "").strip()
        fused["remarks"] = "{}; {}".format(fused["remarks"], label) if fused.get("remarks") else label
        if announce:
            self._session.put(_SUPPRESS_TOPIC.format(uid),
                              json.dumps({"uid": uid, "radar_uid": radar_uid, "ts": now}).encode())
            if self._verbose:
                print("NEPTUN match {} ~ radar {}".format(uid, radar_uid), flush=True)

    def on_any_track(self, sample):
        """Every track on the fabric, whatever its source: announce the loose copies of aircraft that
        another feed already reports by their ICAO address, so the output layers draw one marker."""
        topic = str(sample.key_expr)
        if not topic.endswith("/tracks/v1") or topic.startswith(_OWN_PREFIX):
            return
        if strip_version(topic).rsplit("/", 1)[-1] in {"sapient", "proto", "raw"}:
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(track, dict):
            return
        now = time.time()
        for uid in self._twins.observe(track, now):
            with self._lock:
                if now - self._twin_sent.get(uid, 0) < _SUPPRESS_REFRESH_S:
                    continue
                self._twin_sent[uid] = now
            self._session.put(twin_topic(TOPIC_ROOT, uid), json.dumps({"uid": uid, "ts": now}).encode())
            if self._verbose:
                print("twin of an ICAO-keyed aircraft: {}".format(uid), flush=True)

    def on_adsb(self, sample):
        topic = str(sample.key_expr)
        # Enrichment tracks are consumed from the JSON view only — see on_radar.
        # The old `/v2` protobuf branch keyed on a view name that no longer
        # exists (the typed view is `/proto` now), so it was dead: parsing it
        # here as well would double-ingest every track.
        if not topic.endswith("/json"):
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, DecodeError):
            return
        self._age_out()   # clean stale radar entries before checking coverage
        radar_covers = False
        with self._lock:
            uid = _uid_of(track)
            self._adsb_tracks[uid] = {"track": track, "topic": topic, "ts": time.time()}
            icao = track.get("icao24", "").strip().lower()
            if icao:
                self._adsb_by_icao[icao] = uid
                radar_covers = icao in self._radar_by_icao
            sq = track.get("squawk", "")
            if sq and sq not in ("0000", "7500", "7600", "7700"):
                self._adsb_by_squawk.setdefault(sq, [])
                if uid not in self._adsb_by_squawk[sq]:
                    self._adsb_by_squawk[sq].append(uid)

        if not radar_covers:
            # No radar covering this aircraft — publish ADS-B track as fallback so
            # it appears in ATAK. When radar picks it up, the fused track takes over
            # seamlessly (same ICAO-based UID, same marker in ATAK).
            affiliation = "mil" if track.get("is_military") else "civ"
            pub_topic = TOPIC_FUSED.format(TOPIC_ROOT, affiliation)
            publish_collection(self._session, pub_topic, track, NormalizedTrack)
            if self._verbose:
                ident = track.get("callsign") or track.get("icao24") or "?"
                print("ADSB fallback [no radar] {}".format(ident), flush=True)

    # ------------------------------------------------------------------
    # Fusion logic
    # ------------------------------------------------------------------

    def _fuse_and_publish(self, radar: dict, radar_topic: str):
        adsb = self._find_match(radar)
        if adsb is not None:
            fused, method = self._merge(radar, adsb)
        else:
            fused, method = dict(radar), "radar-only"
        self._corroborate_with_neptun(fused, radar)

        # Matched tracks retain the ADS-B database's civil/military category;
        # tak_layer.py then applies the scenario ICAO affiliation classifier.
        # Unmatched radar-only contacts stay unknown.
        if adsb is None:
            aff_slot = "unknown"
        else:
            aff_slot = "mil" if adsb.get("is_military") else "civ"
        topic = TOPIC_FUSED.format(TOPIC_ROOT, aff_slot)
        publish_collection(self._session, topic, fused, NormalizedTrack)
        if self._verbose:
            ident = (fused.get("callsign") or fused.get("icao24") or
                     fused.get("radar_id") or "?")
            print("FUSE [{}] {} → {}".format(method, ident,
                  "/".join(topic.split("/")[1:4])), flush=True)

    def _find_match(self, radar: dict) -> dict | None:
        """Return the best-matching ADS-B track or None."""
        now = time.time()
        with self._lock:
            # 1. ICAO exact match
            icao = radar.get("icao24", "").strip().lower()
            if icao:
                uid = self._adsb_by_icao.get(icao)
                if uid and uid in self._adsb_tracks:
                    entry = self._adsb_tracks[uid]
                    if now - entry["ts"] < _MAX_AGE_S:
                        return entry["track"]

            # 2. Squawk + altitude match
            sq = radar.get("squawk", "")
            if sq and sq not in ("0000", "7500", "7600", "7700"):
                uids = self._adsb_by_squawk.get(sq, [])
                r_alt = radar.get("alt_baro_ft")
                for uid in uids:
                    if uid not in self._adsb_tracks:
                        continue
                    entry = self._adsb_tracks[uid]
                    if now - entry["ts"] >= _MAX_AGE_S:
                        continue
                    a_alt = entry["track"].get("alt_baro_ft")
                    if r_alt and a_alt and abs(r_alt - a_alt) < 500:
                        return entry["track"]
                    elif r_alt is None and a_alt is None:
                        return entry["track"]

            # 3. Spatial proximity
            r_lat = radar.get("lat_deg")
            r_lon = radar.get("lon_deg")
            if r_lat is None or r_lon is None:
                return None
            best_d, best_t = _SPATIAL_NM, None
            for uid, entry in self._adsb_tracks.items():
                if now - entry["ts"] >= _MAX_AGE_S:
                    continue
                t = entry["track"]
                a_lat = t.get("lat_deg")
                a_lon = t.get("lon_deg")
                if a_lat is None or a_lon is None:
                    continue
                d = _haversine_nm(r_lat, r_lon, a_lat, a_lon)
                if d < best_d:
                    best_d, best_t = d, t
            if best_t is not None:
                best_t = dict(best_t)
                best_t["_fusion_method"] = "spatial-{:.2f}NM".format(best_d)
                return best_t

        return None

    def _merge(self, radar: dict, adsb: dict) -> tuple[dict, str]:
        fused  = {}
        method = adsb.get("_fusion_method", "icao-exact")

        if _RADAR_PREF:
            # Radar is authoritative for all kinematics — start from radar entirely,
            # then layer on only the identity fields from ADS-B.
            fused.update(radar)
            for k in _ID_FIELDS:
                if k in adsb:
                    fused[k] = adsb[k]
            # Supplement: take kinematic fields from ADS-B only if radar cannot
            # provide them (e.g. vertical rate — not available in CAT-48).
            for k in _ADSB_SUPPLEMENT:
                if k not in fused and k in adsb:
                    fused[k] = adsb[k]
        else:
            # ADS-B pref: start from ADS-B, overwrite kinematics with radar
            fused.update(adsb)
            for k in _RADAR_FIELDS:
                if k in radar:
                    fused[k] = radar[k]

        # Always use the fresher timestamp
        fused["_ts"]  = max(radar.get("_ts", 0), adsb.get("_ts", 0))
        fused["_src"] = "{} + {}".format(
            radar.get("_src", "radar"), adsb.get("_src", "adsb"))
        fused.pop("_fusion_method", None)
        return fused, method

    # ------------------------------------------------------------------
    # Cache maintenance
    # ------------------------------------------------------------------

    def _age_out(self):
        now = time.time()
        with self._lock:
            stale_r = [k for k, v in self._radar_tracks.items()
                       if now - v["ts"] > _MAX_AGE_S]
            stale_a = [k for k, v in self._adsb_tracks.items()
                       if now - v["ts"] > _MAX_AGE_S]
            stale_set = set(stale_r)

            for k in stale_r:
                entry = self._radar_tracks.pop(k)
                icao = entry["track"].get("icao24", "").strip().lower()
                if icao and self._radar_by_icao.get(icao) == k:
                    del self._radar_by_icao[icao]

                # Cross-radar handoff promotion:
                # If k was a primary, elect the first surviving secondary as the
                # new primary so the ATAK marker transfers cleanly.
                if self._radar_primary.get(k) == k:
                    new_primary = None
                    for other_uid, p in list(self._radar_primary.items()):
                        if p == k and other_uid not in stale_set and other_uid in self._radar_tracks:
                            new_primary = other_uid
                            break
                    if new_primary:
                        # Repoint every uid that referenced old primary → new primary
                        for uid2 in list(self._radar_primary):
                            if self._radar_primary[uid2] == k:
                                self._radar_primary[uid2] = new_primary
                        self._radar_primary[new_primary] = new_primary
                        if self._verbose:
                            print("HANDOFF promote {} → {}".format(k, new_primary), flush=True)
                self._radar_primary.pop(k, None)

            for k in stale_a:
                entry = self._adsb_tracks.pop(k)
                icao = entry["track"].get("icao24", "").strip().lower()
                self._adsb_by_icao.pop(icao, None)
                sq = entry["track"].get("squawk", "")
                if sq in self._adsb_by_squawk:
                    try:
                        self._adsb_by_squawk[sq].remove(k)
                    except ValueError:
                        pass


def run(args):
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("fusion Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    fuser   = TrackFuser(session, args.verbose)
    subs    = []

    print("Track fusion bridge started", flush=True)
    print("  Spatial threshold: {} NM".format(_SPATIAL_NM), flush=True)
    print("  Max track age:     {} s".format(_MAX_AGE_S), flush=True)
    print("  Position source:   {}".format("radar" if _RADAR_PREF else "ADS-B"), flush=True)

    for topic in _RADAR_TOPICS:
        subs.append(subscribe(session, topic, fuser.on_radar))
        print("  SUB radar: {}".format(topic), flush=True)
    for topic in _ADSB_TOPICS:
        subs.append(subscribe(session, topic, fuser.on_adsb))
        print("  SUB adsb:  {}".format(topic), flush=True)
    subs.append(subscribe(session, _NEPTUN_TOPIC, fuser.on_neptun))
    print("  SUB neptun: {}".format(_NEPTUN_TOPIC), flush=True)
    for domain in ("air", "land", "sea"):
        topic = "{}/{}/**".format(TOPIC_ROOT, domain)
        subs.append(subscribe(session, topic, fuser.on_any_track))
        print("  SUB twins:  {}".format(topic), flush=True)

    print("Fusion running — Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for sub in subs:
            sub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Multi-source track fusion bridge")
    ap.add_argument("--spatial-nm", type=float,
                    default=_SPATIAL_NM,
                    help="Spatial correlation threshold in NM (default {})".format(_SPATIAL_NM))
    ap.add_argument("--max-age", type=float,
                    default=_MAX_AGE_S,
                    help="Track cache age limit in seconds (default {})".format(_MAX_AGE_S))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
