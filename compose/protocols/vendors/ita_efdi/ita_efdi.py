#!/usr/bin/env python3
"""ITA-EFDI's drone and radar feeds on the EFDI Backbone trial fabric ->
EFDI tracks.

Three topics, two slots, all wire format JSON — the portal's uploaded
schemas for these (DroneTelemetry, DroneSensor, RadarSensor/TrackList/...)
are structural mirrors for documentation/compat-checking only, same as
tacvox's own schema; no .proto/generated bindings needed here, same reason
tacvox.py has none. Field names below are the protobuf-JSON-mapping
camelCase names the schema viewer showed (e.g. "haeM", "courseDeg"), since
that's what a JSON producer built against that schema actually emits.

  <slot 46625c9d.../ITA-EFDI/DRONE-01/v1>  DroneTelemetry — CoT-shaped:
    cotType/cotHow, position{latitude,longitude,haeM}, flight{speedMps,
    headingDeg}. Its own schema comment calls it "the simulated drone" —
    noted, but not flagged synthetic/fabricated the way hellman/orion/
    sitaware's schemas explicitly are, so still decoded here.

  <slot 45d4f055.../ITA-EFDI/DRONE-02/v1>  DroneSensor — directly CoT-shaped:
    point{lat,lon,hae}, detail.track{speed,course}, detail.contact{callsign}.

  <slot 46625c9d.../ITA-EFDI/RADAR-01/v1>  same slot as DRONE-01, a real
    radar site. Multiple message kinds can land on this one topic (TrackList,
    RadarSensor, RadarStatus, SweepResult, HealthResponse) with no shape
    marker beyond their own JSON keys, so this sniffs by key presence and
    only decodes the two with map value: TrackList (a live track list — the
    same radar-site pattern tak_layer.py already renders for CAT-34) and
    RadarSensor (the site's own fixed antenna position, decoded once as a
    static sensor marker like protocols/random/camera_sites.py does).
"""

from __future__ import annotations

import json
import time

from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

_DRONE01_SUFFIX = "/ITA-EFDI/DRONE-01/v1"
_DRONE02_SUFFIX = "/ITA-EFDI/DRONE-02/v1"
_RADAR01_SUFFIX = "/ITA-EFDI/RADAR-01/v1"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

_AFFILIATION_BY_CODE = {"f": "friendly", "h": "hostile", "n": "neutral", "u": "unknown"}
_DOMAIN_BY_CODE = {"A": "air", "G": "land", "S": "sea", "P": "space"}


def _cot_affiliation(cot_type: str) -> str:
    parts = (cot_type or "").split("-")
    return _AFFILIATION_BY_CODE.get(parts[1].lower(), "unknown") if len(parts) >= 2 else "unknown"


def _cot_domain(cot_type: str) -> str:
    parts = (cot_type or "").split("-")
    return _DOMAIN_BY_CODE.get(parts[2].upper(), "air") if len(parts) >= 3 else "air"


def drone01_record(payload: dict) -> tuple[dict, str] | None:
    position = payload.get("position")
    if not isinstance(position, dict):
        return None
    lat, lon = position.get("latitude"), position.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or (lat == 0 and lon == 0):
        return None
    cot_type = payload.get("cotType", "")
    record = {
        "_ts": time.time(),
        "_src": "backbone:ita_efdi:drone01:{}".format(payload.get("uid", "unknown")),
        "uid": payload.get("uid") or "ita-efdi-drone01",
        "lat_deg": lat,
        "lon_deg": lon,
        "label": payload.get("callsign") or payload.get("uid") or "drone",
    }
    if isinstance(position.get("haeM"), (int, float)):
        record["alt_m"] = position["haeM"]
    flight = payload.get("flight")
    if isinstance(flight, dict):
        if isinstance(flight.get("speedMps"), (int, float)):
            record["speed_mps"] = flight["speedMps"]
        heading = flight.get("headingDeg", flight.get("courseDeg"))
        if isinstance(heading, (int, float)):
            record["heading_deg"] = heading
    sensor = payload.get("sensor")
    if isinstance(sensor, dict) and sensor.get("platform"):
        record["platform"] = sensor["platform"]
    return record, _cot_domain(cot_type), _cot_affiliation(cot_type)


def drone02_record(payload: dict) -> tuple[dict, str] | None:
    point = payload.get("point")
    if not isinstance(point, dict):
        return None
    lat, lon = point.get("lat"), point.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or (lat == 0 and lon == 0):
        return None
    cot_type = payload.get("type", "")
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    contact = detail.get("contact") if isinstance(detail.get("contact"), dict) else {}
    track = detail.get("track") if isinstance(detail.get("track"), dict) else {}
    record = {
        "_ts": time.time(),
        "_src": "backbone:ita_efdi:drone02:{}".format(payload.get("uid", "unknown")),
        "uid": payload.get("uid") or "ita-efdi-drone02",
        "lat_deg": lat,
        "lon_deg": lon,
        "label": contact.get("callsign") or payload.get("uid") or "drone",
    }
    if isinstance(point.get("hae"), (int, float)):
        record["alt_m"] = point["hae"]
    if isinstance(track.get("speed"), (int, float)):
        record["speed_mps"] = track["speed"]
    if isinstance(track.get("course"), (int, float)):
        record["heading_deg"] = track["course"]
    if contact.get("callsign"):
        record["callsign"] = contact["callsign"]
    return record, _cot_domain(cot_type), _cot_affiliation(cot_type)


_RADAR_AFFILIATION = {"AFF_FRIENDLY": "friendly", "AFF_HOSTILE": "hostile", "AFF_UNKNOWN": "unknown"}


def radar_track_records(payload: dict) -> list[dict]:
    """TrackList: {count, site, tracks: [...]}."""
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return []
    out = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        position = track.get("position")
        if not isinstance(position, dict):
            continue
        lat, lon = position.get("lat"), position.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or (lat == 0 and lon == 0):
            continue
        record = {
            "_ts": time.time(),
            "_src": "backbone:ita_efdi:radar-track",
            "uid": track.get("trackId") or "ita-efdi-radar-track",
            "lat_deg": lat,
            "lon_deg": lon,
            "label": track.get("callsign") or track.get("trackId") or "radar contact",
            "affiliation": _RADAR_AFFILIATION.get(track.get("affiliation"), "unknown"),
        }
        if isinstance(position.get("haeM"), (int, float)):
            record["alt_m"] = position["haeM"]
        velocity = track.get("velocity")
        if isinstance(velocity, dict):
            if isinstance(velocity.get("speedMps"), (int, float)):
                record["speed_mps"] = velocity["speedMps"]
            if isinstance(velocity.get("courseDeg"), (int, float)):
                record["heading_deg"] = velocity["courseDeg"]
        out.append(record)
    return out


def radar_site_record(payload: dict) -> dict | None:
    """RadarSensor: {sensorId, callsign, position, zenoh, ...} — the site's
    own fixed antenna, decoded once as a static sensor marker."""
    position = payload.get("position")
    if not isinstance(position, dict):
        return None
    lat, lon = position.get("lat"), position.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or (lat == 0 and lon == 0):
        return None
    return {
        "_ts": time.time(),
        "_src": "backbone:ita_efdi:radar-site",
        "sensor_type": "radar",
        "sensor_id": payload.get("sensorId") or "ita-efdi-radar",
        "sensor_name": payload.get("callsign") or payload.get("sensorId") or "ITA-EFDI radar",
        "lat_deg": lat,
        "lon_deg": lon,
        "is_online": True,
    }


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("ita_efdi: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        try:
            payload = json.loads(payload_bytes(sample).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        try:
            if key.endswith(_DRONE01_SUFFIX):
                result = drone01_record(payload)
                if result:
                    record, domain, affiliation = result
                    topic = "{}/{}/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, domain, affiliation)
                    session.put(topic, json.dumps(record).encode())
            elif key.endswith(_DRONE02_SUFFIX):
                result = drone02_record(payload)
                if result:
                    record, domain, affiliation = result
                    topic = "{}/{}/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, domain, affiliation)
                    session.put(topic, json.dumps(record).encode())
            elif key.endswith(_RADAR01_SUFFIX):
                for record in radar_track_records(payload):
                    affiliation = record.pop("affiliation")
                    topic = "{}/air/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, affiliation)
                    session.put(topic, json.dumps(record).encode())
                site = radar_site_record(payload)
                if site:
                    topic = TOPIC_ROOT + "/land/backbone/neutral/sensor/tracks/v1"
                    session.put(topic, json.dumps(site).encode())
        except Exception as exc:
            print("ita_efdi decode error on {}: {}".format(key, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("ita_efdi: {} -> tracks (DRONE-01, DRONE-02, RADAR-01)".format(INPUT_TOPIC), flush=True)
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
