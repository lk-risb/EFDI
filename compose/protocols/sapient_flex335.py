#!/usr/bin/env python3
"""BSI Flex 335 v2 SAPIENT TCP bridge and map-ready message decoder.

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

import zenoh
from zenoh_auth import apply_zenoh_auth

from namespace_prefix import topic_root


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
    horizontal_speed_units: int = 1
    vertical_speed_units: int = 1


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
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("SAPIENT location is outside WGS-84 bounds")
    metadata = {
        "sapient_location_coordinate_system": int(coordinate_system),
        "sapient_location_datum": int(datum or 0),
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
    return elevation, azimuth % 360.0, distance, {
        "sapient_range_m": round(distance, 2),
        "sapient_azimuth_deg": round(azimuth % 360.0, 3),
        "sapient_elevation_deg": round(elevation, 3),
        "sapient_range_bearing_datum": int(datum or 0),
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
            (2, "start_frequency_hz"),
            (3, "centre_frequency_hz"),
            (4, "stop_frequency_hz"),
            (5, "pulse_duration_s"),
        ):
            item = _float(_first(fields, number, 5))
            if item is not None:
                signal[name] = item
        if signal:
            result.append(signal)
    return result


def _speed_scale(units: int) -> float:
    return 1.0 / 3.6 if units == 2 else 1.0


def _clean_class(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


def topic_for_track(topic_root: str, track: dict) -> str:
    if track.get("sapient_message_type") == "status_report":
        return "{}/land/sapient/flex335/neutral/sensor/status/v1".format(topic_root)

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
    return "{}/{}/sapient/flex335/unknown/{}/tracks/v1".format(topic_root, domain, entity)


def topic_for_frame(track_topic: str) -> str:
    """Native-protobuf egress topic paired with a JSON track topic.

    The /v1 topics carry the flattened JSON view (lossy: only the fields this
    module models). The /v2 sibling carries the original BSI Flex 335 v2
    SapientMessage bytes verbatim, so fabric consumers get full fidelity —
    including fields EFDI does not decode. JSON is published alongside during
    the transition and is intended to be retired once consumers move to /v2.
    """
    if track_topic.endswith("/v1"):
        return track_topic[: -len("/v1")] + "/v2"
    return track_topic + "/v2"


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
                if isinstance(horizontal, int):
                    node.horizontal_speed_units = horizontal
                if isinstance(vertical, int):
                    node.vertical_speed_units = vertical

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

        velocity = _first(fields, 19, 2)
        if isinstance(velocity, bytes):
            velocity_fields = _field_list(velocity)
            east = _double(_first(velocity_fields, 1, 1))
            north = _double(_first(velocity_fields, 2, 1))
            up = _double(_first(velocity_fields, 3, 1))
            if east is not None and north is not None:
                east *= _speed_scale(node.horizontal_speed_units)
                north *= _speed_scale(node.horizontal_speed_units)
                track["speed_ms"] = round(math.hypot(east, north), 3)
                track["heading_deg"] = round(math.degrees(math.atan2(east, north)) % 360.0, 2)
            if up is not None:
                track["vertical_rate_ms"] = round(
                    up * _speed_scale(node.vertical_speed_units), 3
                )

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
        return track


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

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
RECONNECT_S = 5


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5(
            "transport/link/tls",
            json.dumps(
                {
                    "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
                    "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
                    "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
                    "enable_mtls": True,
                    "verify_name_on_connect": True,
                }
            ),
        )
    return conf


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
    session.put(
        topic,
        json.dumps(event.track, separators=(",", ":")).encode("utf-8"),
        encoding=zenoh.Encoding.APPLICATION_JSON,
    )
    # Native BSI Flex 335 v2 egress. `frame` is the bare SapientMessage — both
    # ingress paths strip the 32-bit little-endian length prefix before this
    # point (iter_frames, and the --zenoh-raw reassembler), so the bytes are a
    # complete protobuf message. Republished verbatim: no re-encode, no field
    # loss, and no dependency on a locally-modelled schema.
    session.put(
        topic_for_frame(topic),
        frame,
        encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
    )
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
            session = zenoh.open(make_config())
            break
        except zenoh.ZError as exc:
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
            session = zenoh.open(make_config())
            break
        except zenoh.ZError as exc:
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

    subscriber = session.declare_subscriber(topic, on_sample)
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
