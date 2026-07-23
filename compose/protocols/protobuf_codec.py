"""Small reflection-based adapters for the JSON→Protobuf dual-publish seam."""

from __future__ import annotations

import json
import struct
import time


def source_track_to_message(message_class, track: dict):
    message = message_class()
    fields = message.DESCRIPTOR.fields_by_name
    for key, value in track.items():
        name = "timestamp" if key == "_ts" else "source" if key == "_src" else key
        if name not in fields or value is None:
            continue
        # Decoders emit richer values than the flat contract models (nested
        # dicts, out-of-range numbers) and repeated fields need extend(), not
        # assignment — _assign skips what will not fit rather than failing the
        # whole message. The JSON view still carries everything.
        _assign(message, fields[name], name, value)
    return message


# The four egress formats are siblings under the same message-kind segment,
# named for what they ARE rather than numbered. Formats do not supersede one
# another — all four are published permanently — so a version number would be
# the wrong word. Versions stay on the control plane (@config/v1), where a
# contract genuinely can revise.
#
#   .../json/tracks      flat JSON, the readable view
#   .../tracks/sapient   BSI Flex 335 v2, the interop standard
#   .../tracks/proto     EFDI's own protobuf, full per-protocol detail
#   .../tracks/native    original wire bytes, nothing lost
# The four egress formats are siblings, named for what they ARE rather than
# numbered. Formats never supersede one another — all four publish permanently —
# so a version number would be the wrong word. Versions stay on the control
# plane (@config/v1), where a contract genuinely can revise.
#
# The format sits BEFORE the message-kind segment so the terminal position stays
# free for a track identity:
#
#   .../aircraft/json/tracks       flat JSON, the readable view
#   .../aircraft/sapient/tracks    BSI Flex 335 v2, the interop standard
#   .../aircraft/proto/tracks      EFDI's own protobuf, full per-protocol detail
#   .../aircraft/native/tracks     original wire bytes, nothing lost
_FORMATS = ("json", "sapient", "proto", "raw")


def _sibling(topic: str, fmt: str) -> str:
    """Swap the view of an object key, or append one if it has none.

    ONLY the final segment is considered. Scanning the whole key would be wrong:
    `sapient` is both a format name and the name of a SOURCE, so a key like
    .../air/sapient/unknown/aircraft/... would have its source rewritten and the
    track would be published under a different sensor.
    """
    parts = topic.split("/")
    if parts and parts[-1] in _FORMATS:
        parts[-1] = fmt
        return "/".join(parts)
    return topic + "/" + fmt


def _has_format(topic: str) -> bool:
    """A view suffix is always terminal — see _sibling for why only the last
    segment may be tested."""
    return topic.rsplit("/", 1)[-1] in _FORMATS


def dual_topic(topic: str) -> str:
    """Per-protocol protobuf view of an object key (.../{id} -> .../{id}/proto)."""
    if topic.endswith("/proto"):
        return topic
    return _sibling(topic, "proto") if _has_format(topic) else topic + "/proto"


def native_topic(topic: str) -> str:
    """Verbatim-source-bytes view of an object key (.../{id} -> .../{id}/raw)."""
    if topic.endswith("/raw"):
        return topic
    return _sibling(topic, "raw") if _has_format(topic) else topic + "/raw"


def publish_native(session, topic: str, payload: bytes, protocol: str, zenoh,
                   profile: str = "", content_type: str = "application/octet-stream",
                   received_timestamp: float = 0.0) -> None:
    """Publish source bytes verbatim inside a RawEnvelope protobuf.

    The decoded json/sapient/proto views are only ever as complete as what the
    decoder models. This carries the ORIGINAL wire bytes, so a consumer that needs a
    field EFDI does not decode can still recover it, and the fabric still
    carries protobuf (RawEnvelope) rather than a bare octet stream.

    Best-effort, like publish_dual: never let this break a working publisher.
    """
    from protocols.random.raw_envelope_pb2 import RawEnvelope

    try:
        envelope = RawEnvelope(
            received_timestamp=received_timestamp or time.time(),
            source=protocol,
            protocol=protocol,
            payload=bytes(payload),
            content_type=content_type,
        )
        if profile:
            envelope.profile = profile
        data = envelope.SerializeToString()
    except Exception as exc:  # noqa: BLE001
        print("native envelope failed for {}: {}".format(topic, exc), flush=True)
        return
    session.put(topic, data, encoding=zenoh.Encoding.APPLICATION_PROTOBUF)


def asterix_data_block(category: int, record: bytes) -> bytes:
    """Wrap one decoded ASTERIX record back into a standalone data block.

    A record on its own is not self-describing — the CAT byte and 2-byte total
    length live in the block header that framed it. Re-adding them makes each
    published sample a complete, standard-conformant ASTERIX data block that any
    off-the-shelf decoder can read.
    """
    total = 3 + len(record)
    if not 0 <= category <= 255 or total > 0xFFFF:
        raise ValueError("ASTERIX record does not fit a data block header")
    return bytes((category,)) + struct.pack(">H", total) + bytes(record)


def wrapped_track_message(
    message_class,
    track: dict,
    affiliation: str = "unknown",
    wrapper_field: str = "track",
):
    """Build a protocol wrapper message with a nested NormalizedTrack and the
    protocol-specific scalar fields alongside it.

    Every per-protocol contract in compose/protocols/*.proto follows this shape
    (see mavlink.proto, vmf.proto, vendor sapient/flex335.proto). Some contracts use
    a different nested field name such as `sensor` or `normalized`; callers can
    override `wrapper_field` for those cases.
    """
    from protocols.random.normalized_track_pb2 import NormalizedTrack

    message = message_class()
    getattr(message, wrapper_field).CopyFrom(
        normalized_track_message(NormalizedTrack, track, affiliation)
    )
    fields = message.DESCRIPTOR.fields_by_name
    for key, value in track.items():
        if key == wrapper_field or key not in fields or value is None:
            continue
        _assign(message, fields[key], key, value)
    return message


def _assign(message, descriptor, key: str, value) -> None:
    """Set one field, tolerating values the contract cannot hold.

    Repeated fields reject plain assignment (`msg.f = [...]` raises), so they
    have to be extended. Getting this wrong is not a lost field but a lost
    MESSAGE: the exception propagates out of the builder and publish_dual's
    guard then drops the whole protobuf sample.
    """
    # protobuf 7 (upb) dropped FieldDescriptor.label in favour of is_repeated;
    # fall back to the label constant so this works on older runtimes too.
    repeated = getattr(descriptor, "is_repeated", None)
    if repeated is None:
        repeated = getattr(descriptor, "label", None) == 3  # LABEL_REPEATED
    try:
        if repeated:
            if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
                return
            getattr(message, key).extend(value)
        else:
            setattr(message, key, value)
    except (TypeError, ValueError, AttributeError):
        # Richer than the flat contract models (nested dict, out-of-range
        # number, wrong element type) — skip the field, keep the message.
        return


# ---------------------------------------------------------------------------
# Topic taxonomy
#
#   {prefix}/{pod}/{domain}/{source}/{affiliation}/{entity}/{type}/{id}
#
# SAPIENT is the fabric contract, so the bare semantic key IS the SAPIENT
# message and carries no format marker. The other views hang off the same path:
# All four views are named, so nothing is implicit:
#   .../{id}/sapient  SapientMessage       (BSI Flex 335 v2) — the contract
#   .../{id}/json     flat JSON
#   .../{id}/proto    EFDI per-protocol protobuf
#   .../{id}/raw      original wire bytes
#
# `type` and `id` are per-OBJECT, so the key can only be built once the track is
# in hand. Publishers pass the semantic prefix up to {entity}; this appends the
# tail, in one place rather than at all 26 publish sites.
_LEGACY_TAIL = ("json", "sapient", "proto", "raw", "native", "tracks", "status",
                "alerts", "features", "routes", "observations")

# Order matters: the FIRST match becomes the object's key for its lifetime.
# Registration leads because it is the human-readable identity operators use
# (LY-ABC), with ICAO24 right behind it as the always-present fallback —
# registration needs a registry lookup and is not always resolved.
_ID_KEYS = ("registration", "icao24", "mmsi", "uid", "callsign",
            "track_num", "track_number", "radar_id", "object_id")


def _slug(value, fallback: str = "unknown") -> str:
    """One safe key segment: no '/', no '*', no whitespace."""
    text = str(value).strip().lower() if value is not None else ""
    out = "".join(c if (c.isalnum() or c in "._-") else "-" for c in text).strip("-")
    return out[:64] or fallback


def object_type(track: dict) -> str:
    """Specific type when the sensor knows it, `unknown` when it cannot.

    Cooperative sources (ADS-B, Remote ID) carry a real type; a radar return
    does not, and publishes `unknown` rather than inventing one.
    """
    for key in ("aircraft_type", "type", "vehicle_type", "sapient_class",
                "remote_id_ua_type", "object_class", "ship_type"):
        value = track.get(key)
        if isinstance(value, str) and value.strip():
            return _slug(value)
    return "unknown"


def object_id(track: dict) -> str:
    for key in _ID_KEYS:
        value = track.get(key)
        if value not in (None, "", 0):
            return _slug(value)
    return "unknown"


def semantic_topic(prefix: str, track: dict) -> str:
    """Append {type}/{id} to a publisher's semantic prefix.

    Tolerates prefixes still carrying a legacy trailing segment (a format name
    or `tracks`) so publishers can be migrated one at a time without a flag day.
    """
    parts = [p for p in prefix.split("/") if p != ""]
    while parts and parts[-1] in _LEGACY_TAIL:
        parts.pop()
    parts.append(object_type(track))
    parts.append(object_id(track))
    return "/".join(parts)


def publish_dual(
    session,
    topic: str,
    track: dict,
    message_class,
    zenoh,
    wrapper_field: str = "track",
) -> None:
    """Publish one track on its object key, in every view.

    `topic` is the publisher's SEMANTIC prefix (…/{domain}/{source}/{affil}/
    {entity}); the object key adds {type}/{id}. SAPIENT lands on the bare key —
    it is the fabric contract, so it needs no format marker — and the JSON,
    per-protocol and native views hang off it.

    Every leg after SAPIENT is best-effort: a schema mismatch in one view must
    never stop the others being delivered. Failures print, they do not raise.
    """
    key = semantic_topic(topic, track)

    # SAPIENT is the fabric contract and the view consumers are expected to
    # read, so it is published first — a failure in any other view must not
    # delay it. Every view is explicitly named; none is implicit.
    try:
        from protocols.sapient_encode import publish_sapient
        publish_sapient(session, key, track, zenoh)
    except Exception as exc:  # noqa: BLE001 — never break the remaining views
        print("sapient view failed for {}: {}".format(key, exc), flush=True)

    session.put(
        key + "/json",
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
        print("protobuf encode failed for {}: {}".format(key, exc), flush=True)
        return
    session.put(key + "/proto", payload, encoding=zenoh.Encoding.APPLICATION_PROTOBUF)


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
