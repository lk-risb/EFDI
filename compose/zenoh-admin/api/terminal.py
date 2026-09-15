"""Live in-memory registry of every EFDI-tracked drone/UAV and ground sensor,
feeding the WebUI's Terminal tab map. Replaces sensors.py's single-vendor
(dronuradaras-only) list: this subscribes broadly instead of to one vendor's
key expression, same background-subscriber-held-for-app-lifespan shape as
sensors.py and topics.py's start_topic_observer().

Two independent in-memory caches, one per subscriber:

  _DRONES  — anything under air/**. Every bridge that publishes tracks
             (mainline_terminal, STANAG 4586/5516 via fusion.py, NFFI,
             ASTERIX radar/ADS-B) writes a JSON view at the bare object key
             (see protocols/track_views.py's view_key(): "json is the
             canonical view and carries NO format segment"), built from the
             same dict shape protocols/proto/normalized_track.proto defines
             (uid, callsign, classification, affiliation, lat_deg, lon_deg,
             baro_alt_m/geo_alt_m/height_m, speed_ms, heading_deg, on_ground,
             emergency, deleted, _ts/_src) — confirmed by reading
             track_views.py's source_track_to_message(), which maps a
             track dict's keys onto that proto's fields by name, so any
             bridge using publish_dual()/publish_collection() must already
             populate these names for its protobuf view to come out right.
             mainline_terminal (terminal_bridge.py) is the one exception:
             it does not go through publish_dual(), so its track dict uses
             its own field names (alt_m, ground_speed_kts, armed,
             flight_state, battery_pct, mission_id, mission_phase, alerts)
             instead — handled as a second known shape below.

  _SENSORS — anything under land/**/sensor/**. Today this is only
             mainline_dronuradaras's acoustic sensor nodes (sensor_id,
             sensor_name, lat_deg, lon_deg, is_online, last_seen,
             last_detection_ts, last_detection_audio_url) — the exact same
             payload sensors.py's _KEY_EXPR watched, just generalized to the
             wildcard tak_layer.py's own "land/**/*/sensor/**" CoT-type table
             already uses, so a future sensor vendor needs no change here.

Known limitation: a track re-published under a different uid by a later
stage (e.g. fusion.py's fused output alongside the raw source track it fused
from) shows as two separate map entities, same as it shows as two separate
CoT markers in TAK today — this module does not attempt cross-source
deduplication that tak_layer.py itself does not do either.

Malformed or non-JSON payloads (the proto/sapient/native sibling views under
the same air/**/land/** trees) are silently skipped, same defensive
try/except json.loads() pattern sensors.py already uses — cheaper than
parsing each key to exclude known format segments first, and just as
correct since those views are never valid JSON to begin with.
"""

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from .deps import require_role
from .local_zenoh import open_local_session

router = APIRouter(prefix="/api/terminal", tags=["terminal"])

# OpenStreetMap's tile usage policy (operations.osmfoundation.org/policies/tiles)
# requires a real, identifying User-Agent and forbids uncached direct-from-browser
# use — a Leaflet <img> tag hitting tile servers straight from the client can't
# satisfy either (browsers won't let JS set a custom User-Agent, and every open
# tab would re-fetch the same tiles). Same fix TAK's admin/api/live_map.py
# already applies for its own live map: proxy and cache here instead, one
# fetch per tile ever, with our own identity in the request.
_TILE_CACHE_DIR = Path(os.environ.get("TERMINAL_TILE_CACHE_DIR", "/tmp/terminal-tile-cache"))
_TILE_USER_AGENT = "EFDI-Admin-Terminal/1.0 (self-hosted internal deployment)"

_LOCK = threading.Lock()
_DRONES: dict[str, dict] = {}    # uid -> latest known NormalizedTrack-shaped payload
_SENSORS: dict[str, dict] = {}   # sensor_id -> latest known sensor payload

_AIR_KEY_EXPR = "**/air/**"
_SENSOR_KEY_EXPR = "**/land/**/sensor/**"


def _observe_drone(sample) -> None:
    try:
        payload = json.loads(bytes(sample.payload).decode())
    except (ValueError, TypeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return
    uid = payload.get("uid")
    if not uid:
        return
    with _LOCK:
        if payload.get("deleted") or payload.get("_delete"):
            _DRONES.pop(uid, None)
            return
        _DRONES[uid] = payload


def _observe_sensor(sample) -> None:
    try:
        payload = json.loads(bytes(sample.payload).decode())
    except (ValueError, TypeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return
    sensor_id = payload.get("sensor_id")
    if not sensor_id:
        return
    with _LOCK:
        if payload.get("_delete"):
            _SENSORS.pop(sensor_id, None)
            return
        _SENSORS[sensor_id] = payload


def start_terminal_observer():
    try:
        session = open_local_session()
        session.declare_subscriber(_AIR_KEY_EXPR, _observe_drone)
        session.declare_subscriber(_SENSOR_KEY_EXPR, _observe_sensor)
        return session
    except Exception as exc:
        print(f"[terminal] observer not started: {exc}", flush=True)
        return None


def _speed_kts(payload: dict) -> float | None:
    # mainline_terminal already converts to knots at the source (see
    # terminal_bridge.py's _flight_state_to_track); NormalizedTrack-shaped
    # sources carry speed_ms instead.
    if payload.get("ground_speed_kts") is not None:
        return payload["ground_speed_kts"]
    speed_ms = payload.get("speed_ms")
    return speed_ms * 1.943844 if speed_ms is not None else None


def _alt_m(payload: dict) -> float | None:
    for key in ("alt_m", "baro_alt_m", "geo_alt_m", "height_m"):
        if payload.get(key) is not None:
            return payload[key]
    return None


def _drone_status(payload: dict) -> str:
    if payload.get("emergency"):
        return "emergency"
    if payload.get("flight_state"):
        return str(payload["flight_state"])
    if payload.get("on_ground"):
        return "on_ground"
    if payload.get("armed"):
        return "armed"
    return "airborne"


def _normalize_drone(payload: dict) -> dict | None:
    lat, lon = payload.get("lat_deg"), payload.get("lon_deg")
    if lat is None or lon is None:
        return None
    return {
        "id": payload["uid"],
        "kind": "drone",
        "source": payload.get("_src") or payload.get("source") or "unknown",
        "callsign": payload.get("callsign") or payload["uid"],
        "lat": lat,
        "lon": lon,
        "alt_m": _alt_m(payload),
        "heading_deg": payload.get("heading_deg"),
        "speed_kts": _speed_kts(payload),
        "status": _drone_status(payload),
        "updated_ts": payload.get("_ts") or payload.get("timestamp"),
        "raw": payload,
    }


def _normalize_sensor(payload: dict) -> dict | None:
    lat, lon = payload.get("lat_deg"), payload.get("lon_deg")
    if lat is None or lon is None:
        return None
    return {
        "id": payload["sensor_id"],
        "kind": "sensor",
        "source": payload.get("_src") or "unknown",
        "callsign": payload.get("sensor_name") or payload["sensor_id"],
        "lat": lat,
        "lon": lon,
        "alt_m": None,
        "heading_deg": None,
        "speed_kts": None,
        "status": "online" if payload.get("is_online") else "offline",
        "updated_ts": payload.get("_ts"),
        "raw": payload,
    }


def _fetch_tile(z: int, x: int, y: int) -> bytes:
    request = urllib.request.Request(
        f"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        headers={"User-Agent": _TILE_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


@router.get("/tiles/{z}/{x}/{y}.png")
async def get_tile(z: int, x: int, y: int):
    # No auth dependency, unlike every other route here — Leaflet requests
    # tiles as plain <img> tags, which can't carry this app's bearer token.
    # Tile imagery isn't sensitive; only the live entity positions above are.
    cache_path = _TILE_CACHE_DIR / str(z) / str(x) / f"{y}.png"
    if cache_path.is_file():
        return FileResponse(cache_path, media_type="image/png")
    try:
        content = await asyncio.to_thread(_fetch_tile, z, x, y)
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="Tile upstream error") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"Tile fetch failed: {exc}") from exc
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(content)
    return Response(content=content, media_type="image/png")


@router.get("/entities")
async def list_entities(_=Depends(require_role("readonly", "admin", "superadmin"))):
    with _LOCK:
        drones = list(_DRONES.values())
        sensors = list(_SENSORS.values())
    entities = []
    for payload in drones:
        entity = _normalize_drone(payload)
        if entity is not None:
            entities.append(entity)
    for payload in sensors:
        entity = _normalize_sensor(payload)
        if entity is not None:
            entities.append(entity)
    return {"entities": entities, "server_time": time.time()}
