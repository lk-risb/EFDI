#!/usr/bin/env python3
"""nvg_layer.py — Zenoh EFDI track topics → NVG translation layer.

Subscribes to all EFDI track topics, translates them into NVG 2.0 items, and
pushes live positions to an NVG-capable SitaWare endpoint. This file is kept as
a legacy push adapter; the HQ-only runtime uses ``nvg_bridge.py`` instead.

NVG items are PUT individually on each Zenoh update so connected operators see
real-time movement.  A background refresh thread re-PUTs live items every
REFRESH_S seconds and DELETEs items older than STALE_S seconds.

NVG REST base: https://<host>:<port>/SWEdge/nvg/v2
  PUT  /sources/{source}/items/{item-id}   — create / update one item
  DELETE /sources/{source}/items/{item-id} — remove one item

Required env vars (or --args). Named SITAWARE_NVG_* (not SITAWARE_*) because this
is the outbound NVG push adapter and uses a separate endpoint/credentials from
the inbound SitaWare HQ pull in sitaware_bridge.py:
  SITAWARE_NVG_URL    https://nvg-endpoint.example     (no trailing slash)
  SITAWARE_NVG_USER    your integration username
  SITAWARE_NVG_PASS    your integration password
  SITAWARE_NVG_SOURCE  efdi-live                 (source name, created automatically)

Run:
  SITAWARE_NVG_URL=https://192.168.1.10:8080 SITAWARE_NVG_USER=admin SITAWARE_NVG_PASS=secret \\
    venv/bin/python3 nvg_layer.py
"""

import argparse
import base64
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import zenoh
from zenoh_auth import apply_zenoh_auth
from layers.cot_layer import (
    _build_remarks,
    _callsign as _cot_callsign,
    _course,
    _is_adsb_surface_vehicle,
    _is_hostile_icao24,
    _is_hostile_mmsi,
    _speed_ms,
    _uid as _cot_uid,
)
from namespace_prefix import topic_root

ORG    = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

NVG_NS      = "https://tide.act.nato.int/schemas/2012/10/nvg"
NVG_VERSION = "2.0.2"
REFRESH_S   = 10    # re-PUT all live tracks at this interval
STALE_S     = 120   # delete tracks older than this
ZENOH_RETRY_S = 5

# APP-6(B) SIDC codes — keyed by new schema wildcard patterns. A resolver
# function is used where CoT already derives affiliation from ICAO/MMSI ranges;
# both output protocols must classify the same track identically.
# ** matches zero-or-more Zenoh path segments.
def _civil_air_sidc(track: dict) -> str:
    if _is_adsb_surface_vehicle(track):
        return "SNGPEV----*****"
    return "SHAPCF----*****" if _is_hostile_icao24(track.get("icao24")) \
        else "SNAPCF----*****"


def _military_air_sidc(track: dict) -> str:
    if _is_adsb_surface_vehicle(track):
        return "SNGPEV----*****"
    return "SHAPMF----*****" if _is_hostile_icao24(track.get("icao24")) \
        else "SNAPMF----*****"


def _civil_sea_sidc(track: dict) -> str:
    return "SHSPXF----*****" if _is_hostile_mmsi(track.get("mmsi")) \
        else "SNSPXF----*****"


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


_TOPIC_SIDC = {
    "air/**/civ/aircraft/**":        _civil_air_sidc,
    "air/**/mil/aircraft/**":        _military_air_sidc,
    "air/**/friendly/aircraft/**":   "SFAPMF----*****",
    "air/**/friendly/uav/**":        "SFAPMFQ---*****",
    "air/**/hostile/aircraft/**":    "SHAPMF----*****",
    "air/**/neutral/aircraft/**":    "SNAPMF----*****",
    "air/**/unknown/**":             _unknown_air_sidc,
    "land/**/civ/vehicle/**":        "SFGPUCV---*****",  # Friendly Ground Vehicle
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


# ---------------------------------------------------------------------------
# NVG XML builders
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
            "{} dBFS".format(track["rssi_db"])
            if track.get("rssi_db") is not None else None
        )),
    )
    if any(value is not None for _, value in quality_rows):
        section("ADS-B QUALITY")
        for label, value in quality_rows:
            add(label, value)

    detail_rows = (
        ("Aircraft description", track.get("aircraft_description")),
        ("APRS symbol", track.get("symbol")),
        ("APRS path", track.get("path")),
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


# ---------------------------------------------------------------------------
# NVG HTTP client
# ---------------------------------------------------------------------------

class NvgClient:
    def __init__(self, base_url: str, source: str, user: str, password: str):
        self.base    = base_url.rstrip("/")
        self.source  = source
        self._auth   = "Basic " + base64.b64encode("{}:{}".format(user, password).encode()).decode()

    def _req(self, method: str, path: str, body: str | None = None) -> int:
        source = urllib.parse.quote(self.source, safe="")
        url = "{}/SWEdge/nvg/v2/sources/{}/items{}".format(self.base, source, path)
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization":  self._auth,
            "Content-Type":   "application/xml; charset=utf-8",
            "Accept":         "application/xml",
            "User-Agent":     "efdi-nvg-bridge/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except urllib.error.URLError as exc:
            print("NVG HTTP error:", exc, flush=True)
            return 0

    def put_item(self, item_id: str, nvg_xml: str) -> bool:
        status = self._req("PUT", "/{}".format(urllib.parse.quote(item_id, safe="")), nvg_xml)
        return status in (200, 201, 204)

    def delete_item(self, item_id: str) -> bool:
        status = self._req("DELETE", "/{}".format(urllib.parse.quote(item_id, safe="")))
        return status in (200, 204, 404)


# ---------------------------------------------------------------------------
# Track cache + refresh thread
# ---------------------------------------------------------------------------

class TrackCache:
    def __init__(self, client: NvgClient, stale_s: int, refresh_s: int, verbose: bool):
        self._client   = client
        self._stale_s  = stale_s
        self._refresh_s = refresh_s
        self._verbose  = verbose
        self._lock     = threading.Lock()
        self._tracks: dict[str, dict] = {}   # uid → {track, sidc, last_seen}
        self._thread   = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()

    def upsert(self, track: dict, sidc: str):
        result = track_to_nvg_item(track, sidc)
        if result is None:
            return
        uid, nvg_xml = result
        ok = self._client.put_item(uid, nvg_xml)
        if self._verbose:
            label = track.get("callsign") or track.get("mmsi") or uid[-10:]
            print("NVG PUT {} {} {}".format("OK" if ok else "FAIL", sidc[:6], label), flush=True)
        with self._lock:
            self._tracks[uid] = {"track": track, "sidc": sidc, "last_seen": time.time()}

    def remove(self, track: dict):
        uid = _uid(track)
        with self._lock:
            self._tracks.pop(uid, None)
        self._client.delete_item(uid)
        if self._verbose:
            print("NVG DEL offline", uid, flush=True)

    def _refresh_loop(self):
        while True:
            time.sleep(self._refresh_s)
            now = time.time()
            to_delete = []
            to_refresh = []
            with self._lock:
                for uid, entry in list(self._tracks.items()):
                    age = now - entry["last_seen"]
                    if age > self._stale_s:
                        to_delete.append(uid)
                    else:
                        to_refresh.append((uid, entry["track"], entry["sidc"]))
                for uid in to_delete:
                    del self._tracks[uid]

            for uid in to_delete:
                self._client.delete_item(uid)
                if self._verbose:
                    print("NVG DEL stale", uid, flush=True)

            live = 0
            for uid, track, sidc in to_refresh:
                result = track_to_nvg_item(track, sidc)
                if result:
                    self._client.put_item(uid, result[1])
                    live += 1
            if to_refresh or to_delete:
                print("NVG refresh: {} live, {} expired".format(live, len(to_delete)), flush=True)


# ---------------------------------------------------------------------------
# Zenoh subscriber callbacks
# ---------------------------------------------------------------------------

def make_handler(sidc, cache: TrackCache):
    def handler(sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        if track.get("_delete"):
            cache.remove(track)
            return
        cache.upsert(track, _resolve_sidc(sidc, track))
    return handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    if not args.url:
        raise SystemExit("SITAWARE_NVG_URL not set — pass --url https://host:port or set env var")
    if not args.url.lower().startswith("https://"):
        raise SystemExit("SITAWARE_NVG_URL must use https://")
    if not args.user or not args.password:
        raise SystemExit("SITAWARE_NVG_USER / SITAWARE_NVG_PASS not set")

    client = NvgClient(args.url, args.source, args.user, args.password)
    cache  = TrackCache(client, stale_s=STALE_S, refresh_s=REFRESH_S, verbose=args.verbose)

    print("NVG push endpoint: {}  source: {}".format(args.url, args.source), flush=True)

    while True:
        try:
            session = zenoh.open(make_config())
            break
        except zenoh.ZError as exc:
            print("SitaWare NVG Zenoh connect failed: {} — retry in {}s".format(exc, ZENOH_RETRY_S), flush=True)
            time.sleep(ZENOH_RETRY_S)
    subs = []
    for suffix, sidc in _TOPIC_SIDC.items():
        key = "{}/{}".format(TOPIC_ROOT, suffix)
        subs.append(session.declare_subscriber(key, make_handler(sidc, cache)))
        print(
            "SUB {} → SIDC {}".format(key, getattr(sidc, "__name__", sidc)),
            flush=True,
        )

    print("Bridge running — Ctrl-C to stop", flush=True)
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
    ap = argparse.ArgumentParser(description="Zenoh tracks → NVG push bridge")
    ap.add_argument("--url",      default=os.environ.get("SITAWARE_NVG_URL", ""),
                    help="NVG base URL, e.g. https://192.168.1.10:8080")
    ap.add_argument("--user",     default=os.environ.get("SITAWARE_NVG_USER", ""),
                    help="NVG username")
    ap.add_argument("--password", default=os.environ.get("SITAWARE_NVG_PASS", ""),
                    help="NVG password")
    ap.add_argument("--source",   default=os.environ.get("SITAWARE_NVG_SOURCE", "efdi-live"),
                    help="NVG source name (default: efdi-live)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each NVG PUT/DELETE")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
