#!/usr/bin/env python3
"""SAPIENT FLEX 335 v2 TCP bridge and map-ready message decoder.

The official SAPIENT protobuf schema contains many tasking and management
messages.  EFDI only needs the common envelope, registration/status metadata,
detections, and alerts at the data-plane boundary.  This module decodes that
documented subset directly from protobuf wire format so the PID-managed bridge
does not need generated bindings or a second runtime dependency.

Reference schema: https://github.com/dstl/SAPIENT-Proto-Files
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
import socket
import struct
import time
from typing import Iterator
import uuid

from namespace_prefix import topic_root
from protocols.gateway import ZError, open_session, publish_dual, publish_native, subscribe
from protocols.track_views import (
    native_topic,
    semantic_topic,
)
from protocols.proto.flex335_pb2 import SapientFlex335Track

# Outbound (EFDI -> SAPIENT) encoders live in this same file, alongside the
# inbound decoder above — one file per protocol, both directions. Encoding
# targets the official schema vendored at compose/vendor/sapient_msg
# (Apache-2.0, see docs/INSTALL.md §7 "Integrations → Vendored third-party schemas"), not a
# local approximation, so the output is interoperable with real SAPIENT
# systems.
from sapient_msg.bsi_flex_335_v2_0.alert_ack_pb2 import AlertAck
from sapient_msg.bsi_flex_335_v2_0.alert_pb2 import Alert
from sapient_msg.bsi_flex_335_v2_0.detection_report_pb2 import DetectionReport
from sapient_msg.bsi_flex_335_v2_0.location_pb2 import (
    LocationCoordinateSystem,
    LocationDatum,
)
from sapient_msg.bsi_flex_335_v2_0.registration_ack_pb2 import RegistrationAck
from sapient_msg.bsi_flex_335_v2_0.registration_pb2 import Registration
from sapient_msg.bsi_flex_335_v2_0.sapient_message_pb2 import SapientMessage
from sapient_msg.bsi_flex_335_v2_0.status_report_pb2 import StatusReport
from sapient_msg.bsi_flex_335_v2_0.task_ack_pb2 import TaskAck
from sapient_msg.bsi_flex_335_v2_0.task_pb2 import Task


MAX_FRAME_BYTES = 1_048_576
MAX_OBJECTS = 20_000
ZENOH_RETRY_S = 5

_CONTENT_FIELDS = {
    4: "registration",
    5: "registration_ack",
    6: "status_report",
    7: "detection_report",
    8: "task",
    9: "task_ack",
    10: "alert",
    11: "alert_ack",
    12: "error",
}

_NODE_TYPES = {
    1: "other",
    2: "radar",
    3: "lidar",
    4: "camera",
    5: "seismic",
    6: "acoustic",
    7: "proximity_sensor",
    8: "passive_rf",
    9: "human",
    10: "chemical",
    11: "biological",
    12: "radiation",
    13: "kinetic",
    14: "jammer",
    15: "cyber",
    16: "ldew",
    17: "rfdew",
    18: "mobile_node",
    19: "pointable_node",
    20: "fusion_node",
}

_SYSTEM_STATES = {
    0: "unspecified",
    1: "ok",
    2: "warning",
    3: "error",
    5: "goodbye",
}

_ALERT_TYPES = {
    0: "unspecified",
    1: "information",
    2: "warning",
    3: "critical",
    4: "error",
    5: "fatal",
    6: "mode_change",
}

_ALERT_STATUS = {
    0: "unspecified",
    1: "active",
    2: "acknowledge",
    3: "reject",
    4: "ignore",
    5: "clear",
}

_ALERT_PRIORITY = {0: "unspecified", 1: "low", 2: "medium", 3: "high"}

_SAPIENT_INFO = {0: "unspecified", 1: "new", 2: "unchanged"}

_POWER_SOURCES = {
    0: "unspecified", 1: "other", 2: "mains", 3: "internal_battery",
    4: "external_battery", 5: "generator", 6: "solar_pv", 7: "wind_turbine",
    8: "fuel_cell",
}

_POWER_STATUS = {0: "unspecified", 1: "ok", 2: "fault"}

_STATUS_LEVELS = {
    0: "unspecified", 2: "information", 3: "warning", 4: "error",
}

_STATUS_TYPES = {
    0: "unspecified", 1: "internal_fault", 2: "external_fault", 3: "illumination",
    4: "weather", 5: "clutter", 6: "exposure", 7: "motion_sensitivity",
    8: "ptz_status", 9: "pd", 10: "far", 11: "not_detecting", 12: "platform",
    13: "other",
}

_TASK_STATUS = {
    0: "unspecified", 1: "accepted", 2: "rejected", 3: "completed", 4: "failed",
}

_ALERT_ACK_STATUS = {
    0: "unspecified", 1: "accepted", 2: "rejected", 3: "cancelled",
}

_TASK_CONTROL = {0: "unspecified", 1: "start", 2: "stop", 3: "pause"}
_DELETE_STATES = frozenset(
    {"clear", "closed", "delete", "deleted", "end", "ended", "lost", "removed", "terminated"}
)


@dataclass
class WireField:
    number: int
    wire_type: int
    value: int | bytes


@dataclass
class NodeState:
    name: str | None = None
    icd_version: str | None = None
    node_types: list[str] = field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    horizontal_speed_units: int | None = None
    vertical_speed_units: int | None = None
    capabilities: list[dict] = field(default_factory=list)
    status_interval_s: float | None = None
    dependent_nodes: list[str] = field(default_factory=list)
    reporting_region_count: int = 0
    config_data: list[dict] = field(default_factory=list)


@dataclass
class SapientEvent:
    kind: str
    node_id: str
    timestamp: float
    track: dict | None = None
    warning: str | None = None


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if offset >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("protobuf varint exceeds 64 bits")


def iter_fields(data: bytes) -> Iterator[WireField]:
    """Yield validated fields from one protobuf message."""
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number = tag >> 3
        wire_type = tag & 0x07
        if number == 0:
            raise ValueError("invalid protobuf field zero")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated protobuf fixed64")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated protobuf fixed32")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        else:
            raise ValueError("unsupported protobuf wire type {}".format(wire_type))
        yield WireField(number, wire_type, value)


def _field_list(data: bytes) -> list[WireField]:
    return list(iter_fields(data))


def _first(fields: list[WireField], number: int, wire_type: int | None = None):
    for item in fields:
        if item.number == number and (wire_type is None or item.wire_type == wire_type):
            return item.value
    return None


def _all(fields: list[WireField], number: int, wire_type: int | None = None):
    return [
        item.value
        for item in fields
        if item.number == number and (wire_type is None or item.wire_type == wire_type)
    ]


def _text(value) -> str | None:
    if not isinstance(value, bytes):
        return None
    try:
        result = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    result = " ".join(result.split())
    return result[:512] if result else None


def _double(value) -> float | None:
    if not isinstance(value, int):
        return None
    result = struct.unpack("<d", value.to_bytes(8, "little"))[0]
    return result if math.isfinite(result) else None


def _float(value) -> float | None:
    if not isinstance(value, int):
        return None
    result = struct.unpack("<f", value.to_bytes(4, "little"))[0]
    return result if math.isfinite(result) else None


def _timestamp(value: bytes | None, fallback: float | None = None) -> float:
    if not isinstance(value, bytes):
        return time.time() if fallback is None else fallback
    fields = _field_list(value)
    seconds = _first(fields, 1, 0)
    nanos = _first(fields, 2, 0) or 0
    if not isinstance(seconds, int) or not isinstance(nanos, int) or nanos >= 1_000_000_000:
        return time.time() if fallback is None else fallback
    return float(seconds) + nanos / 1_000_000_000.0


def _decode_location(value: bytes) -> tuple[float, float, float | None, dict]:
    fields = _field_list(value)
    x = _double(_first(fields, 1, 1))
    y = _double(_first(fields, 2, 1))
    z = _double(_first(fields, 3, 1))
    coordinate_system = _first(fields, 7, 0)
    datum = _first(fields, 8, 0)
    if x is None or y is None:
        raise ValueError("SAPIENT location has no coordinates")
    if coordinate_system == 1:
        lon, lat = x, y
    elif coordinate_system == 2:
        lon, lat = math.degrees(x), math.degrees(y)
    elif coordinate_system == 5:
        raise ValueError("SAPIENT UTM location needs a deployment coordinate transform")
    else:
        raise ValueError("unsupported SAPIENT location coordinate system")
    if datum not in (1, 2):
        raise ValueError("SAPIENT location datum must be WGS84 ellipsoid or WGS84 geoid")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("SAPIENT location is outside WGS-84 bounds")
    metadata = {
        "sapient_location_coordinate_system": int(coordinate_system),
        "sapient_location_datum": int(datum),
        "altitude_reference": "wgs84_ellipsoid" if datum == 1 else "wgs84_geoid",
    }
    x_error = _double(_first(fields, 4, 1))
    y_error = _double(_first(fields, 5, 1))
    z_error = _double(_first(fields, 6, 1))
    if x_error is not None:
        metadata["lon_error"] = x_error
    if y_error is not None:
        metadata["lat_error"] = y_error
    if z_error is not None:
        metadata["alt_error_m"] = z_error
    return lat, lon, z, metadata


def _decode_range_bearing(value: bytes) -> tuple[float, float, float, dict]:
    fields = _field_list(value)
    elevation = _double(_first(fields, 1, 1)) or 0.0
    azimuth = _double(_first(fields, 2, 1))
    distance = _double(_first(fields, 3, 1))
    coordinate_system = _first(fields, 7, 0)
    datum = _first(fields, 8, 0)
    if azimuth is None or distance is None:
        raise ValueError("SAPIENT range/bearing lacks azimuth or range")
    if coordinate_system in (2, 4):
        azimuth = math.degrees(azimuth)
        elevation = math.degrees(elevation)
    elif coordinate_system not in (1, 3):
        raise ValueError("unsupported SAPIENT range/bearing coordinate system")
    if coordinate_system in (3, 4):
        distance *= 1000.0
    if datum != 1:
        if datum in (2, 3, 4):
            raise ValueError(
                "SAPIENT magnetic/grid/platform bearing needs a deployment heading transform"
            )
        raise ValueError("SAPIENT range/bearing datum must be true north")
    return elevation, azimuth % 360.0, distance, {
        "sapient_range_m": round(distance, 2),
        "sapient_azimuth_deg": round(azimuth % 360.0, 3),
        "sapient_elevation_deg": round(elevation, 3),
        "sapient_range_bearing_datum": int(datum),
    }


def _project(origin: NodeState, elevation: float, azimuth: float, distance: float):
    if origin.latitude is None or origin.longitude is None:
        raise ValueError("range/bearing detection arrived before sensor location")
    horizontal = distance * math.cos(math.radians(elevation))
    angular = horizontal / 6_371_008.8
    bearing = math.radians(azimuth)
    lat1 = math.radians(origin.latitude)
    lon1 = math.radians(origin.longitude)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular)
        + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    altitude = None
    if origin.altitude_m is not None:
        altitude = origin.altitude_m + distance * math.sin(math.radians(elevation))
    return math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0, altitude


def _decode_info(values: list[bytes]) -> dict[str, str]:
    result = {}
    for value in values[:64]:
        fields = _field_list(value)
        key = _text(_first(fields, 1, 2))
        item = _text(_first(fields, 2, 2))
        if key and item:
            result[key[:96]] = item[:256]
    return result


def _decode_classification(value: bytes) -> dict:
    fields = _field_list(value)
    result = {"type": _text(_first(fields, 1, 2)) or "unknown"}
    confidence = _float(_first(fields, 2, 5))
    if confidence is not None:
        result["confidence"] = round(confidence, 4)
    sub_classes = []
    for sub_value in _all(fields, 3, 2)[:16]:
        sub_fields = _field_list(sub_value)
        sub_type = _text(_first(sub_fields, 1, 2))
        if sub_type:
            sub = {"type": sub_type}
            sub_confidence = _float(_first(sub_fields, 2, 5))
            level = _first(sub_fields, 3, 0)
            if sub_confidence is not None:
                sub["confidence"] = round(sub_confidence, 4)
            if isinstance(level, int):
                sub["level"] = level
            sub_classes.append(sub)
    if sub_classes:
        result["sub_classes"] = sub_classes
    return result


def _primary_class(classifications: list[dict]) -> str:
    if not classifications:
        return "unknown"
    return max(classifications, key=lambda item: item.get("confidence", -1.0))["type"].lower()


def _decode_signals(values: list[bytes]) -> list[dict]:
    result = []
    for value in values[:16]:
        fields = _field_list(value)
        signal = {}
        for number, name in (
            (1, "amplitude"),
            (2, "start_frequency"),
            (3, "centre_frequency"),
            (4, "stop_frequency"),
            (5, "pulse_duration"),
        ):
            item = _float(_first(fields, number, 5))
            if item is not None:
                signal[name] = item
        if signal:
            result.append(signal)
    return result


_TIME_UNITS_S = {1: 1e-9, 2: 1e-6, 3: 1e-3, 4: 1.0, 5: 60.0, 6: 3600.0, 7: 86400.0}


def _duration_seconds(units, amount) -> float | None:
    if not isinstance(units, int) or amount is None:
        return None
    scale = _TIME_UNITS_S.get(units)
    return round(amount * scale, 6) if scale is not None else None


def _speed_scale(units: int | None) -> float | None:
    if units == 1:
        return 1.0
    if units == 2:
        return 1.0 / 3.6
    return None


def _clean_class(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


def _modality(track: dict) -> str:
    """The `modality` topic segment, taken from the node's own registration.

    A SAPIENT node declares its NodeType when it registers, and _NODE_TYPES
    already renders that enum as the exact vocabulary the topic uses — so an
    incoming SAPIENT camera lands under /camera/ and a radar under /radar/,
    rather than every node collapsing into one segment.
    """
    types = track.get("sapient_node_types") or []
    return str(types[0]) if types else "unknown"


def topic_for_track(topic_root: str, track: dict) -> str:
    if track.get("sapient_message_type") == "status_report":
        return "{}/land/sapient/{}/neutral/sensor".format(topic_root, _modality(track))

    object_class = _clean_class(str(track.get("sapient_class") or "unknown"))
    if any(token in object_class for token in ("drone", "uas", "uav", "quadcopter")):
        domain, entity = "air", "uav"
    elif any(token in object_class for token in ("aircraft", "aeroplane", "airplane", "helicopter")):
        domain, entity = "air", "aircraft"
    elif any(token in object_class for token in ("boat", "ship", "vessel", "watercraft")):
        domain, entity = "sea", "vessel"
    elif any(token in object_class for token in ("person", "human", "pedestrian")):
        domain, entity = "land", "person"
    elif any(token in object_class for token in ("car", "truck", "vehicle")):
        domain, entity = "land", "vehicle"
    elif any(token in object_class for token in ("emitter", "rf", "signal")):
        domain, entity = "land", "sensor"
    else:
        domain, entity = "air", "aircraft"
    return "{}/{}/sapient/{}/unknown/{}".format(
        topic_root, domain, _modality(track), entity)


def topic_for_frame(track_topic: str) -> str:
    """Deprecated alias for native_topic().

    Kept so existing callers keep working after the egress topics were renamed
    from numbered versions to named formats (.../json, /sapient, /proto,
    /native). The verbatim SapientMessage now rides the /native sibling.
    """
    return native_topic(track_topic)

class SapientDecoder:
    def __init__(self):
        self.nodes: dict[str, NodeState] = {}
        self._objects: OrderedDict[tuple[str, str], dict] = OrderedDict()

    def decode(self, data: bytes) -> SapientEvent:
        if not data or len(data) > MAX_FRAME_BYTES:
            raise ValueError("invalid SAPIENT protobuf frame size")
        fields = _field_list(data)
        node_id = _text(_first(fields, 2, 2)) or "unknown"
        sent_at = _timestamp(_first(fields, 1, 2))
        contents = [item for item in fields if item.number in _CONTENT_FIELDS and item.wire_type == 2]
        if len(contents) != 1:
            raise ValueError("SAPIENT envelope must contain exactly one content message")
        content = contents[0]
        kind = _CONTENT_FIELDS[content.number]
        assert isinstance(content.value, bytes)
        node = self.nodes.setdefault(node_id, NodeState())

        if kind == "registration":
            self._registration(node, content.value)
            return SapientEvent(kind, node_id, sent_at)
        if kind == "status_report":
            track = self._status(node_id, node, sent_at, content.value)
            return SapientEvent(kind, node_id, sent_at, track)
        if kind == "detection_report":
            try:
                track = self._detection(node_id, node, sent_at, content.value)
                return SapientEvent(kind, node_id, sent_at, track)
            except ValueError as exc:
                return SapientEvent(kind, node_id, sent_at, warning=str(exc))
        if kind == "alert":
            try:
                track = self._alert(node_id, node, sent_at, content.value)
                return SapientEvent(kind, node_id, sent_at, track)
            except ValueError as exc:
                return SapientEvent(kind, node_id, sent_at, warning=str(exc))
        if kind == "registration_ack":
            info = self._registration_ack(content.value)
            print("SAPIENT registration_ack from {}: {}".format(node_id, info), flush=True)
            return SapientEvent(kind, node_id, sent_at)
        if kind == "task":
            info = self._task(content.value)
            print("SAPIENT task from {}: {}".format(node_id, info), flush=True)
            return SapientEvent(kind, node_id, sent_at)
        if kind == "task_ack":
            info = self._task_ack(content.value)
            print("SAPIENT task_ack from {}: {}".format(node_id, info), flush=True)
            return SapientEvent(kind, node_id, sent_at)
        if kind == "alert_ack":
            info = self._alert_ack(content.value)
            print("SAPIENT alert_ack from {}: {}".format(node_id, info), flush=True)
            return SapientEvent(kind, node_id, sent_at)
        if kind == "error":
            info = self._error(content.value)
            print("SAPIENT error from {}: {}".format(node_id, info), flush=True)
            return SapientEvent(kind, node_id, sent_at)
        return SapientEvent(kind, node_id, sent_at)

    def _registration(self, node: NodeState, data: bytes) -> None:
        fields = _field_list(data)
        node.icd_version = _text(_first(fields, 2, 2)) or node.icd_version
        node.name = _text(_first(fields, 4, 2)) or _text(_first(fields, 3, 2)) or node.name
        node_types = []
        for definition in _all(fields, 1, 2)[:16]:
            node_type = _first(_field_list(definition), 1, 0)
            if isinstance(node_type, int):
                node_types.append(_NODE_TYPES.get(node_type, "type_{}".format(node_type)))
        if node_types:
            node.node_types = node_types

        horizontal_units: set[int] = set()
        vertical_units: set[int] = set()
        for mode in _all(fields, 7, 2)[:32]:
            mode_fields = _field_list(mode)
            for definition in _all(mode_fields, 10, 2)[:32]:
                definition_fields = _field_list(definition)
                velocity_type = _first(definition_fields, 6, 2)
                if not isinstance(velocity_type, bytes):
                    continue
                enu_units = _first(_field_list(velocity_type), 4, 2)
                if not isinstance(enu_units, bytes):
                    continue
                units_fields = _field_list(enu_units)
                horizontal = _first(units_fields, 1, 0)
                vertical = _first(units_fields, 2, 0)
                if horizontal in (1, 2):
                    horizontal_units.add(horizontal)
                if vertical in (1, 2):
                    vertical_units.add(vertical)
        node.horizontal_speed_units = (
            next(iter(horizontal_units)) if len(horizontal_units) == 1 else None
        )
        node.vertical_speed_units = (
            next(iter(vertical_units)) if len(vertical_units) == 1 else None
        )

        node.capabilities = []
        for value in _all(fields, 5, 2)[:32]:
            cap_fields = _field_list(value)
            category = _text(_first(cap_fields, 1, 2))
            cap_type = _text(_first(cap_fields, 2, 2))
            if not category or not cap_type:
                continue
            entry = {"category": category, "type": cap_type}
            cap_value = _text(_first(cap_fields, 3, 2))
            units = _text(_first(cap_fields, 4, 2))
            if cap_value:
                entry["value"] = cap_value
            if units:
                entry["units"] = units
            node.capabilities.append(entry)

        status_def = _first(fields, 6, 2)
        if isinstance(status_def, bytes):
            sd_fields = _field_list(status_def)
            interval = _first(sd_fields, 1, 2)
            node.status_interval_s = None
            if isinstance(interval, bytes):
                duration_fields = _field_list(interval)
                units = _first(duration_fields, 1, 0)
                amount = _float(_first(duration_fields, 3, 5))
                node.status_interval_s = _duration_seconds(units, amount)

        node.dependent_nodes = [
            text for text in (_text(value) for value in _all(fields, 8, 2)[:64]) if text
        ]

        node.reporting_region_count = len(_all(fields, 9, 2))

        node.config_data = []
        for value in _all(fields, 10, 2)[:16]:
            cfg_fields = _field_list(value)
            manufacturer = _text(_first(cfg_fields, 1, 2))
            model = _text(_first(cfg_fields, 2, 2))
            if not manufacturer or not model:
                continue
            entry = {"manufacturer": manufacturer, "model": model}
            serial = _text(_first(cfg_fields, 3, 2))
            hw = _text(_first(cfg_fields, 4, 2))
            sw = _text(_first(cfg_fields, 5, 2))
            if serial: entry["serial_number"] = serial
            if hw: entry["hardware_version"] = hw
            if sw: entry["software_version"] = sw
            node.config_data.append(entry)

    def _status(self, node_id: str, node: NodeState, sent_at: float, data: bytes) -> dict | None:
        fields = _field_list(data)
        location = _first(fields, 7, 2)
        if isinstance(location, bytes):
            node.latitude, node.longitude, node.altitude_m, _ = _decode_location(location)
        if node.latitude is None or node.longitude is None:
            return None
        state = _first(fields, 2, 0)
        report_id = _text(_first(fields, 1, 2))
        mode = _text(_first(fields, 5, 2))
        track = {
            "_ts": sent_at,
            "_src": "SAPIENT FLEX 335",
            "uid": "sapient-node-{}".format(node_id),
            "sensor_id": node_id,
            "callsign": node.name or "SAPIENT-{}".format(node_id[-8:]),
            "lat_deg": round(node.latitude, 7),
            "lon_deg": round(node.longitude, 7),
            "sapient_message_type": "status_report",
            "sapient_node_types": node.node_types,
            "sapient_icd_version": node.icd_version,
        }
        if node.altitude_m is not None:
            track["geo_alt_m"] = round(node.altitude_m, 2)
        if report_id:
            track["sapient_report_id"] = report_id
        if isinstance(state, int):
            track["sensor_status"] = _SYSTEM_STATES.get(state, "state_{}".format(state))
            if state == 5:
                track["_delete"] = True
        if mode:
            track["sensor_mode"] = mode

        info = _first(fields, 3, 0)
        if isinstance(info, int):
            track["sapient_info"] = _SAPIENT_INFO.get(info, "info_{}".format(info))
        active_task_id = _text(_first(fields, 4, 2))
        if active_task_id:
            track["sapient_active_task_id"] = active_task_id

        power = _first(fields, 6, 2)
        if isinstance(power, bytes):
            power_fields = _field_list(power)
            level = _first(power_fields, 3, 0)
            source = _first(power_fields, 4, 0)
            p_status = _first(power_fields, 5, 0)
            if isinstance(level, int):
                track["sapient_power_level_pct"] = level
            if isinstance(source, int):
                track["sapient_power_source"] = _POWER_SOURCES.get(source, "source_{}".format(source))
            if isinstance(p_status, int):
                track["sapient_power_status"] = _POWER_STATUS.get(p_status, "status_{}".format(p_status))

        if _first(fields, 8, 2) is not None:
            track["sapient_field_of_view_present"] = True
        obscuration_count = len(_all(fields, 10, 2))
        if obscuration_count:
            track["sapient_obscuration_count"] = obscuration_count
        coverage_count = len(_all(fields, 12, 2))
        if coverage_count:
            track["sapient_coverage_count"] = coverage_count

        statuses = []
        for value in _all(fields, 11, 2)[:32]:
            status_fields = _field_list(value)
            status_level = _first(status_fields, 1, 0)
            status_type = _first(status_fields, 4, 0)
            entry = {}
            if isinstance(status_level, int):
                entry["level"] = _STATUS_LEVELS.get(status_level, "level_{}".format(status_level))
            if isinstance(status_type, int):
                entry["type"] = _STATUS_TYPES.get(status_type, "type_{}".format(status_type))
            value_text = _text(_first(status_fields, 3, 2))
            if value_text:
                entry["value"] = value_text
            if entry:
                statuses.append(entry)
        if statuses:
            track["sapient_status"] = statuses
        return track

    def _position(self, node: NodeState, fields: list[WireField]):
        location = _first(fields, 6, 2)
        if isinstance(location, bytes):
            return _decode_location(location)
        range_bearing = _first(fields, 5, 2)
        if isinstance(range_bearing, bytes):
            elevation, azimuth, distance, metadata = _decode_range_bearing(range_bearing)
            lat, lon, altitude = _project(node, elevation, azimuth, distance)
            return lat, lon, altitude, metadata
        raise ValueError("SAPIENT report has no supported location")

    def _detection(self, node_id: str, node: NodeState, sent_at: float, data: bytes) -> dict:
        fields = _field_list(data)
        object_id = _text(_first(fields, 2, 2)) or "unknown"
        state = _text(_first(fields, 4, 2))
        cache_key = (node_id, object_id)
        try:
            lat, lon, altitude, position_meta = self._position(node, fields)
        except ValueError:
            cached = self._objects.get(cache_key)
            if not cached or _clean_class(state or "") not in _DELETE_STATES:
                raise
            lat = cached["lat_deg"]
            lon = cached["lon_deg"]
            altitude = cached.get("geo_alt_m")
            position_meta = {}

        classifications = [
            _decode_classification(value) for value in _all(fields, 11, 2)[:32]
        ]
        object_class = _primary_class(classifications)
        report_id = _text(_first(fields, 1, 2))
        display_id = _text(_first(fields, 23, 2))
        track = {
            "_ts": sent_at,
            "_src": "SAPIENT FLEX 335",
            "uid": "sapient-{}-{}".format(node_id, object_id),
            "sensor_id": node_id,
            "object_id": object_id,
            "callsign": display_id or "{}-{}".format(object_class.upper(), object_id[-8:]),
            "lat_deg": round(lat, 7),
            "lon_deg": round(lon, 7),
            "sapient_message_type": "detection_report",
            "sapient_class": object_class,
            "sapient_classifications": classifications,
            "sapient_node_types": node.node_types,
            **position_meta,
        }
        if report_id:
            track["sapient_report_id"] = report_id
        if state:
            track["sapient_state"] = state
            if _clean_class(state) in _DELETE_STATES:
                track["_delete"] = True
        if altitude is not None:
            track["geo_alt_m"] = round(altitude, 2)
        confidence = _float(_first(fields, 7, 5))
        if confidence is not None:
            track["detection_confidence"] = round(confidence, 4)
        colour = _text(_first(fields, 22, 2))
        if colour:
            track["colour"] = colour
        track_info = _decode_info(_all(fields, 8, 2))
        object_info = _decode_info(_all(fields, 10, 2))
        if track_info:
            track["sapient_track_info"] = track_info
        if object_info:
            track["sapient_object_info"] = object_info
        signals = _decode_signals(_all(fields, 14, 2))
        if signals:
            track["sapient_signals"] = signals

        task_id = _text(_first(fields, 3, 2))
        if task_id:
            track["sapient_task_id"] = task_id

        prediction = _first(fields, 9, 2)
        if isinstance(prediction, bytes):
            pred_fields = _field_list(prediction)
            pred_location = _first(pred_fields, 2, 2)
            pred_rb = _first(pred_fields, 1, 2)
            try:
                if isinstance(pred_location, bytes):
                    p_lat, p_lon, p_alt, _meta = _decode_location(pred_location)
                elif isinstance(pred_rb, bytes):
                    p_elev, p_az, p_dist, _meta = _decode_range_bearing(pred_rb)
                    p_lat, p_lon, p_alt = _project(node, p_elev, p_az, p_dist)
                else:
                    p_lat = p_lon = p_alt = None
                if p_lat is not None:
                    track["sapient_predicted_lat_deg"] = round(p_lat, 7)
                    track["sapient_predicted_lon_deg"] = round(p_lon, 7)
                    if p_alt is not None:
                        track["sapient_predicted_alt_m"] = round(p_alt, 2)
            except ValueError:
                pass

        behaviours = []
        for value in _all(fields, 12, 2)[:16]:
            b_fields = _field_list(value)
            b_type = _text(_first(b_fields, 1, 2))
            if not b_type:
                continue
            entry = {"type": b_type}
            b_conf = _float(_first(b_fields, 2, 5))
            if b_conf is not None:
                entry["confidence"] = round(b_conf, 4)
            behaviours.append(entry)
        if behaviours:
            track["sapient_behaviour"] = behaviours

        associated_files = []
        for value in _all(fields, 13, 2)[:16]:
            af_fields = _field_list(value)
            af_type = _text(_first(af_fields, 1, 2))
            af_url = _text(_first(af_fields, 2, 2))
            if af_url:
                associated_files.append({"type": af_type or "unknown", "url": af_url})
        if associated_files:
            track["sapient_associated_files"] = associated_files

        associated_detections = []
        for value in _all(fields, 15, 2)[:32]:
            ad_fields = _field_list(value)
            ad_node = _text(_first(ad_fields, 2, 2))
            ad_object = _text(_first(ad_fields, 3, 2))
            if ad_node and ad_object:
                associated_detections.append({"node_id": ad_node, "object_id": ad_object})
        if associated_detections:
            track["sapient_associated_detections"] = associated_detections

        derived_detections = []
        for value in _all(fields, 16, 2)[:32]:
            dd_fields = _field_list(value)
            dd_node = _text(_first(dd_fields, 2, 2))
            dd_object = _text(_first(dd_fields, 3, 2))
            if dd_node and dd_object:
                derived_detections.append({"node_id": dd_node, "object_id": dd_object})
        if derived_detections:
            track["sapient_derived_detections"] = derived_detections

        sapient_id = _text(_first(fields, 23, 2))
        if sapient_id:
            track["sapient_id"] = sapient_id

        velocity = _first(fields, 19, 2)
        if isinstance(velocity, bytes):
            velocity_fields = _field_list(velocity)
            east = _double(_first(velocity_fields, 1, 1))
            north = _double(_first(velocity_fields, 2, 1))
            up = _double(_first(velocity_fields, 3, 1))
            horizontal_scale = _speed_scale(node.horizontal_speed_units)
            vertical_scale = _speed_scale(node.vertical_speed_units)
            if east is not None and north is not None and horizontal_scale is not None:
                east *= horizontal_scale
                north *= horizontal_scale
                track["speed_ms"] = round(math.hypot(east, north), 3)
                track["heading_deg"] = round(math.degrees(math.atan2(east, north)) % 360.0, 2)
            if up is not None and vertical_scale is not None:
                track["vertical_rate_ms"] = round(up * vertical_scale, 3)

        if track.get("_delete"):
            self._objects.pop(cache_key, None)
        else:
            self._objects[cache_key] = dict(track)
            self._objects.move_to_end(cache_key)
            while len(self._objects) > MAX_OBJECTS:
                self._objects.popitem(last=False)
        return track

    def _alert(self, node_id: str, node: NodeState, sent_at: float, data: bytes) -> dict:
        fields = _field_list(data)
        lat, lon, altitude, position_meta = self._position(node, fields)
        alert_id = _text(_first(fields, 1, 2)) or "unknown"
        alert_type = _first(fields, 2, 0)
        status = _first(fields, 3, 0)
        priority = _first(fields, 8, 0)
        description = _text(_first(fields, 4, 2))
        track = {
            "_ts": sent_at,
            "_src": "SAPIENT FLEX 335",
            "uid": "sapient-alert-{}-{}".format(node_id, alert_id),
            "sensor_id": node_id,
            "object_id": alert_id,
            "callsign": description or "SAPIENT ALERT",
            "lat_deg": round(lat, 7),
            "lon_deg": round(lon, 7),
            "sapient_message_type": "alert",
            "sapient_class": "alert",
            **position_meta,
        }
        if altitude is not None:
            track["geo_alt_m"] = round(altitude, 2)
        if isinstance(alert_type, int):
            track["sapient_alert_type"] = _ALERT_TYPES.get(alert_type, str(alert_type))
        if isinstance(status, int):
            status_name = _ALERT_STATUS.get(status, str(status))
            track["sapient_alert_status"] = status_name
            if status_name == "clear":
                track["_delete"] = True
        if isinstance(priority, int):
            track["sapient_alert_priority"] = _ALERT_PRIORITY.get(priority, str(priority))
        confidence = _float(_first(fields, 10, 5))
        if confidence is not None:
            track["detection_confidence"] = round(confidence, 4)
        if description:
            track["description"] = description

        region_id = _text(_first(fields, 7, 2))
        if region_id:
            track["sapient_region_id"] = region_id

        associated_files = []
        for value in _all(fields, 11, 2)[:16]:
            af_fields = _field_list(value)
            af_type = _text(_first(af_fields, 1, 2))
            af_url = _text(_first(af_fields, 2, 2))
            if af_url:
                associated_files.append({"type": af_type or "unknown", "url": af_url})
        if associated_files:
            track["sapient_associated_files"] = associated_files

        associated_detections = []
        for value in _all(fields, 12, 2)[:32]:
            ad_fields = _field_list(value)
            ad_node = _text(_first(ad_fields, 2, 2))
            ad_object = _text(_first(ad_fields, 3, 2))
            if ad_node and ad_object:
                associated_detections.append({"node_id": ad_node, "object_id": ad_object})
        if associated_detections:
            track["sapient_associated_detections"] = associated_detections

        additional_info = _text(_first(fields, 13, 2))
        if additional_info:
            track["sapient_additional_information"] = additional_info
        return track

    def _registration_ack(self, data: bytes) -> dict:
        fields = _field_list(data)
        acceptance = _first(fields, 1, 0)
        reasons = [text for text in (_text(v) for v in _all(fields, 3, 2)[:16]) if text]
        info = {"accepted": bool(acceptance)}
        if reasons:
            info["reasons"] = reasons
        return info

    def _task(self, data: bytes) -> dict:
        fields = _field_list(data)
        task_id = _text(_first(fields, 1, 2))
        name = _text(_first(fields, 2, 2))
        description = _text(_first(fields, 3, 2))
        control = _first(fields, 6, 0)
        info = {}
        if task_id: info["task_id"] = task_id
        if name: info["task_name"] = name
        if description: info["task_description"] = description
        if isinstance(control, int):
            info["control"] = _TASK_CONTROL.get(control, "control_{}".format(control))
        regions = []
        for value in _all(fields, 7, 2)[:16]:
            r_fields = _field_list(value)
            region_id = _text(_first(r_fields, 2, 2))
            region_name = _text(_first(r_fields, 3, 2))
            if region_id or region_name:
                regions.append({"region_id": region_id, "region_name": region_name})
        if regions:
            info["regions"] = regions
        command = _first(fields, 8, 2)
        if isinstance(command, bytes):
            cmd_fields = _field_list(command)
            request = _text(_first(cmd_fields, 1, 2))
            mode_change = _text(_first(cmd_fields, 5, 2))
            param = _text(_first(cmd_fields, 8, 2))
            if request: info["command_request"] = request
            if mode_change: info["command_mode_change"] = mode_change
            if param: info["command_parameter"] = param
        return info

    def _task_ack(self, data: bytes) -> dict:
        fields = _field_list(data)
        task_id = _text(_first(fields, 1, 2))
        status = _first(fields, 2, 0)
        reasons = [text for text in (_text(v) for v in _all(fields, 5, 2)[:16]) if text]
        info = {}
        if task_id: info["task_id"] = task_id
        if isinstance(status, int):
            info["task_status"] = _TASK_STATUS.get(status, "status_{}".format(status))
        if reasons:
            info["reasons"] = reasons
        return info

    def _alert_ack(self, data: bytes) -> dict:
        fields = _field_list(data)
        alert_id = _text(_first(fields, 1, 2))
        reasons = [text for text in (_text(v) for v in _all(fields, 4, 2)[:16]) if text]
        status = _first(fields, 5, 0)
        info = {}
        if alert_id: info["alert_id"] = alert_id
        if isinstance(status, int):
            info["alert_ack_status"] = _ALERT_ACK_STATUS.get(status, "status_{}".format(status))
        if reasons:
            info["reasons"] = reasons
        return info

    def _error(self, data: bytes) -> dict:
        fields = _field_list(data)
        messages = [text for text in (_text(v) for v in _all(fields, 3, 2)[:32]) if text]
        packet = _first(fields, 1, 2)
        info = {"messages": messages}
        if isinstance(packet, bytes):
            info["packet_bytes"] = len(packet)
        return info


# ---------------------------------------------------------------------------
# Encode (EFDI -> SAPIENT)
#
# The reading half above decodes SAPIENT arriving from a sensor. This half
# turns EFDI-side data into real SAPIENT messages, so a consumer needs to
# understand SAPIENT alone rather than every source protocol. Encoding
# targets the official schema vendored at compose/vendor/sapient_msg
# (Apache-2.0), not a local approximation, so the output is interoperable
# with real SAPIENT systems. SAPIENT is a common-denominator contract: it
# deliberately cannot express every field a specific sensor knows, so
# track_to_sapient() is lossy BY DESIGN, published ALONGSIDE the per-protocol
# /proto message and the byte-exact /raw tier rather than replacing them.
# ---------------------------------------------------------------------------

# Crockford base32, per the ULID spec — excludes I, L, O and U so the encoded
# form cannot be misread. report_id/object_id are declared `is_ulid` in the
# schema, so a plain UUID would be the wrong shape.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# This pod's SAPIENT node identity. The schema requires a UUID; a deployment
# that has not set one gets a stable per-process fallback so output is still
# well-formed rather than empty.
_NODE_ID = os.environ.get("SAPIENT_NODE_ID", "") or str(uuid.uuid4())

# EFDI classification vocabulary -> SAPIENT classification strings. Values not
# listed fall through unchanged: an unknown-but-present classification is more
# useful to a consumer than silently dropping it.
_CLASSIFICATION = {
    "aircraft": "Air Vehicle",
    "uav": "Air Vehicle",
    "drone": "Air Vehicle",
    "helicopter": "Air Vehicle",
    "vessel": "Surface Vessel",
    "ship": "Surface Vessel",
    "boat": "Surface Vessel",
    "vehicle": "Land Vehicle",
    "car": "Land Vehicle",
    "truck": "Land Vehicle",
    "person": "Human",
    "pedestrian": "Human",
}


def _ulid(value: int | None = None) -> str:
    """26-character Crockford-base32 ULID.

    With no argument: 48-bit millisecond timestamp + 80 random bits, the normal
    ULID shape. With an argument the whole 128-bit value is supplied by the
    caller, which is how a per-object id stays byte-identical across reports.
    """
    if value is None:
        ms = int(time.time() * 1000) & ((1 << 48) - 1)
        value = (ms << 80) | (uuid.uuid4().int & ((1 << 80) - 1))
    value &= (1 << 128) - 1
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _stable_object_id(track: dict) -> str:
    """A ULID derived entirely from the object's identity.

    The whole 128 bits come from the identity hash — NOT just the random half.
    Deriving only the low bits leaves the timestamp half moving, so two reports
    for one aircraft a millisecond apart would produce different object_ids and
    the consumer would see two contacts instead of a track.
    """
    ident = str(
        track.get("uid")
        or track.get("icao24")
        or track.get("mmsi")
        or track.get("track_num")
        or track.get("callsign")
        or ""
    )
    if not ident:
        return _ulid()
    return _ulid(uuid.uuid5(uuid.NAMESPACE_OID, ident).int)


def _classification_for(track: dict) -> str | None:
    for key in ("classification", "sapient_class", "target_type", "entity"):
        raw = track.get(key)
        if isinstance(raw, str) and raw.strip():
            lowered = raw.lower()
            for token, mapped in _CLASSIFICATION.items():
                if token in lowered:
                    return mapped
            return raw
    return None


def track_to_sapient(track: dict, node_id: str = "") -> SapientMessage | None:
    """Build a SapientMessage/DetectionReport from a normalized EFDI track.

    Returns None when the track has no position: `location` is inside a
    mandatory oneof, so a DetectionReport without one is not a valid SAPIENT
    message and is better not published than published malformed.
    """
    lat, lon = track.get("lat_deg"), track.get("lon_deg")
    if lat is None or lon is None:
        return None

    message = SapientMessage()
    message.timestamp.FromNanoseconds(int(float(track.get("_ts") or time.time()) * 1e9))
    message.node_id = node_id or _NODE_ID

    report = message.detection_report
    report.report_id = _ulid()
    report.object_id = _stable_object_id(track)

    location = report.location
    location.x = float(lon)          # schema: x is normally longitude
    location.y = float(lat)          # schema: y is normally latitude
    altitude = track.get("geo_alt_m", track.get("baro_alt_m", track.get("alt_m")))
    if altitude is not None:
        location.z = float(altitude)
    location.coordinate_system = LocationCoordinateSystem.LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M
    location.datum = LocationDatum.LOCATION_DATUM_WGS84_G

    confidence = track.get("detection_confidence", track.get("confidence"))
    if confidence is not None:
        try:
            report.detection_confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            pass

    classification = _classification_for(track)
    if classification:
        entry = report.classification.add()
        entry.type = classification

    # Ground speed + heading describe the same vector ENUVelocity wants, just in
    # polar form; convert rather than drop it.
    speed, heading = track.get("speed_ms"), track.get("heading_deg")
    if speed is not None and heading is not None:
        try:
            radians = math.radians(float(heading))
            velocity = report.enu_velocity
            velocity.east_rate = float(speed) * math.sin(radians)
            velocity.north_rate = float(speed) * math.cos(radians)
            vertical = track.get("vertical_rate_ms")
            if vertical is not None:
                velocity.up_rate = float(vertical)
        except (TypeError, ValueError):
            pass

    identifier = track.get("callsign") or track.get("icao24") or track.get("uid")
    if identifier:
        report.id = str(identifier)[:128]

    return message


def sapient_topic(topic: str) -> str:
    """SAPIENT view of an object key (.../{id} -> .../{id}/sapient/tracks/v1).

    Named like every other view so no format is implicit — a consumer reading
    the key always knows what the bytes are — and versioned like every other
    view so the goat boundary admits it.
    """
    from protocols.track_views import view_key
    return view_key(topic, "sapient")


def publish_sapient(session, topic: str, track: dict, zenoh, node_id: str = "") -> None:
    """Publish the SAPIENT view of a track. Best-effort, like the other tiers:
    a conversion failure must never take down the JSON or protobuf legs."""
    try:
        message = track_to_sapient(track, node_id=node_id)
        if message is None:
            return
        payload = message.SerializeToString()
    except Exception as exc:  # noqa: BLE001 — never break the other tiers
        print("sapient encode failed for {}: {}".format(topic, exc), flush=True)
        return
    from protocols.data_stats import record_out
    from protocols.track_views import proto_encoding
    session.put(sapient_topic(topic), payload, encoding=proto_encoding(message, zenoh))
    record_out("egress-sapient", len(payload))


def _envelope(node_id: str = "") -> SapientMessage:
    message = SapientMessage()
    message.timestamp.FromNanoseconds(int(time.time() * 1e9))
    message.node_id = node_id or _NODE_ID
    return message


def registration_to_sapient(
    node_type: int,
    icd_version: str,
    name: str = "",
    capabilities: list[dict] | None = None,
    config_data: list[dict] | None = None,
    node_id: str = "",
) -> SapientMessage:
    """Build a SapientMessage/Registration announcing this pod as a SAPIENT node."""
    message = _envelope(node_id)
    reg = message.registration
    reg.node_definition.add().node_type = node_type
    reg.icd_version = icd_version
    if name:
        reg.name = name
    for cap in capabilities or ():
        entry = reg.capabilities.add()
        entry.category = cap.get("category", "")
        entry.type = cap.get("type", "")
        if cap.get("value") is not None:
            entry.value = str(cap["value"])
        if cap.get("units"):
            entry.units = cap["units"]
    for cfg in config_data or ():
        entry = reg.config_data.add()
        entry.manufacturer = cfg.get("manufacturer", "EFDI")
        entry.model = cfg.get("model", "EFDI")
        if cfg.get("serial_number"):
            entry.serial_number = cfg["serial_number"]
        if cfg.get("hardware_version"):
            entry.hardware_version = cfg["hardware_version"]
        if cfg.get("software_version"):
            entry.software_version = cfg["software_version"]
    return message


def status_report_to_sapient(
    state: int,
    mode: str,
    lat: float | None = None,
    lon: float | None = None,
    alt_m: float | None = None,
    node_id: str = "",
) -> SapientMessage:
    """Build a SapientMessage/StatusReport announcing this pod's own health."""
    message = _envelope(node_id)
    report = message.status_report
    report.report_id = _ulid()
    report.system = state
    report.info = StatusReport.INFO_NEW
    report.mode = mode
    if lat is not None and lon is not None:
        location = report.node_location
        location.x = float(lon)
        location.y = float(lat)
        if alt_m is not None:
            location.z = float(alt_m)
        location.coordinate_system = LocationCoordinateSystem.LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M
        location.datum = LocationDatum.LOCATION_DATUM_WGS84_G
    return message


def alert_to_sapient(
    alert_type: int,
    status: int,
    description: str = "",
    lat: float | None = None,
    lon: float | None = None,
    priority: int = 0,
    confidence: float | None = None,
    node_id: str = "",
) -> SapientMessage:
    """Build a SapientMessage/Alert reporting this pod's own alert condition."""
    message = _envelope(node_id)
    alert = message.alert
    alert.alert_id = _ulid()
    alert.alert_type = alert_type
    alert.status = status
    if description:
        alert.description = description
    if lat is not None and lon is not None:
        location = alert.location
        location.x = float(lon)
        location.y = float(lat)
        location.coordinate_system = LocationCoordinateSystem.LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M
        location.datum = LocationDatum.LOCATION_DATUM_WGS84_G
    if priority:
        alert.priority = priority
    if confidence is not None:
        alert.confidence = max(0.0, min(1.0, float(confidence)))
    return message


def registration_ack_to_sapient(
    destination_id: str, accepted: bool = True, reasons: list[str] | None = None,
    node_id: str = "",
) -> SapientMessage:
    """Build a proper protobuf SapientMessage/RegistrationAck.

    Prefer this over the hand-rolled `registration_ack()` wire-encoder below
    when the caller already has a real Zenoh/protobuf runtime available; the
    hand-rolled encoder exists only so the TCP listener can ack a peer before
    a Zenoh session might even be open.
    """
    message = _envelope(node_id)
    message.destination_id = destination_id
    ack = message.registration_ack
    ack.acceptance = accepted
    for reason in reasons or ():
        ack.ack_response_reason.append(reason)
    return message


def task_ack_to_sapient(
    destination_id: str, task_id: str, status: int, reasons: list[str] | None = None,
    node_id: str = "",
) -> SapientMessage:
    """Build a SapientMessage/TaskAck (accept/reject/complete/fail a tasking command)."""
    message = _envelope(node_id)
    message.destination_id = destination_id
    ack = message.task_ack
    ack.task_id = task_id
    ack.task_status = status
    for reason in reasons or ():
        ack.reason.append(reason)
    return message


def alert_ack_to_sapient(
    destination_id: str, alert_id: str, status: int, reasons: list[str] | None = None,
    node_id: str = "",
) -> SapientMessage:
    """Build a SapientMessage/AlertAck (accept/reject/cancel an alert)."""
    message = _envelope(node_id)
    message.destination_id = destination_id
    ack = message.alert_ack
    ack.alert_id = alert_id
    ack.alert_ack_status = status
    for reason in reasons or ():
        ack.reason.append(reason)
    return message


def error_to_sapient(packet: bytes, error_messages: list[str], node_id: str = "") -> SapientMessage:
    """Build a SapientMessage/Error reporting a malformed message this pod received."""
    message = _envelope(node_id)
    error = message.error
    error.packet = packet
    for text in error_messages:
        error.error_message.append(text)
    return message


def task_to_sapient(
    destination_id: str,
    task_id: str,
    control: int,
    task_name: str = "",
    command_request: str = "",
    mode_change: str = "",
    node_id: str = "",
) -> SapientMessage:
    """Build a SapientMessage/Task tasking a sensor node.

    Covers the top-level task envelope and the scalar Command variants
    (request, mode_change); the pointing/movement/filter sub-messages
    (LookAt, MoveTo, Patrol, Follow, region class/behaviour filters) are not
    modelled — EFDI is a fusion/aggregation node, not a sensor tasking
    authority, so those deeper tasking paths are out of scope here.
    """
    message = _envelope(node_id)
    message.destination_id = destination_id
    task = message.task
    task.task_id = task_id
    task.control = control
    if task_name:
        task.task_name = task_name
    if command_request or mode_change:
        command = task.command
        if command_request:
            command.request = command_request
        elif mode_change:
            command.mode_change = mode_change
    return message


def recv_exact(sock, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("SAPIENT connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def iter_frames(sock, max_frame_bytes: int = MAX_FRAME_BYTES):
    """Yield FLEX 335 protobuf frames (32-bit little-endian byte length)."""
    while True:
        length = struct.unpack("<I", recv_exact(sock, 4))[0]
        if length == 0 or length > max_frame_bytes:
            raise ValueError("invalid SAPIENT frame length: {}".format(length))
        yield recv_exact(sock, length)


def _encode_varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _encode_bytes(number: int, value: bytes) -> bytes:
    return _encode_varint((number << 3) | 2) + _encode_varint(len(value)) + value


def registration_ack(bridge_node_id: str, destination_id: str, accepted: bool = True) -> bytes:
    """Build a framed FLEX 335 RegistrationAck for listener mode."""
    now = time.time()
    seconds = int(now)
    nanos = int((now - seconds) * 1_000_000_000)
    timestamp = _encode_varint(8) + _encode_varint(seconds)
    if nanos:
        timestamp += _encode_varint(16) + _encode_varint(nanos)
    ack = _encode_varint(8) + _encode_varint(1 if accepted else 0)
    envelope = b"".join(
        (
            _encode_bytes(1, timestamp),
            _encode_bytes(2, bridge_node_id.encode("utf-8")),
            _encode_bytes(3, destination_id.encode("utf-8")),
            _encode_bytes(5, ack),
        )
    )
    return struct.pack("<I", len(envelope)) + envelope


# ---------------------------------------------------------------------------
# TCP / Zenoh bridge runtime
# ---------------------------------------------------------------------------

TOPIC_ROOT = topic_root()
RECONNECT_S = 5


def _allowed(peer: str, rules: list[str]) -> bool:
    if not rules:
        return peer in {"127.0.0.1", "::1"}
    address = ipaddress.ip_address(peer)
    for rule in rules:
        try:
            if address in ipaddress.ip_network(rule, strict=False):
                return True
        except ValueError:
            continue
    return False


def _publish(session, decoder: SapientDecoder, frame: bytes, verbose: bool, sock=None, bridge_id=""):
    event = decoder.decode(frame)
    if event.kind == "registration" and sock is not None:
        sock.sendall(registration_ack(bridge_id, event.node_id))
        print("SAPIENT registered node {}".format(event.node_id), flush=True)
    if event.warning:
        print("SAPIENT ignored {} from {}: {}".format(event.kind, event.node_id, event.warning), flush=True)
        return
    if event.track is None:
        return
    topic = topic_for_track(TOPIC_ROOT, event.track)
    # /sapient, /json and /proto views on this object's key.
    publish_dual(session, topic, event.track, SapientFlex335Track)
    # /raw carries the original BSI Flex 335 v2 SapientMessage. `frame` is the
    # bare message — both ingress paths strip the 32-bit little-endian length
    # prefix before this point (iter_frames, and the --zenoh-raw reassembler) —
    # so it is republished verbatim: no re-encode, no field loss, and no
    # dependency on the locally-modelled subset above. Built from the SAME
    # object key publish_dual uses, so all four views sit together.
    publish_native(session, native_topic(semantic_topic(topic, event.track)),
                   frame, "sapient",
                   profile="bsi-flex-335-v2", content_type="application/protobuf")
    if verbose:
        print(
            "SAPIENT {} {} -> {} lat={} lon={}".format(
                event.kind,
                event.track.get("callsign", event.node_id),
                topic,
                event.track.get("lat_deg"),
                event.track.get("lon_deg"),
            ),
            flush=True,
        )


def _consume(sock, session, decoder, args, acknowledge=False):
    for frame in iter_frames(sock):
        _publish(
            session,
            decoder,
            frame,
            args.verbose,
            sock=sock if acknowledge else None,
            bridge_id=args.node_id,
        )


def _connect_loop(session, args):
    decoder = SapientDecoder()
    while True:
        sock = None
        try:
            print("Connecting to SAPIENT {}:{}...".format(args.host, args.port), flush=True)
            sock = socket.create_connection((args.host, args.port), timeout=10)
            sock.settimeout(args.timeout)
            print("SAPIENT connected", flush=True)
            _consume(sock, session, decoder, args)
        except (EOFError, OSError, TimeoutError, ValueError) as exc:
            print("SAPIENT connection error: {} - retry in {}s".format(exc, RECONNECT_S), flush=True)
            time.sleep(RECONNECT_S)
        finally:
            if sock is not None:
                sock.close()


def _listen_loop(session, args):
    rules = []
    for value in args.allow_peer:
        rules.extend(item.strip() for item in value.split(",") if item.strip())
    server = socket.socket(socket.AF_INET6 if ":" in args.bind else socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.bind, args.listen))
    server.listen(16)
    print("SAPIENT listener on {}:{}".format(args.bind, args.listen), flush=True)
    while True:
        conn, address = server.accept()
        peer = address[0]
        if not _allowed(peer, rules):
            print("SAPIENT rejected unapproved peer {}".format(peer), flush=True)
            conn.close()
            continue
        print("SAPIENT connected from {}".format(peer), flush=True)
        conn.settimeout(args.timeout)
        try:
            _consume(conn, session, SapientDecoder(), args, acknowledge=not args.no_ack)
        except (EOFError, OSError, TimeoutError, ValueError) as exc:
            if args.verbose:
                print("SAPIENT peer {} disconnected: {}".format(peer, exc), flush=True)
        finally:
            conn.close()


def run(args):
    if args.zenoh_raw:
        return run_zenoh_raw(args)
    while True:
        try:
            session = open_session()
            break
        except ZError as exc:
            print("SAPIENT Zenoh connect failed: {} — retry in {}s".format(exc, ZENOH_RETRY_S), flush=True)
            time.sleep(ZENOH_RETRY_S)
    print("SAPIENT FLEX 335 -> Zenoh root: {}".format(TOPIC_ROOT), flush=True)
    try:
        if args.listen:
            _listen_loop(session, args)
        else:
            _connect_loop(session, args)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def run_zenoh_raw(args):
    """Decode FLEX 335 length-prefixed bytes received by a raw Zenoh bridge."""
    while True:
        try:
            session = open_session()
            break
        except ZError as exc:
            print("SAPIENT raw Zenoh connect failed: {} — retry in {}s".format(exc, ZENOH_RETRY_S), flush=True)
            time.sleep(ZENOH_RETRY_S)
    topic = args.raw_topic or TOPIC_ROOT + "/raw/sapient/flex335/**"
    decoder = SapientDecoder()
    buffer = bytearray()

    def on_sample(sample):
        try:
            data = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
            buffer.extend(data)
            while len(buffer) >= 4:
                length = struct.unpack_from("<I", buffer, 0)[0]
                if length == 0 or length > MAX_FRAME_BYTES:
                    del buffer[0]
                    continue
                if len(buffer) < length + 4:
                    break
                frame = bytes(buffer[4:4 + length])
                del buffer[:4 + length]
                _publish(session, decoder, frame, args.verbose)
        except Exception as exc:
            print("SAPIENT raw decode error:", exc, flush=True)

    subscriber = subscribe(session, topic, on_sample)
    print("SAPIENT FLEX 335 Zenoh raw translator subscribed to {}".format(topic), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main():
    default_bridge_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "efdi:sapient:" + ORG))
    parser = argparse.ArgumentParser(description="BSI Flex 335 v2 SAPIENT -> Zenoh bridge")
    parser.add_argument("--host", default=os.environ.get("SAPIENT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SAPIENT_PORT", "7001")),
        help="connector-mode TCP port",
    )
    parser.add_argument(
        "--listen", type=int, default=int(os.environ.get("SAPIENT_LISTEN_PORT", "0") or 0),
        help="listen for SAPIENT edge nodes instead of connecting",
    )
    parser.add_argument("--bind", default=os.environ.get("SAPIENT_BIND", "127.0.0.1"))
    parser.add_argument(
        "--allow-peer", action="append", default=[os.environ.get("SAPIENT_ALLOW_PEER", "")],
        help="allowed source IP or CIDR in listener mode; repeatable",
    )
    parser.add_argument("--no-ack", action="store_true", help="do not send RegistrationAck")
    parser.add_argument("--node-id", default=os.environ.get("SAPIENT_NODE_ID", default_bridge_id))
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("SAPIENT_TIMEOUT_S", "60"))
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--zenoh-raw", action="store_true",
                        help="decode bytes from .../raw/sapient/flex335/**")
    parser.add_argument("--raw-topic", default=os.environ.get("SAPIENT_RAW_TOPIC", ""))
    args = parser.parse_args()
    if not 1 <= (args.listen or args.port) <= 65535:
        parser.error("TCP port must be between 1 and 65535")
    try:
        uuid.UUID(args.node_id)
    except ValueError:
        parser.error("--node-id must be a UUID")
    run(args)


if __name__ == "__main__":
    main()
