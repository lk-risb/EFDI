"""Integration with MediaMTX (bridges/mediamtx) for the Streams WebUI tab.

No DB model for streams themselves: a drone path is whatever an operator
typed into FreeFlight's RTMP URL (e.g. RISB/SAK/drone4) — nothing about it
is pre-registered or persisted. This module exposes what MediaMTX itself
already knows: which paths currently exist (proxying its own control API,
same host via network_mode: host — see mediamtx.yml's apiAddress) and which
recorded segments a path has within the currently configured retention
window (mediamtx.yml's recordDeleteAfter, default 10 minutes, editable
below), so the WebUI can build a live tile grid plus a scrub-back bar
without owning any of that state itself.

It also exposes mediamtx.yml's own settings as editable fields (GET/PUT
/api/streams/config) — curated to the fields that file actually sets (see
its own comments for why each exists), not MediaMTX's full config schema,
which runs to several hundred internal keys. Same split sitaware_targets.py
documents for its own settings: what varies here, deployment-wide .env
elsewhere. Edited via ruamel.yaml's round-trip mode so this file's own
explanatory comments survive a save, and a successful write restarts
mediamtx (same /api/runtime/services/{name}/restart control.py already
exposes) so the change takes effect immediately, matching config.py's
zenoh-router flow.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from ruamel.yaml import YAML
from sqlalchemy.ext.asyncio import AsyncSession

from .control import _control
from .db import get_db
from .deps import require_role, write_audit

router = APIRouter(prefix="/api/streams", tags=["streams"])

_MEDIAMTX_API_URL = os.environ.get("MEDIAMTX_API_URL", "http://127.0.0.1:9997").rstrip("/")
_RECORDINGS_DIR = Path(os.environ.get("MEDIAMTX_RECORDINGS_DIR", "/mediamtx-recordings")).resolve()
_MEDIAMTX_YML_PATH = Path(os.environ.get("MEDIAMTX_CONFIG_PATH", "/mediamtx-config/mediamtx.yml"))
_RECENT_WINDOW_S = 10 * 60  # matches mediamtx.yml's recordDeleteAfter — kept in sync with
                            # record_retention_minutes below by _read_settings/_write_settings

# mediamtx.yml's recordPath: ./recordings/%path/%Y-%m-%d_%H-%M-%S-%f + recordFormat
# fmp4 -> literal ".mp4" appended by mediamtx itself.
_SEGMENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}\.mp4$")


def _mediamtx_api(path: str) -> dict:
    request = urllib.request.Request(_MEDIAMTX_API_URL + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read(4_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"MediaMTX API: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"MediaMTX API unavailable: {exc}") from exc


def _recordings_dir_for(stream_path: str) -> Path:
    # Drone-chosen paths can contain '/' (RISB/SAK/drone4) — every segment
    # must still resolve to a real descendant of _RECORDINGS_DIR, same guard
    # shape as logs.py's _log_path for a single segment.
    if not stream_path or stream_path.startswith("/") or ".." in stream_path.split("/"):
        raise HTTPException(status_code=400, detail="invalid stream path")
    candidate = (_RECORDINGS_DIR / stream_path).resolve()
    try:
        candidate.relative_to(_RECORDINGS_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid stream path")
    return candidate


@router.get("")
async def list_streams(_=Depends(require_role("readonly", "admin", "superadmin"))):
    """Every path MediaMTX currently knows about, for the live tile grid."""
    data = _mediamtx_api("/v3/paths/list")
    items = data.get("items") or []
    return [
        {
            "name": item.get("name"),
            "ready": bool(item.get("ready")),
            "tracks": item.get("tracks") or [],
            "readers": len(item.get("readers") or []),
            "bytes_received": item.get("bytesReceived"),
        }
        for item in items
        if item.get("name")
    ]


# ---------------------------------------------------------------------------
# mediamtx.yml settings — curated to the fields that file actually sets.
# ---------------------------------------------------------------------------

_LOG_LEVELS = {"error", "warn", "info", "debug"}
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
_DURATION_RE = re.compile(r"^(\d+)([smh])$")


class MediamtxSettings(BaseModel):
    log_level: str = "info"
    rtmp_enabled: bool = True
    rtsp_enabled: bool = True
    rtsp_tcp_only: bool = True
    webrtc_enabled: bool = True
    webrtc_encryption: bool = False
    srt_enabled: bool = False
    record_segment_minutes: int = Field(1, ge=1, le=60)
    record_retention_minutes: int = Field(10, ge=1, le=1440)


def _duration_to_minutes(value, default_minutes: int) -> int:
    match = _DURATION_RE.fullmatch(str(value).strip()) if value is not None else None
    if not match:
        return default_minutes
    amount, unit = int(match.group(1)), match.group(2)
    return max(1, round(amount * _DURATION_UNIT_SECONDS[unit] / 60))


def _minutes_to_duration(minutes: int) -> str:
    return f"{minutes}m"


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    return yaml


def _load_yaml_doc():
    yaml = _yaml()
    try:
        with _MEDIAMTX_YML_PATH.open("r", encoding="utf-8") as handle:
            return yaml, yaml.load(handle)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"mediamtx.yml not found at {_MEDIAMTX_YML_PATH}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"mediamtx.yml is not valid YAML: {exc}") from exc


def _settings_from_doc(doc) -> MediamtxSettings:
    all_others = (doc.get("paths") or {}).get("all_others") or {}
    transports = list(doc.get("rtspTransports") or ["tcp"])
    return MediamtxSettings(
        log_level=str(doc.get("logLevel", "info")),
        rtmp_enabled=bool(doc.get("rtmp", True)),
        rtsp_enabled=bool(doc.get("rtsp", True)),
        rtsp_tcp_only=(transports == ["tcp"]),
        webrtc_enabled=bool(doc.get("webrtc", True)),
        webrtc_encryption=bool(doc.get("webrtcEncryption", False)),
        srt_enabled=bool(doc.get("srt", False)),
        record_segment_minutes=_duration_to_minutes(all_others.get("recordSegmentDuration"), 1),
        record_retention_minutes=_duration_to_minutes(all_others.get("recordDeleteAfter"), 10),
    )


def _apply_settings_to_doc(doc, settings: MediamtxSettings) -> None:
    doc["logLevel"] = settings.log_level
    doc["rtmp"] = settings.rtmp_enabled
    doc["rtsp"] = settings.rtsp_enabled
    doc["rtspTransports"] = ["tcp"] if settings.rtsp_tcp_only else ["udp", "multicast", "tcp"]
    doc["webrtc"] = settings.webrtc_enabled
    doc["webrtcEncryption"] = settings.webrtc_encryption
    doc["srt"] = settings.srt_enabled
    paths = doc.setdefault("paths", {})
    all_others = paths.setdefault("all_others", {})
    all_others["recordSegmentDuration"] = _minutes_to_duration(settings.record_segment_minutes)
    all_others["recordDeleteAfter"] = _minutes_to_duration(settings.record_retention_minutes)


@router.get("/config", response_model=MediamtxSettings)
async def get_mediamtx_settings(_=Depends(require_role("readonly", "admin", "superadmin"))):
    _, doc = _load_yaml_doc()
    return _settings_from_doc(doc)


@router.put("/config")
async def update_mediamtx_settings(
    settings: MediamtxSettings,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    if settings.log_level not in _LOG_LEVELS:
        raise HTTPException(status_code=400, detail=f"log_level must be one of {sorted(_LOG_LEVELS)}")
    yaml, doc = _load_yaml_doc()
    _apply_settings_to_doc(doc, settings)
    tmp_path = _MEDIAMTX_YML_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.dump(doc, handle)
    os.replace(tmp_path, _MEDIAMTX_YML_PATH)

    # Same restart control.py's own /api/runtime/services/{name}/restart uses —
    # applied here directly so saving takes effect immediately, matching
    # config.py's zenoh-router flow, without a second manual step.
    try:
        _control("/v1/services/mediamtx/restart", method="POST")
        restarted = True
    except HTTPException:
        restarted = False  # config is saved either way; retry the restart from Runtime Control

    await write_audit(db, actor.id, "mediamtx_config_updated", json.dumps(settings.model_dump()))
    return {**_settings_from_doc(doc).model_dump(), "restarted": restarted}


def _current_recording_retention_seconds() -> int:
    try:
        _, doc = _load_yaml_doc()
        return _settings_from_doc(doc).record_retention_minutes * 60
    except HTTPException:
        return _RECENT_WINDOW_S  # mediamtx.yml unreadable — fall back to the documented default


@router.get("/{stream_path:path}/recordings")
async def list_recordings(stream_path: str, _=Depends(require_role("readonly", "admin", "superadmin"))):
    """Recorded segments within the currently configured retention window for
    this path, oldest first — the enlarged tile's scrub bar maps its range
    onto this list."""
    directory = _recordings_dir_for(stream_path)
    if not directory.is_dir():
        return []
    cutoff = time.time() - _current_recording_retention_seconds()
    segments = []
    for entry in sorted(directory.iterdir()):
        if not _SEGMENT_RE.fullmatch(entry.name):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        segments.append({"file": entry.name, "modified": mtime})
    return segments


@router.get("/{stream_path:path}/recordings/{filename}")
async def get_recording(
    stream_path: str, filename: str,
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    """Serves one recorded segment. Each fMP4 segment is independently
    playable, and FastAPI's FileResponse handles Range requests natively —
    the browser's <video> element can scrub within it without extra work."""
    if not _SEGMENT_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="invalid segment filename")
    file_path = _recordings_dir_for(stream_path) / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(file_path, media_type="video/mp4")
