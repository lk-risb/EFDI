"""Small reflection-based adapters for the JSON→Protobuf dual-publish seam."""

from __future__ import annotations

import json


def source_track_to_message(message_class, track: dict):
    message = message_class()
    fields = message.DESCRIPTOR.fields_by_name
    for key, value in track.items():
        name = "timestamp" if key == "_ts" else "source" if key == "_src" else key
        if name not in fields or value is None:
            continue
        try:
            setattr(message, name, value)
        except (TypeError, ValueError):
            # Decoders emit richer values than the flat contract models (nested
            # dicts, lists, out-of-range numbers). Skip those rather than fail
            # the whole message — the JSON view still carries them.
            continue
    return message


def dual_topic(topic: str) -> str:
    """Protobuf sibling of a JSON topic (.../v1 -> .../v2)."""
    if topic.endswith("/v1"):
        return topic[: -len("/v1")] + "/v2"
    return topic + "/v2"


def wrapped_track_message(
    message_class,
    track: dict,
    affiliation: str = "unknown",
    wrapper_field: str = "track",
):
    """Build a protocol wrapper message with a nested NormalizedTrack and the
    protocol-specific scalar fields alongside it.

    Every per-protocol contract in compose/protocols/*.proto follows this shape
    (see mavlink.proto, vmf.proto, sapient_flex335.proto). Some contracts use
    a different nested field name such as `sensor` or `normalized`; callers can
    override `wrapper_field` for those cases.
    """
    from protocols.normalized_track_pb2 import NormalizedTrack

    message = message_class()
    getattr(message, wrapper_field).CopyFrom(
        normalized_track_message(NormalizedTrack, track, affiliation)
    )
    fields = message.DESCRIPTOR.fields_by_name
    for key, value in track.items():
        if key == wrapper_field or key not in fields or value is None:
            continue
        try:
            setattr(message, key, value)
        except (TypeError, ValueError):
            continue
    return message


def publish_dual(
    session,
    topic: str,
    track: dict,
    message_class,
    zenoh,
    wrapper_field: str = "track",
) -> None:
    """Publish the flattened JSON view and the protobuf view side by side.

    JSON stays on the existing /v1 topic so current consumers are untouched;
    the protobuf sample goes to the /v2 sibling. Transitional — JSON is meant
    to be retired once consumers have moved to /v2.

    The protobuf leg is deliberately best-effort: a schema/among-field mismatch
    must never stop an already-working JSON publisher from delivering during
    the migration. Failures are printed, not raised.
    """
    session.put(
        topic,
        json.dumps(track, separators=(",", ":")).encode(),
        encoding=zenoh.Encoding.APPLICATION_JSON,
    )
    try:
        fields = message_class.DESCRIPTOR.fields_by_name
        if wrapper_field in fields and fields[wrapper_field].message_type is not None:
            message = wrapped_track_message(
                message_class,
                track,
                str(track.get("affiliation", "unknown")),
                wrapper_field=wrapper_field,
            )
        else:
            message = source_track_to_message(message_class, track)
        payload = message.SerializeToString()
    except Exception as exc:  # noqa: BLE001 — never break the JSON path
        print("protobuf encode failed for {}: {}".format(topic, exc), flush=True)
        return
    session.put(dual_topic(topic), payload, encoding=zenoh.Encoding.APPLICATION_PROTOBUF)


def source_message_to_track(message) -> dict:
    track = {}
    for field, value in message.ListFields():
        key = "_ts" if field.name == "timestamp" else "_src" if field.name == "source" else field.name
        track[key] = value
    return track


def normalized_track_message(message_class, track: dict, affiliation: str):
    message = message_class(
        timestamp=float(track.get("_ts", 0.0)),
        source=str(track.get("_src", "unknown")),
        uid=str(next((track.get(key) for key in ("icao24", "uid", "track_num", "radar_id", "mmsi") if track.get(key)), "unknown")),
        lat_deg=float(track["lat_deg"]),
        lon_deg=float(track["lon_deg"]),
        affiliation=affiliation,
    )
    direct = {"callsign": "callsign", "heading_deg": "heading_deg", "vertical_rate_ms": "vertical_rate_ms", "speed_ms": "speed_ms"}
    for target, source in direct.items():
        if source in track and track[source] is not None:
            setattr(message, target, track[source])
    if isinstance(track.get("on_ground"), bool):
        message.on_ground = track["on_ground"]
    if isinstance(track.get("emergency"), bool):
        message.emergency = track["emergency"]
    if "baro_alt_m" in track:
        message.baro_alt_m = float(track["baro_alt_m"])
    elif "alt_baro_ft" in track:
        message.baro_alt_m = float(track["alt_baro_ft"]) * 0.3048
    elif "alt_m" in track:
        message.baro_alt_m = float(track["alt_m"])
    if "geo_alt_m" in track:
        message.geo_alt_m = float(track["geo_alt_m"])
    elif "alt_geom_ft" in track:
        message.geo_alt_m = float(track["alt_geom_ft"]) * 0.3048
    if "speed_ms" not in track and "ground_speed_kts" in track:
        message.speed_ms = float(track["ground_speed_kts"]) * 0.514444
    if "heading_deg" not in track and "track_deg" in track:
        message.heading_deg = float(track["track_deg"])
    if "vertical_rate_ms" not in track and "baro_vr_fpm" in track:
        message.vertical_rate_ms = float(track["baro_vr_fpm"]) * 0.00508
    if "ias_kt" in track:
        message.airspeed_ms = float(track["ias_kt"]) * 0.514444
    for key in ("icao24", "registration", "aircraft_type", "squawk", "route", "radar_id"):
        value = track.get(key)
        if value not in (None, ""):
            message.source_metadata[key] = str(value)[:256]
    return message
