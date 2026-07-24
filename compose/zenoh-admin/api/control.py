"""Admin API for the host PID-managed bridge/layer runtime."""

from __future__ import annotations

import json
import hashlib
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import require_role, write_audit


router = APIRouter(prefix="/api/runtime", tags=["runtime"])
_CONTROL_URL = os.environ.get("EFDI_CONTROL_URL", "http://127.0.0.1:18896").rstrip("/")
_EXPLICIT_CONTROL_TOKEN = os.environ.get("EFDI_CONTROL_TOKEN", "")
_ADMIN_SECRET = os.environ.get("ZENOH_ADMIN_SECRET_KEY", "")
_CONTROL_TOKEN = _EXPLICIT_CONTROL_TOKEN or (
    hashlib.sha256(f"efdi-control-v1:{_ADMIN_SECRET}".encode()).hexdigest()
    if _ADMIN_SECRET else ""
)


class RuntimeConfigRequest(BaseModel):
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class RuntimeSelectionRequest(BaseModel):
    selected_services: list[str] = Field(default_factory=list)


def _control(path: str, method: str = "GET", body: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if _CONTROL_TOKEN:
        headers["Authorization"] = f"Bearer {_CONTROL_TOKEN}"
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(_CONTROL_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(256_000).decode("utf-8")).get("detail", exc.reason)
        except (ValueError, OSError):
            detail = str(exc.reason)
        raise HTTPException(status_code=502, detail=f"Host control agent: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Host control agent unavailable: {exc}") from exc


@router.get("/catalog")
async def get_catalog(_=Depends(require_role("readonly", "admin", "superadmin"))):
    return _control("/v1/catalog")


@router.get("")
async def get_runtime(_=Depends(require_role("readonly", "admin", "superadmin"))):
    return _control("/v1/runtime")


@router.get("/logs/{name}")
async def get_runtime_logs(name: str, _=Depends(require_role("readonly", "admin", "superadmin"))):
    if not name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in name):
        raise HTTPException(status_code=400, detail="invalid service name")
    return _control(f"/v1/logs/{name}")


@router.put("/config")
async def update_runtime_config(
    body: RuntimeConfigRequest,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    result = _control("/v1/config", method="PUT", body={"values": body.values})
    await write_audit(db, actor.id, "update_runtime_config", ",".join(result.get("updated", [])))
    return result


@router.post("/services/{name}/{action}")
async def service_action(
    name: str,
    action: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=400, detail="action must be start, stop, or restart")
    if not name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in name):
        raise HTTPException(status_code=400, detail="invalid service name")
    result = _control(f"/v1/services/{name}/{action}", method="POST")
    await write_audit(db, actor.id, f"runtime_service_{action}", name)
    return result


@router.put("/selection")
async def update_selection(
    body: RuntimeSelectionRequest,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    result = _control("/v1/selection", method="PUT", body={"selected_services": body.selected_services})
    await write_audit(db, actor.id, "update_runtime_selection", ",".join(result.get("selected_services", [])))
    return result
