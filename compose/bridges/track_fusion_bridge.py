#!/usr/bin/env python3
"""track_fusion_bridge.py — Multi-source track correlation and enrichment.

Subscribes to all air track topics in the EFDI Zenoh fabric, correlates
tracks from multiple sensors, and publishes enriched fused tracks.

Fusion strategy (in priority order):
  1. ICAO 24-bit key match (exact)
       Radar (CAT-48 Mode-S) already decodes ICAO hex from SSR responses.
       ADS-B sources (airplaneslive, ADSB.lol, CAT-21) provide the same key.
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

Fused tracks are published to:
  <ORG>/air/fused/<affiliation>/aircraft/tracks/v1
→ cot_layer.py picks these up and shows them in ATAK with full identity.

Non-correlated radar tracks (truly non-cooperative, no ID possible) are
re-published as-is to:
  <ORG>/air/fused/unknown/aircraft/tracks/v1
so they still appear in ATAK but without enrichment.

Config (compose/.env):
  FUSION_SPATIAL_NM=2.0    Max distance for spatial match (default 2 NM)
  FUSION_MAX_AGE_S=60      Drop tracks older than this from the cache
  FUSION_RADAR_PREF=1      Set 0 to prefer ADS-B position (e.g. for GPS accuracy)

Run:
  venv/bin/python3 bridges/track_fusion_bridge.py
  venv/bin/python3 bridges/track_fusion_bridge.py --verbose
"""

import argparse
import json
import math
import os
import threading
import time

import zenoh
from google.protobuf.message import DecodeError
from zenoh_auth import apply_zenoh_auth
from namespace_prefix import topic_root
from protocols.random.adsblol_track_pb2 import AdsbLolTrack
from protocols.random.airplaneslive_track_pb2 import AirplanesLiveTrack
from protocols.protobuf_codec import source_message_to_track
from protocols.random.normalized_track_pb2 import NormalizedTrack
from protocols.protobuf_codec import normalized_track_message

ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

_SPATIAL_NM      = float(os.environ.get("FUSION_SPATIAL_NM",  "2.0"))
_MAX_AGE_S       = float(os.environ.get("FUSION_MAX_AGE_S",   "60"))
_RADAR_PREF      = os.environ.get("FUSION_RADAR_PREF", "1") != "0"
_PROTOBUF_V2_CONSUME = os.environ.get("EFDI_PROTOBUF_V2_CONSUME", "1") != "0"

# Cross-radar handoff — PSR-only targets seen by multiple radars simultaneously
_HANDOFF_NM      = float(os.environ.get("FUSION_HANDOFF_NM",  "2.0"))  # spatial tolerance
_HANDOFF_HDG_TOL = 45.0   # heading difference tolerance in degrees

TOPIC_FUSED  = "{}/air/fused/{}/aircraft/tracks/v1"
TOPIC_FUSED_V2 = "{}/air/fused/{}/aircraft/tracks/v2"

# Topics we subscribe to as radar sources (primary: positional authority)
_RADAR_TOPICS = [
    "{}/air/asterix/cat48/**".format(TOPIC_ROOT),
    "{}/air/asterix/cat20/**".format(TOPIC_ROOT),
    "{}/air/link16/**".format(TOPIC_ROOT),
    "{}/air/stanag4586/**".format(TOPIC_ROOT),
    "{}/air/mavlink/**".format(TOPIC_ROOT),
]

# Topics we subscribe to as identity enrichment sources
_ADSB_TOPICS = ["{}/air/asterix/cat21/**".format(TOPIC_ROOT)]
if _PROTOBUF_V2_CONSUME:
    _ADSB_TOPICS.extend([
        "{}/air/airplaneslive/adsb/*/aircraft/tracks/v2".format(TOPIC_ROOT),
        "{}/air/adsblol/adsb/*/aircraft/tracks/v2".format(TOPIC_ROOT),
    ])
else:
    _ADSB_TOPICS.extend([
        "{}/air/airplaneslive/adsb/*/aircraft/tracks/v1".format(TOPIC_ROOT),
        "{}/air/adsblol/adsb/*/aircraft/tracks/v1".format(TOPIC_ROOT),
    ])

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


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


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
        # Periodic age-out: ensures _radar_by_icao is cleaned even when radar goes
        # silent (no on_radar() calls), so ADS-B fallback kicks in automatically.
        self._start_age_timer()

    # ------------------------------------------------------------------
    # Ingest handlers
    # ------------------------------------------------------------------

    def on_radar(self, sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, DecodeError):
            return
        topic = str(sample.key_expr)
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
                # Borrow the primary radar's radar_id so cot_layer produces the
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

    def on_adsb(self, sample):
        try:
            topic = str(sample.key_expr)
            if topic.endswith("/v2"):
                message = AirplanesLiveTrack() if "/airplaneslive/" in topic else AdsbLolTrack()
                message.ParseFromString(bytes(sample.payload))
                track = source_message_to_track(message)
            else:
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
            self._session.put(pub_topic, json.dumps(track).encode(),
                              encoding=zenoh.Encoding.APPLICATION_JSON)
            self._session.put(
                TOPIC_FUSED_V2.format(TOPIC_ROOT, affiliation),
                normalized_track_message(NormalizedTrack, track, affiliation).SerializeToString(),
                encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
            )
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

        # Matched tracks retain the ADS-B database's civil/military category;
        # cot_layer.py then applies the scenario ICAO affiliation classifier.
        # Unmatched radar-only contacts stay unknown.
        if adsb is None:
            aff_slot = "unknown"
        else:
            aff_slot = "mil" if adsb.get("is_military") else "civ"
        topic = TOPIC_FUSED.format(TOPIC_ROOT, aff_slot)
        self._session.put(topic, json.dumps(fused).encode(),
                          encoding=zenoh.Encoding.APPLICATION_JSON)
        self._session.put(
            TOPIC_FUSED_V2.format(TOPIC_ROOT, aff_slot),
            normalized_track_message(NormalizedTrack, fused, aff_slot).SerializeToString(),
            encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
        )
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
    session = zenoh.open(make_config())
    fuser   = TrackFuser(session, args.verbose)
    subs    = []

    print("Track fusion bridge started", flush=True)
    print("  Spatial threshold: {} NM".format(_SPATIAL_NM), flush=True)
    print("  Max track age:     {} s".format(_MAX_AGE_S), flush=True)
    print("  Position source:   {}".format("radar" if _RADAR_PREF else "ADS-B"), flush=True)

    for topic in _RADAR_TOPICS:
        subs.append(session.declare_subscriber(topic, fuser.on_radar))
        print("  SUB radar: {}".format(topic), flush=True)
    for topic in _ADSB_TOPICS:
        subs.append(session.declare_subscriber(topic, fuser.on_adsb))
        print("  SUB adsb:  {}".format(topic), flush=True)

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
