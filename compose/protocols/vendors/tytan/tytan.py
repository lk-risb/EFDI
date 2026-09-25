#!/usr/bin/env python3
"""tytan/synaps's interceptor status feed on the EFDI Backbone trial fabric
-> EFDI tracks.

Topic "<slot>/neura/effector/status/v1/*" (protobuf:tytan.synaps.c2.v1.
InterceptorStatus) — a counter-UAS/kinetic interceptor status feed: WGS84
location, ENU velocity, attitude, distance-to-target, remaining energy, and a
full engagement state machine (ARMED -> LIFTOFF -> LOITER -> ONBOARD ->
DETONATION/TERMINATION). This is a real, professionally-specified schema
(sanity-ceiling comments, a reserved field range for future growth) — unlike
the portal's shared "notional.tracks_demo.v1.Entity" schema seen on other
slots, this one is not demo/notional data.

The trailing "/*" in the topic means one sub-topic per interceptor id (e.g.
".../v1/interceptor_123"); bridges/backbone_bridge.py relays the full
original key unchanged, so this matches by substring rather than a fixed
topic suffix — see socbx.py/palantir.py for the fixed-suffix version of the
same pattern.

No affiliation field exists in the schema at all — an interceptor is, by
definition, a friendly countermeasure asset (a hostile system would not be
publishing its own kill-chain status to this fabric), so affiliation is
hardcoded "friendly" rather than guessed from payload content like the other
decoders do.

A DIFFERENT slot registered a second, richer tytan/synaps schema at
"<slot>/synaps/c2/client/v1/1" (protobuf:tytan.synaps.c2.v1.ClientMessage,
compose/schemas/vendors/tytan/c2.proto) — the full bidirectional C2 protocol,
not just interceptor status. Two of its oneof variants carry a position:

  TargetTrack — the threat being engaged (TargetType: FPV/Shahed/rocket/
    scout/bird). This is the picture the target is hostile *to* the friendly
    interceptor fleet, so — same reasoning as the interceptor's own hardcoded
    "friendly" — affiliation is hardcoded "hostile" here: a target track is
    by definition the thing an interceptor is engaging.

  InterceptorTrack — the same interceptor's own position, but from the
    ClientMessage side of the protocol rather than InterceptorStatus's
    ServerMessage side; also "friendly".

Neither carries a heading/speed field directly, only an ENU velocity vector
(east/north/up, m/s) — converted to speed/heading_deg here the same way any
ENU-to-compass conversion works (atan2(east, north)).
"""

from __future__ import annotations

import json
import math
import time

from google.protobuf.message import DecodeError
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe
from schemas.vendors.tytan.c2_pb2 import ClientMessage, TargetType
from schemas.vendors.tytan.interceptor_status_pb2 import InterceptorState, InterceptorStatus

_STATUS_TOPIC_MARKER = "/neura/effector/status/v1/"
_C2_TOPIC_MARKER = "/synaps/c2/client/v1/"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"


def _state_name(state: int) -> str:
    try:
        return InterceptorState.Name(state)
    except ValueError:
        return "INTERCEPTOR_STATE_UNKNOWN"


def status_record(payload: bytes) -> dict | None:
    msg = InterceptorStatus()
    msg.ParseFromString(payload)
    if not msg.HasField("location"):
        return None
    location = msg.location
    if location.latitude_deg == 0 and location.longitude_deg == 0:
        return None
    record = {
        "_ts": time.time(),
        "_src": "backbone:tytan:{}".format(msg.engagement_id or "unknown"),
        "uid": msg.interceptor_id or "tytan-interceptor",
        # WGS84's fields are protobuf `float` (32-bit), not `double` — round
        # off the resulting binary-fraction noise (e.g. 55.70000076293945)
        # rather than publish it verbatim.
        "lat_deg": round(location.latitude_deg, 6),
        "lon_deg": round(location.longitude_deg, 6),
        "alt_m": round(location.altitude_msl_m, 1),
        "label": msg.interceptor_id or "interceptor",
        "state": _state_name(msg.state),
    }
    if msg.engagement_id:
        record["engagement_id"] = msg.engagement_id
    if msg.course_degree:
        record["heading_deg"] = msg.course_degree
    if msg.ground_speed_m_s:
        record["speed_mps"] = msg.ground_speed_m_s
    if msg.distance_to_target_m:
        record["distance_to_target_m"] = msg.distance_to_target_m
    if msg.remaining_energy_percentage:
        record["remaining_energy_pct"] = msg.remaining_energy_percentage
    if msg.warning_flags:
        record["warning_flags"] = msg.warning_flags
    return record


def _enu_speed_heading(enu) -> tuple[float, float] | None:
    if enu.east == 0 and enu.north == 0 and enu.up == 0:
        return None
    speed = math.sqrt(enu.east ** 2 + enu.north ** 2 + enu.up ** 2)
    heading = math.degrees(math.atan2(enu.east, enu.north)) % 360
    return speed, heading


def client_message_record(payload: bytes) -> tuple[dict, str] | None:
    """Returns (record, affiliation) for whichever oneof variant carries a
    position, or None for every other ClientMessage kind (time sync,
    heartbeat, engagement request/abort — none of those are map data)."""
    msg = ClientMessage()
    msg.ParseFromString(payload)
    kind = msg.WhichOneof("payload")

    if kind == "target_track":
        track = msg.target_track
        if not track.HasField("location"):
            return None
        location = track.location
        if location.latitude_deg == 0 and location.longitude_deg == 0:
            return None
        record = {
            "_ts": time.time(),
            "_src": "backbone:tytan:target:{}".format(track.target_id or "unknown"),
            "uid": track.target_id or "tytan-target",
            "lat_deg": round(location.latitude_deg, 6),
            "lon_deg": round(location.longitude_deg, 6),
            "alt_m": round(location.altitude_msl_m, 1),
            "label": track.target_id or "target",
            "target_type": _target_type_name(track.type),
            "auto_engage": track.auto_engage,
            "manual_engagement_allowed": track.manual_engagement_allowed,
        }
        if track.recommended_effector_id:
            record["recommended_effector_id"] = track.recommended_effector_id
        if track.HasField("velocity_m_s"):
            speed_heading = _enu_speed_heading(track.velocity_m_s)
            if speed_heading:
                record["speed_mps"], record["heading_deg"] = speed_heading
        return record, "hostile"

    if kind == "interceptor_track":
        track = msg.interceptor_track
        if not track.HasField("location"):
            return None
        location = track.location
        if location.latitude_deg == 0 and location.longitude_deg == 0:
            return None
        record = {
            "_ts": time.time(),
            "_src": "backbone:tytan:interceptor-track:{}".format(track.interceptor_id or "unknown"),
            "uid": track.interceptor_id or "tytan-interceptor",
            "lat_deg": round(location.latitude_deg, 6),
            "lon_deg": round(location.longitude_deg, 6),
            "alt_m": round(location.altitude_msl_m, 1),
            "label": track.interceptor_id or "interceptor",
        }
        if track.HasField("velocity_m_s"):
            speed_heading = _enu_speed_heading(track.velocity_m_s)
            if speed_heading:
                record["speed_mps"], record["heading_deg"] = speed_heading
        return record, "friendly"

    return None


def _target_type_name(value: int) -> str:
    try:
        return TargetType.Name(value)
    except ValueError:
        return "TARGET_TYPE_UNKNOWN"


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("tytan: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        try:
            if _STATUS_TOPIC_MARKER in key:
                record = status_record(payload_bytes(sample))
                if record:
                    topic = TOPIC_ROOT + "/air/backbone/friendly/unit/tracks/v1"
                    session.put(topic, json.dumps(record).encode())
            elif _C2_TOPIC_MARKER in key:
                result = client_message_record(payload_bytes(sample))
                if result:
                    record, affiliation = result
                    topic = "{}/air/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, affiliation)
                    session.put(topic, json.dumps(record).encode())
        except DecodeError as exc:
            print("tytan decode error on {}: {}".format(key, exc), flush=True)
        except Exception as exc:
            print("tytan error on {}: {}".format(key, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("tytan: {} -> tracks (neura/effector/status/v1/*, synaps/c2/client/v1/*)".format(INPUT_TOPIC), flush=True)
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
