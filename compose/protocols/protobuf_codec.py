"""Small reflection-based adapters for the JSON→Protobuf dual-publish seam."""

from __future__ import annotations


def source_track_to_message(message_class, track: dict):
    message = message_class()
    fields = message.DESCRIPTOR.fields_by_name
    for key, value in track.items():
        name = "timestamp" if key == "_ts" else "source" if key == "_src" else key
        if name not in fields or value is None:
            continue
        setattr(message, name, value)
    return message


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
