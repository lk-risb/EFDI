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
from protocols.track_views import add_version, semantic_topic

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

# Stable per-(host, _entity_kind) identity, so a churned AARTOS trackID
# keeps the SAME CoT uid instead of minting a fresh one. RTSA-Suite PRO
# reassigns a new trackID to the same physical drone almost every poll
# cycle; diffing raw trackIDs meant every reassignment forced a
# delete-then-recreate cycle in TAK, which raced against WinTAK's own CoT
# processing and could leave two or three markers visible for one real
# aircraft. Keying identity by _entity_kind ("uav" vs "unit") instead means
# the SAME marker just keeps updating in place across a trackID churn — no
# delete, no recreate, nothing for the client to race.
# host -> _entity_kind -> {"track_id": str, "uid": str, "last_seen": float}
_class_identity: dict[str, dict[str, dict]] = {}

# Seconds a class must be absent (using AARTOS's own sample timestamp, not
# wall-clock) before its identity is actually tombstoned. AARTOS's own
# detection is noisy enough to skip a class for one poll and pick it back up
# a second later even while the physical object never left; without this
# grace window that single-sample gap alone re-triggers the same
# delete/recreate churn the class-based identity exists to prevent.
_CLASS_GONE_GRACE_S = 5.0


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# A single-antenna bearing-only fix (no real cross-fix from a second
# antenna) appears to default to reporting that antenna's own surveyed
# coordinates rather than a real triangulated point — confirmed live: a
# stationary drone's reported position repeatedly landed within a few
# meters of whichever IsoLOG site last reported a usable bearing. A
# reported drone position this close to any antenna site in the SAME
# sample is almost certainly this degenerate fallback, not a real fix —
# publishing it would visibly snap the marker onto a radar site. Skipped
# instead of relayed, so the marker holds its last real position rather
# than jumping to wherever a single antenna happens to be.
_ANTENNA_FALLBACK_RADIUS_M = 40.0

# _entity_kind -> the exact topic its last live publish went to, kept
# alongside _class_identity. A tombstone must reach the same tak_layer.py
# subscription the live track did (each affiliation/domain combination is
# its own separate Zenoh subscription there) — publishing every tombstone
# to one fixed topic meant a hostile or friendly track's delete never
# reached its own subscriber and only ever cleared an "unknown" one,
# leaving expired hostile/friendly markers stuck on the map until their
# stale timer ran out.
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
        # Leading underscore keeps this out of track_views.object_type()'s
        # candidate-key scan — it already picks the topic's {entity} segment
        # below, and letting it double as {type} too produced .../uav/uav/
        # and .../unit/unit/ instead of a real type or the honest "unknown".
        "_entity_kind": "unit" if _is_operator_track(tracking) else "uav",
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
    """Semantic prefix (domain/source/modality/affiliation/entity) — see
    docs/13-topic-taxonomy.md. publish_dual() appends {type}/{id}/tracks/v1
    itself, so this only needs to reach up to {entity}.

    modality is `passive_rf`: AARTOS/IsoLOG antennas are RF direction-finders,
    never active radar — see docs/13-topic-taxonomy.md's modality vocabulary.
    """
    affiliation = _AFFILIATION.get(track.get("aartos_alert_level", "unknown"), "unknown")
    # An operator/controller position is a person standing on the ground, not
    # an airframe — route it onto tak_layer.py's land/**/{aff}/unit/** wildcard
    # instead of the air/**/{aff}/uav/** one every other AARTOS track uses.
    if track.get("_entity_kind") == "unit":
        return "{}/land/aartos/passive_rf/{}/unit".format(TOPIC_ROOT, affiliation)
    return "{}/air/aartos/passive_rf/{}/uav".format(TOPIC_ROOT, affiliation)


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
        # Also the site's {id} segment in site_topic() below (track_views'
        # object_id() checks "uid" first) — without it every site's {id}
        # fell back to "unknown", noise the source segment already disambiguates.
        "uid": _site_key(antenna_id),
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
    """Full taxonomy key, .../tracks/v1 (see docs/13-topic-taxonomy.md).

    modality stays the literal "radar" (not passive_rf) — that is the exact
    shape tak_layer.py's dedicated radar-site subscriber already listens on
    ("land/*/radar/neutral/radar/**"), matching every other sensor-site
    marker (e.g. protocols/vendors/asterix/cat.py's CAT-34 sensor status)
    regardless of the sensor's true modality. type resolves to "unknown"
    (a site has no vendor type); id resolves to the antenna's own sensor_id
    (carried as "uid" on the site dict), giving each antenna a distinct key
    instead of every site collapsing onto the same "unknown" id segment.
    """
    prefix = "{}/land/{}/radar/neutral/radar".format(TOPIC_ROOT, site["sensor_id"])
    return add_version(semantic_topic(prefix, site))


def run() -> None:
    session = open_session()

    def on_sample(sample) -> None:
        try:
            payload = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        key = str(sample.key_expr)
        host = key[len(RAW_PREFIX):] if key.startswith(RAW_PREFIX) else "unknown"
        class_identity = _class_identity.setdefault(host, {})

        ref_ts = payload.get("startTime") or time.time()
        data = payload.get("data") or {}
        trackings = data.get("trackings") or []
        now_classes: set[str] = set()

        antenna_positions: list[tuple[float, float]] = []
        for antenna in data.get("antennas") or []:
            site = antenna_to_site(antenna, ref_ts)
            if site is None:
                continue
            antenna_positions.append((site["lat_deg"], site["lon_deg"]))
            session.put(site_topic(site), json.dumps(site).encode(), encoding="application/json")

        last_topic = _last_topic.setdefault(host, {})

        for tracking in trackings:
            track_id = tracking.get("trackID")
            if track_id is None:
                continue
            if not tracking.get("trackValid", True):
                continue
            track = tracking_to_track(tracking, ref_ts)
            if track is None:
                continue
            cls = track.get("_entity_kind") or "uav"
            tid_str = str(track_id)
            lat, lon = track.get("lat_deg"), track.get("lon_deg")
            if lat is not None and lon is not None and any(
                    _distance_m(lat, lon, alat, alon) < _ANTENNA_FALLBACK_RADIUS_M
                    for alat, alon in antenna_positions):
                # Degenerate single-antenna fallback fix — keep the
                # identity alive (same bookkeeping a normal update would
                # do) so a filtered sample never tombstones it, but don't
                # publish this position.
                ident = class_identity.get(cls)
                if ident is None:
                    ident = {"track_id": tid_str, "uid": "aartos-{}-{}".format(host, tid_str)}
                    class_identity[cls] = ident
                ident["last_seen"] = ref_ts
                ident["track_id"] = tid_str
                now_classes.add(cls)
                continue
            ident = class_identity.get(cls)
            if ident is not None:
                # Same class already has a stable identity — reuse its uid
                # even if the raw trackID just churned to a new value, so
                # the marker keeps updating in place instead of being
                # deleted and recreated.
                uid = ident["uid"]
                ident["track_id"] = tid_str
            else:
                uid = "aartos-{}-{}".format(host, tid_str)
                ident = {"track_id": tid_str, "uid": uid}
                class_identity[cls] = ident
            ident["last_seen"] = ref_ts
            track["uid"] = uid
            topic = topic_for_track(track)
            publish_dual(session, topic, track, AartosTrack)
            last_topic[cls] = topic
            now_classes.add(cls)

        # Tombstone a class's stable identity only after it has been absent
        # for a full grace window, not the instant one sample is missing it.
        # AARTOS's own detection is noisy enough that a single poll can
        # briefly report zero trackings for a class that is still genuinely
        # present — tombstoning on that first miss just recreated the exact
        # delete/recreate churn this identity scheme exists to avoid, only
        # now triggered by a one-sample detection gap instead of a trackID
        # change. Must go to the SAME topic the identity's live publishes
        # used — tak_layer.py holds one separate subscription per
        # affiliation/domain, so a tombstone sent anywhere else (e.g. a
        # fixed "unknown/uav" regardless of the track's real affiliation)
        # never reaches the subscriber that actually rendered the marker,
        # and it never gets cleared.
        for cls in list(class_identity):
            if cls in now_classes:
                continue
            ident = class_identity[cls]
            if ref_ts - ident.get("last_seen", 0) < _CLASS_GONE_GRACE_S:
                continue
            class_identity.pop(cls)
            tombstone = {
                "uid": ident["uid"],
                "_ts": time.time(),
                "_delete": True,
            }
            # An identity that only ever matched the antenna-fallback filter
            # (never published a real position) has no last_topic entry —
            # the fallback below must still route by domain (unit -> land,
            # everything else -> air), not always assume "air/uav".
            default_topic = "{}/land/aartos/unknown/unit".format(TOPIC_ROOT) if cls == "unit" \
                else "{}/air/aartos/unknown/uav".format(TOPIC_ROOT)
            topic = last_topic.pop(cls, default_topic)
            session.put(topic, json.dumps(tombstone).encode(), encoding="application/json")

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("AARTOS translator: {} -> {}/air/aartos/passive_rf/*/uav".format(INPUT_TOPIC, TOPIC_ROOT), flush=True)
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
