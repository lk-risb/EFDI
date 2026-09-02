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

from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, publish_dual, subscribe
from protocols.proto.aartos_json_pb2 import AartosTrack

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

# trackID -> the exact topic its last live publish went to, kept alongside
# _seen. A tombstone must reach the same tak_layer.py subscription the live
# track did (each affiliation/domain combination is its own separate Zenoh
# subscription there) — publishing every tombstone to one fixed topic meant
# a hostile or friendly track's delete never reached its own subscriber
# and only ever cleared an "unknown" one, leaving expired hostile/friendly
# markers stuck on the map until their stale timer ran out.
_last_topic: dict[str, dict[str, str]] = {}

# categoryName values that identify the drone OPERATOR/controller position
# (RF/WiFi direction-finding of the control link) rather than the airframe
# itself — seen in this deployment's own /dronesdb category list, alongside
# airframe types like Drone/Fixedwing/Glider. These render as a ground unit,
# not a UAV, using the same alertLevel-derived affiliation as the drone.
# Matched case-insensitively — /dronesdb lists them capitalized ("WLAN",
# "Remote", "POA") but this set stays lowercase.
_OPERATOR_CATEGORIES = {"wlan", "remote", "poa"}


def _is_operator_track(tracking: dict) -> bool:
    return str(tracking.get("categoryName") or "").strip().lower() in _OPERATOR_CATEGORIES


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
        "object_class": "unit" if _is_operator_track(tracking) else "uav",
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
    # predxpos/predypos are local ENU offsets in meters (same family as
    # xpos/ypos/zpos, all 0 for a stationary reference), NOT degrees — a
    # prior version of this decoder mislabeled them as
    # aartos_predicted_lat_deg/lon_deg and published them as if they were
    # real coordinates. Left out entirely rather than mistranslated:
    # projecting them into real lat/lon would need the antenna's own
    # azimuth/heading, which this decoder does not currently derive.
    return track


def topic_for_track(track: dict) -> str:
    affiliation = _AFFILIATION.get(track.get("aartos_alert_level", "unknown"), "unknown")
    # An operator/controller position is a person standing on the ground, not
    # an airframe — route it onto tak_layer.py's land/**/{aff}/unit/** wildcard
    # instead of the air/**/{aff}/uav/** one every other AARTOS track uses.
    if track.get("object_class") == "unit":
        return "{}/land/aartos/{}/unit".format(TOPIC_ROOT, affiliation)
    return "{}/air/aartos/{}/uav".format(TOPIC_ROOT, affiliation)


def _site_key(antenna_id) -> str:
    return "aartos-{}".format(_key_segment(str(antenna_id)))


def _key_segment(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value) or "_"


# Each antenna in a Sample carries its own surveyed position — publish it as a
# radar/sensor-site marker the same way protocols/vendors/asterix/cat.py does
# for CAT-34, on the exact topic shape tak_layer.py's dedicated radar-site
# subscriber already listens on ("land/*/radar/neutral/radar/**"). Bare JSON,
# no dual protobuf view: that subscriber explicitly only reads the plain view
# (see make_radar_status_handler's _NON_JSON_VIEWS filter), and there is no
# per-vendor schema for this — matching cat.py's own field names is what lets
# it render with zero tak_layer.py changes.
def antenna_to_site(antenna: dict, ref_ts: float) -> dict | None:
    lat, lon = antenna.get("latitude"), antenna.get("longitude")
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    antenna_id = antenna.get("antennaID")
    if antenna_id is None:
        return None
    site = {
        "_ts": ref_ts,
        "_src": "AARTOS",
        "sensor_id": _site_key(antenna_id),
        "sensor_name": antenna.get("antennaName") or "AARTOS",
        "lat_deg": round(lat, 7),
        "lon_deg": round(lon, 7),
        # IsoLOG antennas are RF direction-finders — they listen, they never
        # transmit — so tak_layer.py renders them with the Direction Finding
        # icon rather than the emitting-radar one. Always true for AARTOS.
        "passive": True,
    }
    elevation = antenna.get("elevation")
    if elevation is not None:
        site["geo_alt_m"] = round(elevation, 2)
    return site


def site_topic(site: dict) -> str:
    return "{}/land/{}/radar/neutral/radar".format(TOPIC_ROOT, site["sensor_id"])


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
        data = payload.get("data") or {}
        trackings = data.get("trackings") or []
        now_ids: set[str] = set()

        for antenna in data.get("antennas") or []:
            site = antenna_to_site(antenna, ref_ts)
            if site is None:
                continue
            session.put(site_topic(site), json.dumps(site).encode(), encoding="application/json")

        last_topic = _last_topic.setdefault(host, {})

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
            topic = topic_for_track(track)
            publish_dual(session, topic, track, AartosTrack)
            last_topic[str(track_id)] = topic

        # Tombstone any previously-seen track (for this host) that dropped
        # out of this sample, so it does not linger as a ghost marker. Must
        # go to the SAME topic the track's live publishes used — tak_layer.py
        # holds one separate subscription per affiliation/domain, so a
        # tombstone sent anywhere else (e.g. a fixed "unknown/uav" regardless
        # of the track's real affiliation) never reaches the subscriber that
        # actually rendered the marker, and it never gets cleared.
        for gone_id in set(seen) - now_ids:
            tombstone = {
                "uid": "aartos-{}-{}".format(host, gone_id),
                "_ts": time.time(),
                "_delete": True,
            }
            topic = last_topic.pop(gone_id, "{}/air/aartos/unknown/uav".format(TOPIC_ROOT))
            session.put(topic, json.dumps(tombstone).encode(), encoding="application/json")

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
