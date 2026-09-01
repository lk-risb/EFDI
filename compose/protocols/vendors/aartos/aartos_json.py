#!/usr/bin/env python3
"""Aaronia AARTOS drone-tracking JSON on Zenoh -> normalized track records.

bridges/aartos_bridge.py lands raw HTTP-stream samples, verbatim, under
``.../raw/aartos/<host>``. This translator decodes the vendor's documented
schema (Aaronia's "Drone Tracking JSON Format": Sample -> TrackState ->
trackings[]) into one canonical EFDI track record per currently-tracked
drone — same "one dedicated decoder per vendor" shape as
protocols/vendors/sapient/flex335.py, since (unlike MQTT) there is exactly
one decode target for this raw feed, not several sharing a transport.

Each finished track publishes to ``air/aartos/<affiliation>/uav``, matching
tak_layer.py's existing wildcard subscriptions ("air/**/hostile/uav/**" etc)
— no tak_layer.py change is needed for this vendor to render on the map.
Affiliation is derived straight from AARTOS's own alertLevel.

A drone that drops out of one sample's trackings[] gets a one-shot _delete
tombstone, tracked per source host so two independent RTSA-Suite instances
never cross-contaminate each other's track-ID space.
"""

from __future__ import annotations

import json
import math
import os
import time

from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

INPUT_TOPIC = os.environ.get("AARTOS_INPUT_TOPIC") or TOPIC_ROOT + "/raw/aartos/**"
RAW_PREFIX = TOPIC_ROOT + "/raw/aartos/"

# alertLevel (vendor's own threat classification) -> the affiliation segment
# tak_layer.py's _TOPIC_COT wildcards already key off of.
_AFFILIATION = {
    "friendly": "friendly",
    "warning": "hostile",
    "defend": "hostile",
    "panic": "hostile",
    "unknown": "unknown",
    "info": "unknown",
    "ignore": "unknown",
}

# trackID -> last-seen epoch, kept separately per source host so two
# independent RTSA-Suite instances never tombstone each other's tracks.
_seen: dict[str, dict[str, float]] = {}


def _velocity(tracking: dict) -> tuple[float | None, float | None]:
    """(speed_ms, heading_deg) from AARTOS's ENU (xyz)velocity, or (None, None)."""
    if not tracking.get("velocityValid"):
        return None, None
    east = tracking.get("xvelocity")
    north = tracking.get("yvelocity")
    if east is None or north is None:
        return None, None
    speed = math.hypot(east, north)
    heading = math.degrees(math.atan2(east, north)) % 360.0
    return round(speed, 3), round(heading, 2)


def tracking_to_track(tracking: dict, ref_ts: float) -> dict | None:
    """Map one AARTOS Tracking entry onto the fabric's canonical track dict.

    Position comes straight from the vendor's own lat/lon (already
    triangulated or projected by RTSA-Suite PRO) — unlike SAPIENT's
    range/bearing, no re-projection is needed here.
    """
    if not tracking.get("positionValid"):
        return None
    lat, lon = tracking.get("latitude"), tracking.get("longitude")
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    track_id = tracking.get("trackID")
    alert = str(tracking.get("alertLevel") or "unknown").lower()
    track = {
        "_ts": ref_ts,
        "_src": "AARTOS",
        "uid": "aartos-{}".format(track_id),
        "lat_deg": round(lat, 7),
        "lon_deg": round(lon, 7),
        "callsign": tracking.get("droneName") or tracking.get("categoryName") or "AARTOS-{}".format(track_id),
        "aartos_category": tracking.get("categoryName"),
        "aartos_alert_level": alert,
        "aartos_probability": tracking.get("probability"),
        "aartos_zone_ids": tracking.get("zoneIDs") or None,
    }
    elevation = tracking.get("elevation")
    if elevation is not None:
        track["geo_alt_m"] = round(elevation, 2)
    speed, heading = _velocity(tracking)
    if speed is not None:
        track["speed_ms"] = speed
        track["heading_deg"] = heading
    pred_lat, pred_lon = tracking.get("predxpos"), tracking.get("predypos")
    if pred_lat is not None and pred_lon is not None:
        track["aartos_predicted_lat_deg"] = pred_lat
        track["aartos_predicted_lon_deg"] = pred_lon
    return track


def topic_for_track(track: dict) -> str:
    affiliation = _AFFILIATION.get(track.get("aartos_alert_level", "unknown"), "unknown")
    return "{}/air/aartos/{}/uav".format(TOPIC_ROOT, affiliation)


def run() -> None:
    session = open_session()

    def on_sample(sample) -> None:
        try:
            payload = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        key = str(sample.key_expr)
        host = key[len(RAW_PREFIX):] if key.startswith(RAW_PREFIX) else "unknown"
        seen = _seen.setdefault(host, {})

        ref_ts = payload.get("startTime") or time.time()
        trackings = ((payload.get("data") or {}).get("trackings")) or []
        now_ids: set[str] = set()

        for tracking in trackings:
            track_id = tracking.get("trackID")
            if track_id is None:
                continue
            now_ids.add(str(track_id))
            if not tracking.get("trackValid", True):
                continue
            track = tracking_to_track(tracking, ref_ts)
            if track is None:
                continue
            track["uid"] = "aartos-{}-{}".format(host, track_id)
            session.put(topic_for_track(track), json.dumps(track).encode(), encoding="application/json")

        # Tombstone any previously-seen track (for this host) that dropped
        # out of this sample, so it does not linger as a ghost marker.
        for gone_id in set(seen) - now_ids:
            tombstone = {
                "uid": "aartos-{}-{}".format(host, gone_id),
                "_ts": time.time(),
                "_delete": True,
            }
            session.put("{}/air/aartos/unknown/uav".format(TOPIC_ROOT),
                        json.dumps(tombstone).encode(), encoding="application/json")

        seen.clear()
        seen.update({tid: time.time() for tid in now_ids})

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("AARTOS translator: {} -> {}/air/aartos/*/uav".format(INPUT_TOPIC, TOPIC_ROOT), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    run()
