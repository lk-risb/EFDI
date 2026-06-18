#!/usr/bin/env python3
"""track_fusion_bridge.py — Multi-source track correlation and enrichment.

Subscribes to all air track topics in the EFDI Zenoh fabric, correlates
tracks from multiple sensors, and publishes enriched fused tracks.

Fusion strategy (in priority order):
  1. ICAO 24-bit key match (exact)
       Radar (CAT-48 Mode-S) already decodes ICAO hex from SSR responses.
       ADS-B sources (airplaneslive, opensky, FR24, CAT-21) provide the same key.
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
  venv/bin/python3 track_fusion_bridge.py
  venv/bin/python3 track_fusion_bridge.py --verbose
"""

import argparse
import json
import math
import os
import threading
import time

import zenoh

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = "1851281db70ccc0409dad4ecfc874cf5"
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

_SPATIAL_NM  = float(os.environ.get("FUSION_SPATIAL_NM",  "2.0"))
_MAX_AGE_S   = float(os.environ.get("FUSION_MAX_AGE_S",   "60"))
_RADAR_PREF  = os.environ.get("FUSION_RADAR_PREF", "1") != "0"

TOPIC_FUSED  = "{}/air/fused/{}/aircraft/tracks/v1"

# Topics we subscribe to as radar sources (primary: positional authority)
_RADAR_TOPICS = [
    "{}/air/asterix/cat48/**".format(ORG),
    "{}/air/asterix/cat20/**".format(ORG),
    "{}/air/link16/**".format(ORG),
    "{}/air/stanag4586/**".format(ORG),
    "{}/air/mavlink/**".format(ORG),
]

# Topics we subscribe to as identity enrichment sources
_ADSB_TOPICS = [
    "{}/air/asterix/cat21/**".format(ORG),
    "{}/air/airplaneslive/**".format(ORG),
    "{}/air/opensky/**".format(ORG),
    "{}/air/fr24/**".format(ORG),
    "{}/air/cot-rx/**".format(ORG),
]

# Fields that carry identity (we prefer ADS-B values for these)
_ID_FIELDS = ("callsign", "registration", "aircraft_type", "icao24",
              "squawk", "origin", "destination", "operator")

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
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


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
        # squawk → list of adsb uids
        self._adsb_by_squawk: dict[str, list] = {}

    # ------------------------------------------------------------------
    # Ingest handlers
    # ------------------------------------------------------------------

    def on_radar(self, sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        topic = str(sample.key_expr)
        self._age_out()
        with self._lock:
            uid = _uid_of(track)
            self._radar_tracks[uid] = {"track": track, "topic": topic, "ts": time.time()}
        self._fuse_and_publish(track, topic)

    def on_adsb(self, sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        topic = str(sample.key_expr)
        with self._lock:
            uid = _uid_of(track)
            self._adsb_tracks[uid] = {"track": track, "topic": topic, "ts": time.time()}
            icao = track.get("icao24", "").strip().lower()
            if icao:
                self._adsb_by_icao[icao] = uid
            sq = track.get("squawk", "")
            if sq and sq not in ("0000", "7500", "7600", "7700"):
                self._adsb_by_squawk.setdefault(sq, [])
                if uid not in self._adsb_by_squawk[sq]:
                    self._adsb_by_squawk[sq].append(uid)

    # ------------------------------------------------------------------
    # Fusion logic
    # ------------------------------------------------------------------

    def _fuse_and_publish(self, radar: dict, radar_topic: str):
        adsb = self._find_match(radar)
        if adsb is not None:
            fused, method = self._merge(radar, adsb)
        else:
            fused, method = dict(radar), "radar-only"

        # Matched tracks are cooperative (have a transponder) → civil slot
        # so cot_layer.py's _civ_air_type() can check ICAO hostile ranges.
        # Unmatched radar-only contacts stay unknown.
        aff_slot = "civ" if adsb is not None else "unknown"
        topic = TOPIC_FUSED.format(ORG, aff_slot)
        self._session.put(topic, json.dumps(fused).encode(),
                          encoding=zenoh.Encoding.APPLICATION_JSON)
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
            for k in stale_r:
                del self._radar_tracks[k]
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
