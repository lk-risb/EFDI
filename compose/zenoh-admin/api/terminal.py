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
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

import zenoh
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .control import _control
from .db import get_db
from .deps import require_role, write_audit
from .local_zenoh import open_local_session
from .models import TerminalAsset, TerminalZone
from .topics import _data_prefix

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
_UNITS: dict[str, dict] = {}     # uid -> latest known ground/operator-position payload

_AIR_KEY_EXPR = "**/air/**"
_SENSOR_KEY_EXPR = "**/land/**/sensor/**"
# A ground/operator position (e.g. AARTOS's WiFi/direction-finding block,
# once wired — see aartos_json.py's _is_operator_track()/topic_for_track())
# publishes here, not under _AIR_KEY_EXPR or _SENSOR_KEY_EXPR — this was a
# real gap: that data already reaches tak_layer.py's identical
# "land/**/*/unit/**" wildcard and renders on TAK, but this module never
# subscribed to it, so it never reached this map at all.
_UNIT_KEY_EXPR = "**/land/**/unit/**"

# Reused for outbound command publishes too (see send_command below) instead
# of opening a fresh Zenoh session per request — same session
# start_terminal_observer() already holds for the app's lifespan.
_session = None

_ACKS: dict[str, dict] = {}   # entity_id -> latest {cmd, result, _ts, ...} ack
_ACK_KEY_EXPR = "**/air/mavlink/ack/**"

_COMMAND_CMDS = {"arm", "disarm", "takeoff", "rtl", "land", "hold", "goto"}

# A real (not fabricated) event log, matching mainline.inc TERMINAL's own
# "cmd goto accepted/executing/succeeded"-style event feed shown in its
# Events panel — but only for events this module can actually observe: a
# command being queued, its ack coming back, a drone's derived status
# changing, and a drone's health.alerts string appearing/changing (the same
# field terminal_bridge.py's _flight_state_to_track already joins from
# mainline.inc's own health data). No TERMINAL-side event we cannot see
# (its own internal mission/geofence logic, other operators' actions) is
# invented here.
_EVENTS: list[dict] = []       # newest last; capped, see _push_event
_EVENTS_CAP = 300
_LAST_STATUS: dict[str, str] = {}   # uid -> last _drone_status() seen
_LAST_ALERTS: dict[str, str] = {}   # uid -> last raw alerts string seen

# terminal_bridge.py joins each alert as "<message> (<severity>)" — see its
# own tests (test_flight_state_joins_health_alerts_into_one_string). Parsed
# back out here only to color the event row; a message with no parseable
# trailing "(word)" tag still shows, just without a specific severity color.
_ALERT_SEVERITY_RE = re.compile(r"\((\w+)\)\s*$")


def _push_event(entity_id: str, kind: str, severity: str, message: str, ts: float | None = None) -> None:
    event = {
        "ts": ts if ts is not None else time.time(),
        "entity_id": entity_id,
        "kind": kind,          # "cmd" | "status" | "alert"
        "severity": severity,  # "info" | "warn" | "crit"
        "message": message,
    }
    with _LOCK:
        _EVENTS.append(event)
        if len(_EVENTS) > _EVENTS_CAP:
            del _EVENTS[: len(_EVENTS) - _EVENTS_CAP]


def _topic_root() -> str:
    """This pod's <prefix>/<PARTNER_NAMESPACE> data root.

    Same shape as compose/control/namespace_prefix.py's topic_root(), used by
    every native bridge (e.g. mavlink_command_bridge.py) to build its command
    key expression. This container never mounts compose/control (it isn't in
    the zenoh-admin image), but topics.py's own _data_prefix() reads the exact
    same bind-mounted state files (see docker-compose.yml's zenoh-admin
    NAMESPACE_PREFIX_FILE/DATA_NAMESPACE_PREFIX_FILE env vars, both pointed at
    the same POD_STATE_DIR files namespace_prefix.py resolves natively) — so
    the two always agree without this module reimplementing file resolution.
    """
    namespace = os.environ.get("PARTNER_NAMESPACE", "").strip("/")
    return "/".join(part for part in (_data_prefix(), namespace) if part)


def _cmd_topic(entity_id: str) -> str:
    return "{}/air/mavlink/cmd/{}".format(_topic_root(), entity_id)


def _observe_ack(sample) -> None:
    try:
        payload = json.loads(bytes(sample.payload).decode())
    except (ValueError, TypeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return
    entity_id = str(sample.key_expr).rsplit("/", 1)[-1]
    with _LOCK:
        _ACKS[entity_id] = payload
    result = payload.get("result")
    severity = "info" if result in ("MAV_RESULT_ACCEPTED", "MAV_RESULT_IN_PROGRESS") else "warn"
    cmd = payload.get("cmd") or "?"
    detail = " — {}".format(payload["detail"]) if payload.get("detail") else ""
    _push_event(entity_id, "cmd", severity, "{} {}{}".format(cmd, result or "no ack", detail), payload.get("_ts"))


class CommandIn(BaseModel):
    cmd: Literal["arm", "disarm", "takeoff", "rtl", "land", "hold", "goto"]
    alt_m: float | None = None
    lat_deg: float | None = None
    lon_deg: float | None = None


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
            _LAST_STATUS.pop(uid, None)
            _LAST_ALERTS.pop(uid, None)
            return
        _DRONES[uid] = payload

    # Only log a transition, not the first-ever sighting of a uid — every
    # entity present at observer startup would otherwise log a spurious
    # "status -> X" the instant this process (re)connects, drowning out
    # real transitions in a fleet of any size.
    status = _drone_status(payload)
    previous_status = _LAST_STATUS.get(uid)
    _LAST_STATUS[uid] = status
    if previous_status is not None and previous_status != status:
        severity = "crit" if status == "emergency" else "info"
        _push_event(uid, "status", severity, "status -> {}".format(status), payload.get("_ts"))

    alerts = payload.get("alerts")
    previous_alerts = _LAST_ALERTS.get(uid, "")
    if alerts:
        _LAST_ALERTS[uid] = alerts
    elif uid in _LAST_ALERTS:
        del _LAST_ALERTS[uid]
    if alerts and alerts != previous_alerts and previous_status is not None:
        match = _ALERT_SEVERITY_RE.search(alerts)
        severity = {"crit": "crit", "critical": "crit", "warn": "warn", "warning": "warn"}.get(
            (match.group(1).lower() if match else ""), "warn"
        )
        _push_event(uid, "alert", severity, alerts, payload.get("_ts"))


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


def _observe_unit(sample) -> None:
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
            _UNITS.pop(uid, None)
            return
        _UNITS[uid] = payload


def start_terminal_observer():
    global _session
    try:
        session = open_local_session()
        session.declare_subscriber(_AIR_KEY_EXPR, _observe_drone)
        session.declare_subscriber(_SENSOR_KEY_EXPR, _observe_sensor)
        session.declare_subscriber(_UNIT_KEY_EXPR, _observe_unit)
        session.declare_subscriber(_ACK_KEY_EXPR, _observe_ack)
        _session = session
        return session
    except Exception as exc:
        print(f"[terminal] observer not started: {exc}", flush=True)
        return None


# Infrastructure/system health events — mirrors mainline.inc TERMINAL's own
# "world-sim heartbeat is unhealthy" / "auto-operator heartbeat is unhealthy"
# alerts (source MAINFRAME), which are service-health checks, not anything
# derived from a tracked entity. This backend already computes exactly that
# signal for every native bridge/layer via admin_control.py's GET /v1/runtime
# (proxied here as control.py's _control("/v1/runtime")) — reused rather than
# re-implemented, so this can never drift from what the Runtime Control page
# itself shows.
_HEALTH_POLL_INTERVAL_S = 10
_LAST_SERVICE_STATUS: dict[str, str] = {}
_SERVICE_STATUS_SEVERITY = {
    "running": "info",
    "stopped": "info",
    "crashed": "crit",
    "degraded": "warn",
    "needs-config": "warn",
    "unavailable": "warn",
}


async def _poll_system_health() -> None:
    while True:
        try:
            data = await asyncio.to_thread(_control, "/v1/runtime")
            for svc in data.get("services", []):
                name, status = svc.get("name"), svc.get("status")
                if not name or status is None:
                    continue
                # Only log a transition, not the first-ever poll — every
                # configured service would otherwise log a spurious
                # "-> running" the instant this process (re)starts.
                previous = _LAST_SERVICE_STATUS.get(name)
                _LAST_SERVICE_STATUS[name] = status
                if previous is None or previous == status:
                    continue
                severity = _SERVICE_STATUS_SEVERITY.get(status, "warn")
                _push_event(name, "system", severity, "{} -> {}".format(name, status))
        except Exception as exc:
            print(f"[terminal] system health poll failed: {exc}", flush=True)
        await asyncio.sleep(_HEALTH_POLL_INTERVAL_S)


def start_system_health_poller(loop):
    return loop.create_task(_poll_system_health())


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


def _normalize_unit(payload: dict) -> dict | None:
    lat, lon = payload.get("lat_deg"), payload.get("lon_deg")
    if lat is None or lon is None:
        return None
    return {
        "id": payload["uid"],
        "kind": "unit",
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
        units = list(_UNITS.values())
    entities = []
    for payload in drones:
        entity = _normalize_drone(payload)
        if entity is not None:
            entities.append(entity)
    for payload in sensors:
        entity = _normalize_sensor(payload)
        if entity is not None:
            entities.append(entity)
    for payload in units:
        entity = _normalize_unit(payload)
        if entity is not None:
            entities.append(entity)
    return {"entities": entities, "server_time": time.time()}


@router.post("/entities/{entity_id}/command", status_code=202)
async def send_command(
    entity_id: str,
    request: CommandIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("admin", "superadmin")),
):
    """Publish a drone command for a bridge (currently mavlink_command_bridge.py)
    to pick up and execute — see that file's module docstring for the exact
    command flow and JSON shape. This only confirms the command was published;
    whether the drone actually accepted it comes back asynchronously on the
    <topic_root>/air/mavlink/ack/<entity_id> topic, polled via the ack route
    below (mirrors mainline.inc TERMINAL's own async "goto executing" pattern).
    """
    if request.cmd == "goto" and (request.lat_deg is None or request.lon_deg is None):
        raise HTTPException(status_code=422, detail="goto requires lat_deg and lon_deg")
    if _session is None:
        raise HTTPException(status_code=503, detail="Zenoh session not available")

    payload = {"cmd": request.cmd}
    if request.alt_m is not None:
        payload["alt_m"] = request.alt_m
    if request.lat_deg is not None:
        payload["lat_deg"] = request.lat_deg
    if request.lon_deg is not None:
        payload["lon_deg"] = request.lon_deg

    _session.put(
        _cmd_topic(entity_id),
        json.dumps(payload).encode(),
        encoding=zenoh.Encoding.APPLICATION_JSON,
    )
    await write_audit(db, actor.id, "terminal_command", "{}: {}".format(entity_id, request.cmd))
    _push_event(entity_id, "cmd", "info", "{} queued by {}".format(request.cmd, actor.username))
    return {"queued": True, "entity_id": entity_id, "cmd": request.cmd}


@router.get("/entities/{entity_id}/command/ack")
async def get_command_ack(
    entity_id: str,
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    with _LOCK:
        ack = _ACKS.get(entity_id)
    return {"ack": ack}


@router.get("/events")
async def list_events(
    limit: int = 200,
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    """Newest-first slice of the real event log (see _push_event) — command
    lifecycle, drone status transitions, and health.alerts changes only.
    Feeds the WebUI's Events/Alerts panels and timeline strip."""
    limit = max(1, min(limit, _EVENTS_CAP))
    with _LOCK:
        events = list(_EVENTS[-limit:])
    events.reverse()
    return {"events": events, "server_time": time.time()}


# ── Assets: operator-placed POI markers (GSM towers, buildings, rally
# points) — mainline.inc TERMINAL's "Assets" tab/"Manage assets" link. Never
# detected from any sensor feed; these exist purely because an admin placed
# them, so writes are admin/superadmin-only while any signed-in role can
# view them (same read/write split as topics.py's registrations).

_ASSET_CATEGORIES = {"generic", "gsm_tower", "building", "industrial", "rally_point"}


class AssetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category: str = "generic"
    lat_deg: float
    lon_deg: float
    description: str = Field(default="", max_length=512)


def _asset_out(asset: TerminalAsset) -> dict:
    return {
        "id": asset.id,
        "name": asset.name,
        "category": asset.category,
        "lat": asset.lat_deg,
        "lon": asset.lon_deg,
        "description": asset.description,
        "created_at": asset.created_at.isoformat(),
    }


@router.get("/assets")
async def list_assets(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    result = await db.execute(select(TerminalAsset).order_by(TerminalAsset.name))
    return {"assets": [_asset_out(a) for a in result.scalars()]}


@router.post("/assets", status_code=201)
async def create_asset(
    request: AssetIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("admin", "superadmin")),
):
    category = request.category.strip().lower()
    if category not in _ASSET_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of {sorted(_ASSET_CATEGORIES)}")
    if not (-90 <= request.lat_deg <= 90) or not (-180 <= request.lon_deg <= 180):
        raise HTTPException(status_code=422, detail="lat_deg/lon_deg out of range")
    asset = TerminalAsset(
        name=request.name.strip(),
        category=category,
        lat_deg=request.lat_deg,
        lon_deg=request.lon_deg,
        description=request.description.strip(),
        created_by=actor.id,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    await write_audit(db, actor.id, "create_terminal_asset", asset.name)
    return _asset_out(asset)


@router.delete("/assets/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("admin", "superadmin")),
):
    asset = await db.get(TerminalAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    name = asset.name
    await db.delete(asset)
    await db.commit()
    await write_audit(db, actor.id, "delete_terminal_asset", name)


# ── Zones: operator-drawn circular geofences/AOs — mainline.inc TERMINAL's
# "Bounding Box"/"Alpha Zona" markers, drawn in its Plan module. A circle
# (center + radius), not a full polygon editor — the smallest honest version
# of the real feature. Tier mirrors this app's existing info/warn/crit
# severity scale (see TerminalEvent) rather than a separate vocabulary.

_ZONE_TIERS = {"info", "warn", "crit"}


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    tier: str = "info"
    center_lat_deg: float
    center_lon_deg: float
    radius_m: float = Field(gt=0, le=200_000)
    description: str = Field(default="", max_length=512)


def _zone_out(zone: TerminalZone) -> dict:
    return {
        "id": zone.id,
        "name": zone.name,
        "tier": zone.tier,
        "center_lat": zone.center_lat_deg,
        "center_lon": zone.center_lon_deg,
        "radius_m": zone.radius_m,
        "description": zone.description,
        "created_at": zone.created_at.isoformat(),
    }


@router.get("/zones")
async def list_zones(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    result = await db.execute(select(TerminalZone).order_by(TerminalZone.name))
    return {"zones": [_zone_out(z) for z in result.scalars()]}


@router.post("/zones", status_code=201)
async def create_zone(
    request: ZoneIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("admin", "superadmin")),
):
    tier = request.tier.strip().lower()
    if tier not in _ZONE_TIERS:
        raise HTTPException(status_code=422, detail=f"tier must be one of {sorted(_ZONE_TIERS)}")
    if not (-90 <= request.center_lat_deg <= 90) or not (-180 <= request.center_lon_deg <= 180):
        raise HTTPException(status_code=422, detail="center_lat_deg/center_lon_deg out of range")
    zone = TerminalZone(
        name=request.name.strip(),
        tier=tier,
        center_lat_deg=request.center_lat_deg,
        center_lon_deg=request.center_lon_deg,
        radius_m=request.radius_m,
        description=request.description.strip(),
        created_by=actor.id,
    )
    db.add(zone)
    await db.commit()
    await db.refresh(zone)
    await write_audit(db, actor.id, "create_terminal_zone", zone.name)
    return _zone_out(zone)


@router.delete("/zones/{zone_id}", status_code=204)
async def delete_zone(
    zone_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("admin", "superadmin")),
):
    zone = await db.get(TerminalZone, zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="zone not found")
    name = zone.name
    await db.delete(zone)
    await db.commit()
    await write_audit(db, actor.id, "delete_terminal_zone", name)
