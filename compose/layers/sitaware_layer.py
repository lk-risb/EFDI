#!/usr/bin/env python3
"""sitaware_layer.py — Zenoh EFDI track topics → NVG 2.0.2 feed for SitaWare.

Converts live EFDI tracks to NVG items (APP-6 / 2525 symbol codes, position,
modifiers, extended data) and serves them as one NVG document over HTTP(S).

This is the C2 EGRESS for SitaWare: a _layer writes out to a C2 system. That
SitaWare initiates the transfer — HQ 6.22 polls this endpoint via an "NVG
Import Subscription" — is a property of the transport, not of the direction the
data travels. Ingest from SitaWare's REST API belongs in bridges/sitaware_bridge.py;
there is no separate NVG-XML ingest bridge — one dialect in, one dialect out.

Required configuration:

    SITAWARE_HQ_NVG_USER=<feed-user>
    SITAWARE_HQ_NVG_PASS=<password>

For a remote HQ host, either configure TLS or explicitly acknowledge an
isolated lab-only HTTP connection:

    SITAWARE_HQ_NVG_BIND=0.0.0.0
    SITAWARE_HQ_NVG_PORT=8088
    SITAWARE_HQ_NVG_TLS_CERT=/path/to/server-cert.pem
    SITAWARE_HQ_NVG_TLS_KEY=/path/to/server-key.pem

The SitaWare subscription URL is then https://<efdi-host>:8088/nvg.
"""

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import ssl
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from layers.tak_layer import (
    _build_remarks,
    _callsign as _cot_callsign,
    _course,
    _is_adsb_surface_vehicle,
    _is_unfused_sensor_track,
    _speed_ms,
    _uid as _cot_uid,
)
from namespace_prefix import topic_root
from protocols.gateway import open_session, subscribe

TOPIC_ROOT = topic_root()

NVG_NS      = "https://tide.act.nato.int/schemas/2012/10/nvg"
NVG_VERSION = "2.0.2"
REFRESH_S   = 10    # re-PUT all live tracks at this interval
STALE_S     = 120   # delete tracks older than this
ZENOH_RETRY_S = 5

# APP-6(B) SIDC codes — keyed by new schema wildcard patterns. `civ`/`mil`
# affiliation means "civilian traffic"/"military traffic" with no posture
# judgment implied, so these render as the neutral affiliation letter (N)
# unconditionally; a real hostile/friendly/neutral classification comes from
# the actual data (SAPIENT classification, IFF, an explicit source
# affiliation field) via the dedicated topic entries, not from nationality.
# ** matches zero-or-more Zenoh path segments.
def _civil_air_sidc(track: dict) -> str:
    if _is_adsb_surface_vehicle(track):
        return "SNGPEV----*****"
    return "SNAPCF----*****"


def _military_air_sidc(track: dict) -> str:
    if _is_adsb_surface_vehicle(track):
        return "SNGPEV----*****"
    return "SNAPMF----*****"


def _civil_sea_sidc(track: dict) -> str:
    return "SNSPXF----*****"


def _unknown_air_sidc(track: dict) -> str:
    object_class = str(
        track.get("sapient_class")
        or track.get("remote_id_ua_type")
        or track.get("utm_vehicle_type")
        or ""
    ).lower()
    if any(token in object_class for token in ("drone", "uas", "uav", "quadcopter")):
        return "SUAPMFQ---*****"
    return "SUAPMF----*****"


def _resolve_sidc(resolver, track: dict) -> str:
    return resolver(track) if callable(resolver) else resolver


# Views carrying the same object in a non-JSON encoding. Anything else,
# including a bare topic with no view suffix, is the flat JSON payload.
_NON_JSON_VIEWS = frozenset({"sapient", "proto", "raw"})

_TOPIC_SIDC = {
    "air/**/civ/aircraft/**":        _civil_air_sidc,
    "air/**/mil/aircraft/**":        _military_air_sidc,
    "air/**/friendly/aircraft/**":   "SFAPMF----*****",
    "air/**/friendly/uav/**":        "SFAPMFQ---*****",
    "air/**/hostile/aircraft/**":    "SHAPMF----*****",
    # Mirrors friendly/uav. Without it a hostile UAV reached TAK but was dropped
    # from the NVG feed entirely, so the two C2 systems disagreed on whether the
    # track existed at all.
    "air/**/hostile/uav/**":         "SHAPMFQ---*****",
    "air/**/neutral/aircraft/**":    "SNAPMF----*****",
    "air/**/unknown/**":             _unknown_air_sidc,
    # Equipment/Vehicle/Civilian — NOT "UCV", which is Unit/Combat/Aviation and
    # renders every civilian car as a rotary-wing aviation unit. Matches the
    # a-f-G-E-V-C that tak_layer sends for the same track.
    "land/**/civ/vehicle/**":        "SFGPEVC---*****",  # Friendly Civilian Vehicle
    "land/**/neutral/station/**":    "SNGPES----*****",  # Neutral Ground Sensor (HQ-supported)
    "land/**/neutral/sensor/**":     "SNGPES----*****",  # Neutral Ground Sensor
    "land/**/neutral/radar/**":      "SNGPESR---*****",  # Neutral Ground Radar
    "land/**/friendly/unit/**":      "SFGPU-----*****",  # Friendly Ground Unit (NFFI)
    "land/**/hostile/unit/**":       "SHGPU-----*****",
    "land/**/neutral/unit/**":       "SNGPU-----*****",
    "land/**/unknown/unit/**":       "SUGPU-----*****",
    "land/**/unknown/vehicle/**":    "SUGPEV----*****",
    "land/**/unknown/person/**":     "SUGPUCI---*****",
    "land/**/unknown/sensor/**":     "SUGPES----*****",
    "land/**/neutral/zone/**":       "SNGPES----*****",
    "land/**/neutral/alert/**":      "SNGPES----*****",
    "sea/**/civ/vessel/**":          _civil_sea_sidc,
    "sea/**/mil/vessel/**":          "SNSPXF----*****",  # Neutral Sea Surface (military)
    "sea/**/friendly/vessel/**":     "SFSPXF----*****",
    "sea/**/hostile/vessel/**":      "SHSPXF----*****",
    "sea/**/neutral/vessel/**":      "SNSPXF----*****",
    "sea/**/unknown/vessel/**":      "SUSPXF----*****",
    "space/**/civ/satellite/**":     "SFPP------*****",  # Friendly Space (satellite)
    "space/**/friendly/satellite/**":"SFPP------*****",
    "space/**/hostile/satellite/**": "SHPP------*****",
    "space/**/neutral/satellite/**": "SNPP------*****",
    "space/**/unknown/satellite/**": "SUPP------*****",
    # Neutral emplaced sensor: supported by HQ 6.22, appropriate for a fixed
    # measuring device, and distinct from the generic dronuradaras sensor. The
    # standards-native METOC scheme renders as Unknown in this HQ release.
    "env/weather/station/**":        "SNGPESE---*****",
}


# ---------------------------------------------------------------------------
# Zenoh config
# ---------------------------------------------------------------------------

def _uid(track: dict) -> str:
    return _cot_uid(track)


def _callsign(track: dict, uid: str) -> str:
    return _cot_callsign(track, uid)


def _cot_type_for_sidc(sidc: str) -> str:
    affiliation = {"F": "f", "H": "h", "N": "n", "U": "u"}.get(sidc[1:2], "u")
    dimension = sidc[2:3] if sidc[2:3] in {"A", "G", "S", "P"} else "G"
    return "a-{}-{}".format(affiliation, dimension)


def _iso_timestamp(value: object) -> str | None:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric_value):
        return None
    try:
        return (
            datetime.fromtimestamp(numeric_value, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _timestamp(track: dict) -> str | None:
    return _iso_timestamp(track.get("_ts"))


def _xml_safe_text(value: object, limit: int) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = "".join(
        char
        for char in str(value)
        if char in "\t\n\r"
        or "\x20" <= char <= "\ud7ff"
        or "\ue000" <= char <= "\ufffd"
    )
    return text[:limit] if text else None


def _nvg_text(value: object, limit: int = 256) -> str | None:
    text = _xml_safe_text(value, limit)
    return " ".join(text.split()) if text else None


def _finite_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _metadata_text(value: object, limit: int = 256) -> str | None:
    if isinstance(value, (list, tuple)):
        values = [_nvg_text(item, 64) for item in value]
        return _nvg_text(", ".join(item for item in values if item), limit)
    return _nvg_text(value, limit)


def _primary_altitude(track: dict) -> tuple[float, str] | None:
    for key, scale, label in (
        ("geo_alt_m", 1.0, "geometric WGS84"),
        ("alt_geom_ft", 0.3048, "geometric WGS84"),
        ("baro_alt_m", 1.0, "barometric"),
        ("alt_baro_ft", 0.3048, "barometric"),
        ("alt_3d_ft", 0.3048, "radar 3D"),
        ("alt_ft", 0.3048, "reported"),
        ("alt_m", 1.0, "reported"),
        ("alt_km", 1000.0, "reported"),
    ):
        number = _finite_number(track.get(key))
        if number is not None:
            return round(number * scale, 1), label
    return None


def _altitude_from(track: dict, fields: tuple[tuple[str, float], ...]) -> float | None:
    for key, scale in fields:
        number = _finite_number(track.get(key))
        if number is not None:
            return round(number * scale, 1)
    return None


def _barometric_altitude_m(track: dict) -> float | None:
    return _altitude_from(track, (
        ("baro_alt_m", 1.0),
        ("alt_baro_ft", 0.3048),
        ("alt_ft", 0.3048),
    ))


def _geometric_altitude_m(track: dict) -> float | None:
    return _altitude_from(track, (
        ("geo_alt_m", 1.0),
        ("alt_geom_ft", 0.3048),
    ))


def _altitude_modifier_value(track: dict) -> str | None:
    geometric_m = _geometric_altitude_m(track)
    barometric_m = _barometric_altitude_m(track)
    if geometric_m is not None and barometric_m is not None:
        geometric_ft = geometric_m / 0.3048
        barometric_ft = barometric_m / 0.3048
        return "GEO {} ft | {} m | BARO FL{:03d} | {} ft".format(
            round(geometric_ft),
            round(geometric_m),
            round(barometric_ft / 100),
            round(barometric_ft),
        )
    if barometric_m is not None:
        altitude_ft = barometric_m / 0.3048
        return "FL{:03d} | {} ft | {} m | barometric".format(
            round(altitude_ft / 100), round(altitude_ft), round(barometric_m)
        )
    primary = _primary_altitude(track)
    if primary:
        altitude_m, source = primary
        return "{} ft | {} m | {}".format(
            round(altitude_m / 0.3048), round(altitude_m), source
        )
    return None


_CARD_LABELS = {
    "CS (callsign)": "Callsign",
    "REG (registration)": "Registration",
    "ICAO (hex address)": "ICAO address",
    "FLAG (country)": "Country",
    "OPR (operator)": "Operator",
    "MODE 3 (squawk)": "Squawk / Mode 3",
    "TIME": "Observation",
    "LAT (latitude)": "Latitude",
    "LON (longitude)": "Longitude",
    "HDG (heading)": "Heading",
    "ALT (Altitude)": "Altitude",
    "V/S (vertical speed)": "Vertical speed",
    "V/S (geometric vertical speed)": "Geometric vertical speed",
    "SPD (speed)": "Speed",
    "GS (ground speed)": "Ground speed",
    "SRC (data source)": "Data source",
    "RDR (radar ID)": "Radar ID",
    "SITE (SAC/SIC)": "SAC / SIC",
    "RNG (range)": "Range / azimuth",
    "RSSI (signal strength)": "Signal strength",
    "ACC (position accuracy)": "Position accuracy",
    "ADS-B ACC (ADS-B accuracy)": "ADS-B accuracy",
    "EMITTER (aircraft category)": "Emitter category",
    "TAS (true airspeed)": "True airspeed",
    "IAS (indicated airspeed)": "Indicated airspeed",
    "MACH (Mach number)": "Mach",
    "MAG HDG (magnetic heading)": "Magnetic heading",
    "SEL ALT (selected altitude)": "Selected altitude",
    "FINAL ALT (target altitude)": "Target altitude",
    "TURN RATE": "Turn rate",
    "TYPE (aircraft)": "Aircraft type",
    "CALL": "Callsign",
    "STATION": "Station",
    "LAT": "Latitude",
    "LON": "Longitude",
    "TEMP": "Temperature",
    "HUMIDITY": "Humidity",
    "WIND": "Wind",
    "PRESSURE": "Pressure",
    "CLOUD": "Cloud cover",
    "PRECIP": "Precipitation",
    "SRC": "Data source",
}


def _format_altitude(altitude_m: float, flight_level: bool = False) -> str:
    altitude_ft = altitude_m / 0.3048
    prefix = "FL{:03d} / ".format(round(altitude_ft / 100)) if flight_level else ""
    return "{}{} ft / {} m".format(prefix, round(altitude_ft), round(altitude_m))


def _format_vertical_rate_fpm(value: object) -> str | None:
    rate_fpm = _finite_number(value)
    if rate_fpm is None:
        return None
    return "{:+.0f} ft/min / {:+.1f} m/s".format(rate_fpm, rate_fpm / 196.85)


def _nvg_extended_data(track: dict, uid: str, sidc: str) -> list[tuple[str, str]]:
    """Build an operator-facing NVG attribute card, aligned with TAK remarks."""
    result: list[tuple[str, str]] = []
    used_keys: dict[str, int] = {}
    seen_rows: set[tuple[str, str]] = set()

    def add(key: str, value: object, limit: int = 512) -> None:
        if len(result) >= 96:
            return
        clean_key = _nvg_text(key, 96)
        clean_value = _metadata_text(value, limit)
        if not clean_key or clean_value is None:
            return
        row = (clean_key, clean_value)
        if row in seen_rows:
            return
        seen_rows.add(row)
        count = used_keys.get(clean_key, 0) + 1
        used_keys[clean_key] = count
        if count > 1:
            clean_key = "{} ({})".format(clean_key, count)
        result.append((clean_key, clean_value))

    def section(title: str) -> None:
        add(title, "────────────")

    # Reuse the domain-aware stat-card formatter used by TAK. This preserves
    # the detailed CAT-34/48/62 radar, IFF, flight-plan, maritime, sensor and
    # weather information instead of maintaining a second divergent field map.
    has_altitude_detail = _primary_altitude(track) is not None
    remarks = _build_remarks(track, _cot_type_for_sidc(sidc))
    for line in remarks.splitlines():
        if line.startswith("─── ") and line.endswith(" ───"):
            section(line[4:-4].strip())
            continue
        if ": " in line:
            label, value = line.split(": ", 1)
            card_label = _CARD_LABELS.get(label, label)
            # The TAK formatter exposes one generic altitude. The dedicated NVG
            # block below distinguishes pressure flight level from geometric z,
            # so retaining both rows would be redundant and potentially unclear.
            if card_label == "Altitude" and has_altitude_detail:
                continue
            add(card_label, value)
        elif line.strip():
            add("Status", line.strip("[] "))

    # The TAK card selects one primary altitude. NVG can show all independent
    # readings, so add a compact altitude block without conflating geometric
    # altitude with pressure flight level.
    altitude_rows: list[tuple[str, object]] = []
    geometric_m = _geometric_altitude_m(track)
    barometric_m = _barometric_altitude_m(track)
    primary = _primary_altitude(track)
    if primary and primary[1] not in {"barometric", "geometric WGS84"}:
        altitude_rows.append(("Primary", "{} ({})".format(
            _format_altitude(primary[0]), primary[1]
        )))
    if barometric_m is not None:
        altitude_rows.append(("Barometric", _format_altitude(barometric_m, True)))
    if geometric_m is not None:
        altitude_rows.append(("Geometric WGS84", _format_altitude(geometric_m)))
    for key, label, scale in (
        ("alt_3d_ft", "Radar 3D", 0.3048),
        ("mode_c_alt_ft", "Mode C", 0.3048),
        ("measured_alt_ft", "Measured", 0.3048),
        ("meas_alt_ft", "Measured", 0.3048),
        ("calc_alt_ft", "Calculated", 0.3048),
        ("selected_alt_ft", "Selected MCP/FCU", 0.3048),
        ("fms_selected_alt_ft", "Selected FMS", 0.3048),
        ("final_alt_ft", "Target / final", 0.3048),
    ):
        value = _finite_number(track.get(key))
        if value is not None:
            altitude_rows.append((label, _format_altitude(value * scale)))
    for key, label in (
        ("baro_vr_fpm", "Barometric vertical rate"),
        ("geo_vr_fpm", "Geometric vertical rate"),
    ):
        value = _format_vertical_rate_fpm(track.get(key))
        if value:
            altitude_rows.append((label, value))
    vertical_rate = _finite_number(track.get("vertical_rate_ms"))
    if vertical_rate is not None:
        altitude_rows.append((
            "Vertical rate",
            "{:+.1f} m/s / {:+.0f} ft/min".format(
                vertical_rate, vertical_rate * 196.85
            ),
        ))
    if altitude_rows:
        section("ALTITUDE DETAIL")
        for label, value in altitude_rows:
            add(label, value)

    guidance_rows = (
        ("Autopilot modes", track.get("nav_modes")),
        ("Selected heading", (
            "{}°".format(track["selected_heading_deg"])
            if track.get("selected_heading_deg") is not None else None
        )),
        ("Altimeter / QNH", (
            "{} hPa".format(track["baro_setting_mb"])
            if track.get("baro_setting_mb") is not None else None
        )),
    )
    if any(value is not None for _, value in guidance_rows):
        section("FLIGHT GUIDANCE")
        for label, value in guidance_rows:
            add(label, value)

    quality_rows = (
        ("Position source", track.get("pos_source")),
        ("NACp / position", track.get("nac_p")),
        ("NACv / velocity", track.get("nac_v")),
        ("NIC / integrity", track.get("nic")),
        ("Containment radius", (
            "{} m".format(track["radius_containment_m"])
            if track.get("radius_containment_m") is not None else None
        )),
        ("Position age", (
            "{} s".format(track["position_age_s"])
            if track.get("position_age_s") is not None else None
        )),
        ("SIL", track.get("sil")),
        ("SIL basis", track.get("sil_type")),
        ("Geometric vertical accuracy", track.get("gva")),
        ("System design assurance", track.get("sda")),
        ("ADS-B version", track.get("adsb_version")),
        ("Messages received", track.get("message_count")),
        ("Last message age", (
            "{} s".format(track["message_age_s"])
            if track.get("message_age_s") is not None else None
        )),
        ("Signal strength", (
            "{} dBFS".format(
                track["rssi_db"]
                if track.get("rssi_db") is not None
                else track["rssi_dbfs"]
            )
            if track.get("rssi_db") is not None
            or track.get("rssi_dbfs") is not None
            else None
        )),
    )
    if any(value is not None for _, value in quality_rows):
        section("ADS-B QUALITY")
        for label, value in quality_rows:
            add(label, value)

    detail_rows = (
        ("Aircraft description", track.get("aircraft_description")),
        ("Comment", track.get("comment")),
        ("Sensor ID", track.get("sensor_id")),
        ("Sensor type", track.get("sensor_type")),
    )
    if any(value is not None for _, value in detail_rows):
        section("ADDITIONAL DETAILS")
        for label, value in detail_rows:
            add(label, value)

    section("SYSTEM")
    add("EFDI track ID", uid)
    return result


def _nvg_modifiers(track: dict, label: str) -> str:
    identity = []
    for field_label, key in (
        ("SRC", "_src"),
        ("ICAO", "icao24"),
        ("REG", "registration"),
        ("MMSI", "mmsi"),
        ("SQ", "squawk"),
    ):
        value = _nvg_text(track.get(key), 48)
        if value:
            identity.append("{} {}".format(field_label, value))

    modifiers = [("T", label)]
    entity_type = _nvg_text(
        track.get("aircraft_type")
        or track.get("ship_type")
        or track.get("sensor_type"),
        64,
    )
    if entity_type:
        modifiers.append(("V", entity_type))
    if identity:
        modifiers.append(("H", " | ".join(identity)))

    timestamp = _timestamp(track)
    if timestamp:
        modifiers.append(("W", timestamp))
    altitude_modifier = _altitude_modifier_value(track)
    if altitude_modifier:
        modifiers.append(("X", altitude_modifier))
    lat = _finite_number(track.get("lat_deg"))
    lon = _finite_number(track.get("lon_deg"))
    if lat is not None and lon is not None:
        modifiers.append(("Y", "{:.5f}, {:.5f}".format(lat, lon)))
    if any(
        track.get(key) is not None
        for key in ("speed_ms", "ground_speed_kts", "speed_kts", "sog_ms")
    ):
        speed_ms = _speed_ms(track)
        modifiers.append((
            "Z",
            "{} kt | {} km/h".format(
                round(speed_ms / 0.514444), round(speed_ms * 3.6)
            ),
        ))
    squawk = _nvg_text(track.get("squawk"), 16)
    if squawk:
        modifiers.append(("P", squawk))

    status = []
    if track.get("on_ground"):
        status.append("ON GROUND")
    if track.get("is_military"):
        status.append("MILITARY")
    emergency = _nvg_text(track.get("emergency_str"), 48)
    if emergency:
        status.append("EMERGENCY " + emergency.upper())
    if status:
        modifiers.append(("G", " | ".join(status)))

    def safe(value: str) -> str:
        return value.replace(":", "-").replace(";", ",")

    return ";".join("{}:{}".format(key, safe(value)) for key, value in modifiers)


def track_to_nvg_item(
    track: dict,
    sidc: str,
    symbol_scheme: str = "app6c",
    valid_until: float | None = None,
) -> tuple[str, str] | None:
    """Return (item_id, NVG XML string) or None if no position."""
    lat = track.get("lat_deg")
    lon = track.get("lon_deg")
    if lat is None or lon is None:
        return None

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    uid   = _uid(track)
    label = _nvg_text(_callsign(track, uid), 128) or uid[-12:]

    ET.register_namespace("", NVG_NS)
    root = ET.Element("{%s}nvg" % NVG_NS, {"version": NVG_VERSION})

    point_attrs = {
        "uri":    "urn:efdi:" + urllib.parse.quote(uid, safe="-._~"),
        "symbol": symbol_scheme + ":" + sidc,
        "label":  label,
        "modifiers": _nvg_modifiers(track, label),
        "x":      str(round(lon, 6)),
        "y":      str(round(lat, 6)),
    }
    primary_altitude = _primary_altitude(track)
    if primary_altitude:
        point_attrs["z"] = str(primary_altitude[0])
    if any(track.get(key) is not None for key in ("speed_ms", "ground_speed_kts", "speed_kts", "sog_ms")):
        point_attrs["speed"] = str(round(_speed_ms(track) * 3.6, 2))
    if any(track.get(key) is not None for key in ("heading_deg", "track_deg", "cog_deg")):
        point_attrs["course"] = str(_course(track))

    point = ET.SubElement(root, "{%s}point" % NVG_NS, point_attrs)
    geometry = track.get("geometry")
    if isinstance(geometry, dict) and os.environ.get("NVG_GEOMETRY_ENABLE", "1") not in {"0", "false", "no"}:
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")

        def add_points(parent, values):
            for coordinate in values[:256] if isinstance(values, list) else []:
                if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 2:
                    continue
                try:
                    x_lon, y_lat = float(coordinate[0]), float(coordinate[1])
                except (TypeError, ValueError):
                    continue
                if -180 <= x_lon <= 180 and -90 <= y_lat <= 90:
                    ET.SubElement(parent, "{%s}point" % NVG_NS, {
                        "x": str(round(x_lon, 6)), "y": str(round(y_lat, 6))
                    })

        shape_attrs = {
            "uri": "urn:efdi:" + urllib.parse.quote(uid + "-geometry", safe="-._~"),
            "label": label,
        }
        if geometry_type == "Polygon" and isinstance(coordinates, list) and coordinates:
            polygon = ET.SubElement(root, "{%s}polygon" % NVG_NS, shape_attrs)
            add_points(polygon, coordinates[0])
        elif geometry_type == "LineString":
            line = ET.SubElement(root, "{%s}polyline" % NVG_NS, shape_attrs)
            add_points(line, coordinates)
        elif geometry_type == "MultiLineString" and isinstance(coordinates, list):
            for index, values in enumerate(coordinates[:8]):
                line = ET.SubElement(root, "{%s}polyline" % NVG_NS,
                                      dict(shape_attrs, uri=shape_attrs["uri"] + "-{}".format(index)))
                add_points(line, values)
        elif geometry_type == "Circle" and isinstance(geometry.get("radius_km"), (int, float)):
            ET.SubElement(root, "{%s}circle" % NVG_NS, dict(
                shape_attrs, x=str(round(lon, 6)), y=str(round(lat, 6)),
                radius_km=str(round(float(geometry["radius_km"]), 3))))
    text_info = _xml_safe_text(
        _build_remarks(track, _cot_type_for_sidc(sidc)),
        20_000,
    )
    if text_info:
        ET.SubElement(point, "{%s}textInfo" % NVG_NS).text = text_info
    timestamp = _timestamp(track)
    if timestamp:
        ET.SubElement(point, "{%s}TimeStamp" % NVG_NS).text = timestamp
    expiry = _iso_timestamp(valid_until)
    if expiry:
        time_span = ET.SubElement(point, "{%s}TimeSpan" % NVG_NS)
        ET.SubElement(time_span, "{%s}end" % NVG_NS).text = expiry
    extended_fields = _nvg_extended_data(track, uid, sidc)
    extended_fields.append(("EFDI provenance", "fabric-export"))
    if extended_fields:
        extended_data = ET.SubElement(point, "{%s}ExtendedData" % NVG_NS)
        for key, value in extended_fields:
            ET.SubElement(
                extended_data,
                "{%s}SimpleData" % NVG_NS,
                {"key": key},
            ).text = value

    xml_str = '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")
    return uid, xml_str


MAX_ZENOH_PAYLOAD = 1_000_000
_TOPIC_STALE_S = {
    "env/weather/station/**": 7200.0,
}
_HQ_SYMBOL_SCHEME = "2525b"
_ACCESS_LOG_INTERVAL_S = 60.0


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class NVGFeedCache:
    """Thread-safe, size-bounded snapshot of recently received NVG items."""

    def __init__(
        self,
        stale_s: float,
        max_tracks: int,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not math.isfinite(stale_s) or stale_s <= 0:
            raise ValueError("stale_s must be a positive finite number")
        if max_tracks <= 0:
            raise ValueError("max_tracks must be positive")
        self._stale_s = stale_s
        self._max_tracks = max_tracks
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, float, float]] = {}

    def upsert(self, track: dict, sidc: str, stale_s: float | None = None) -> str | None:
        item_stale_s = self._stale_s if stale_s is None else stale_s
        if not math.isfinite(item_stale_s) or item_stale_s <= 0:
            raise ValueError("item stale_s must be a positive finite number")
        result = track_to_nvg_item(
            track,
            sidc,
            symbol_scheme=_HQ_SYMBOL_SCHEME,
            valid_until=self._wall_clock() + item_stale_s,
        )
        if result is None:
            return None
        uid, xml = result
        now = self._clock()
        with self._lock:
            if uid not in self._items and len(self._items) >= self._max_tracks:
                oldest_uid = min(self._items, key=lambda key: self._items[key][1])
                del self._items[oldest_uid]
            self._items[uid] = (xml, now, item_stale_s)
        return uid

    def remove(self, track: dict) -> str:
        uid = _uid(track)
        with self._lock:
            self._items.pop(uid, None)
        return uid

    def _snapshot(self) -> list[tuple[str, str]]:
        now = self._clock()
        with self._lock:
            expired = [
                uid for uid, (_, seen_at, stale_s) in self._items.items()
                if now - seen_at > stale_s
            ]
            for uid in expired:
                del self._items[uid]
            return sorted((uid, xml) for uid, (xml, _, _) in self._items.items())

    def document(self) -> tuple[bytes, int]:
        snapshot = self._snapshot()
        ET.register_namespace("", NVG_NS)
        root = ET.Element("{%s}nvg" % NVG_NS, {"version": NVG_VERSION})
        count = 0
        for _, item_xml in snapshot:
            # This XML was generated locally by track_to_nvg_item; no external
            # or user-supplied XML is parsed here.
            try:
                item_root = SafeET.fromstring(item_xml)
            except (SafeET.ParseError, DefusedXmlException):
                continue
            for child in item_root:
                root.append(child)
                count += 1
        body = b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(
            root, encoding="utf-8"
        )
        return body, count


def basic_authorized(header: str | None, username: str, password: str) -> bool:
    if not header or not username or not password:
        return False
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    expected = "Basic " + token
    return hmac.compare_digest(header, expected)


class NVGFeedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        cache: NVGFeedCache,
        feed_path: str,
        username: str,
        password: str,
        allow_anonymous: bool,
        verbose: bool,
    ) -> None:
        self.cache = cache
        self.feed_path = feed_path
        self.username = username
        self.password = password
        self.allow_anonymous = allow_anonymous
        self.verbose = verbose
        self._request_lock = threading.Lock()
        self._successful_requests = 0
        self._unauthorized_requests = 0
        self._last_successful_request: float | None = None
        self._last_unauthorized_request: float | None = None
        self._last_access_log = {"successful": 0.0, "unauthorized": 0.0}
        super().__init__(address, NVGFeedHandler)

    @staticmethod
    def _timestamp(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )

    def record_feed_request(self, authorized: bool) -> bool:
        """Record one feed request and return whether it should be logged."""
        now = time.time()
        monotonic_now = time.monotonic()
        outcome = "successful" if authorized else "unauthorized"
        with self._request_lock:
            if authorized:
                self._successful_requests += 1
                self._last_successful_request = now
                count = self._successful_requests
            else:
                self._unauthorized_requests += 1
                self._last_unauthorized_request = now
                count = self._unauthorized_requests
            should_log = (
                count == 1
                or monotonic_now - self._last_access_log[outcome] >= _ACCESS_LOG_INTERVAL_S
            )
            if should_log:
                self._last_access_log[outcome] = monotonic_now
            return should_log

    def request_stats(self) -> dict[str, int | float | str | None]:
        now = time.time()
        with self._request_lock:
            last_success = self._last_successful_request
            return {
                "successful_requests": self._successful_requests,
                "unauthorized_requests": self._unauthorized_requests,
                "last_successful_request": self._timestamp(last_success),
                "last_unauthorized_request": self._timestamp(
                    self._last_unauthorized_request
                ),
                "seconds_since_last_success": (
                    round(max(0.0, now - last_success), 1)
                    if last_success is not None
                    else None
                ),
            }


class NVGFeedHandler(BaseHTTPRequestHandler):
    server: NVGFeedServer
    server_version = "EFDI-NVG/1.0"
    sys_version = ""

    def _authorized(self) -> bool:
        return self.server.allow_anonymous or basic_authorized(
            self.headers.get("Authorization"),
            self.server.username,
            self.server.password,
        )

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _reject_unauthorized(self) -> None:
        body = b"Authentication required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="EFDI NVG feed", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self, include_body: bool) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path not in {self.server.feed_path, "/healthz"}:
            body = b"Not found\n"
            self._headers(404, "text/plain; charset=utf-8", len(body))
            if include_body:
                self.wfile.write(body)
            return
        authorized = self._authorized()
        if not authorized:
            if path == self.server.feed_path and self.server.record_feed_request(False):
                print(
                    "NVG feed rejected unauthorized request from {}".format(
                        self.client_address[0]
                    ),
                    flush=True,
                )
            self._reject_unauthorized()
            return

        if path == "/healthz":
            _, count = self.server.cache.document()
            body = json.dumps(
                {
                    "status": "ok",
                    "tracks": count,
                    "feed_requests": self.server.request_stats(),
                },
                separators=(",", ":"),
            ).encode()
            content_type = "application/json; charset=utf-8"
        else:
            body, count = self.server.cache.document()
            if self.server.record_feed_request(True):
                print(
                    "NVG feed served {} tracks to {}".format(
                        count, self.client_address[0]
                    ),
                    flush=True,
                )
            content_type = "application/xml; charset=utf-8"
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route(include_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = b"Method not allowed\n"
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        if self.server.verbose:
            print("NVG HTTP {} - {}".format(self.client_address[0], fmt % args), flush=True)


def make_handler(
    sidc,
    cache: NVGFeedCache,
    verbose: bool,
    stale_s: float | None = None,
):
    def handler(sample) -> None:
        try:
            payload = bytes(sample.payload)
            if len(payload) > MAX_ZENOH_PAYLOAD:
                raise ValueError("payload exceeds 1 MB")
            track = json.loads(payload.decode("utf-8"))
            if not isinstance(track, dict):
                raise ValueError("track payload is not an object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            if verbose:
                print("NVG feed ignored invalid Zenoh sample: {}".format(exc), flush=True)
            return
        # SitaWare-originated objects remain on Zenoh and are forwarded to TAK,
        # but must not be written straight back into the same SitaWare import
        # subscription. The matching ingress bridge preserves the raw export.
        if track.get("_ingress") == "sitaware_nvg":
            return
        if _is_unfused_sensor_track(track, str(sample.key_expr)):
            return
        if track.get("_delete"):
            uid = cache.remove(track)
            if verbose:
                print("NVG feed removed {}".format(uid), flush=True)
            return
        uid = cache.upsert(
            track,
            _resolve_sidc(sidc, track),
            stale_s=stale_s if stale_s is not None else STALE_S,
        )
        if verbose and uid:
            print("NVG feed cached {}".format(uid), flush=True)

    return handler


def _validate_args(args, password: str) -> None:
    if not args.path.startswith("/") or "?" in args.path or "#" in args.path:
        raise SystemExit("SITAWARE_HQ_NVG_PATH must be an absolute path without query/fragment")
    if not (1 <= args.port <= 65535):
        raise SystemExit("SITAWARE_HQ_NVG_PORT must be between 1 and 65535")
    if bool(args.tls_cert) != bool(args.tls_key):
        raise SystemExit("Set both SITAWARE_HQ_NVG_TLS_CERT and SITAWARE_HQ_NVG_TLS_KEY")
    if not args.allow_anonymous and (not args.user or not password):
        raise SystemExit(
            "Set SITAWARE_HQ_NVG_USER and SITAWARE_HQ_NVG_PASS, or explicitly "
            "set SITAWARE_HQ_NVG_ALLOW_ANONYMOUS=1"
        )
    non_loopback = args.bind not in {"127.0.0.1", "::1", "localhost"}
    if non_loopback and not args.tls_cert and not args.allow_insecure_http:
        raise SystemExit(
            "Refusing a non-loopback plain-HTTP feed. Configure TLS, or explicitly "
            "set SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1 for an isolated lab network."
        )


def run(args) -> None:
    password = os.environ.get("SITAWARE_HQ_NVG_PASS", "")
    _validate_args(args, password)

    cache = NVGFeedCache(args.stale_s, args.max_tracks)
    server = NVGFeedServer(
        (args.bind, args.port),
        cache,
        args.path,
        args.user,
        password,
        args.allow_anonymous,
        args.verbose,
    )
    scheme = "http"
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"

    session = open_session()
    subscribers = []
    try:
        for suffix, sidc in _TOPIC_SIDC.items():
            key = "{}/{}".format(TOPIC_ROOT, suffix)
            item_stale_s = max(args.stale_s, _TOPIC_STALE_S.get(suffix, args.stale_s))
            subscribers.append(
                subscribe(
                    session,
                    key,
                    make_handler(sidc, cache, args.verbose, stale_s=item_stale_s),
                )
            )
            print(
                "SUB {} -> SIDC {}".format(
                    key, getattr(sidc, "__name__", sidc)
                ),
                flush=True,
            )

        print(
            "SitaWare HQ NVG feed listening on {}://{}:{}{} (stale={}s, max={})".format(
                scheme, args.bind, args.port, args.path, args.stale_s, args.max_tracks
            ),
            flush=True,
        )
        if scheme == "http" and args.user:
            print(
                "WARNING: Basic Auth is being sent over plain HTTP. Use only on an "
                "isolated lab network and migrate the feed to HTTPS.",
                flush=True,
            )
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        for subscriber in subscribers:
            subscriber.undeclare()
        session.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Zenoh tracks -> SitaWare HQ NVG pull feed")
    parser.add_argument("--bind", default=os.environ.get("SITAWARE_HQ_NVG_BIND", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SITAWARE_HQ_NVG_PORT", "8088"))
    )
    parser.add_argument("--path", default=os.environ.get("SITAWARE_HQ_NVG_PATH", "/nvg"))
    parser.add_argument("--user", default=os.environ.get("SITAWARE_HQ_NVG_USER", ""))
    parser.add_argument(
        "--tls-cert", default=os.environ.get("SITAWARE_HQ_NVG_TLS_CERT", "")
    )
    parser.add_argument("--tls-key", default=os.environ.get("SITAWARE_HQ_NVG_TLS_KEY", ""))
    parser.add_argument(
        "--stale-s",
        type=float,
        default=float(os.environ.get("SITAWARE_HQ_NVG_STALE_S", "120")),
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        default=int(os.environ.get("SITAWARE_HQ_NVG_MAX_TRACKS", "10000")),
    )
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        default=_env_true("SITAWARE_HQ_NVG_ALLOW_ANONYMOUS"),
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        default=_env_true("SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP"),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
