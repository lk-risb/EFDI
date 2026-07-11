import os
import re

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import FileResponse

from .db import get_db
from .deps import require_role, write_audit
from .models import BrandSettings

router = APIRouter(prefix="/api/branding", tags=["branding"])
_superadmin = require_role("superadmin")

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_DEFAULTS = {
    "org_name": "Zenoh Admin",
    "accent_fill": "#d4d4d8",
    "accent_fill_hover": "#e4e4e7",
    "accent_text": "#18181b",
    "accent_ring": "#a1a1aa",
}

LOGO_DIR = "/data/branding"
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB
_ALLOWED_LOGO_EXT = {".png", ".jpg", ".jpeg"}  # no .svg — served without sanitization, would allow same-origin script execution


class BrandingUpdateRequest(BaseModel):
    org_name: str | None = None
    accent_fill: str | None = None
    accent_fill_hover: str | None = None
    accent_text: str | None = None
    accent_ring: str | None = None


async def _get_or_create(db: AsyncSession) -> BrandSettings:
    result = await db.execute(select(BrandSettings).where(BrandSettings.id == "singleton"))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = BrandSettings(id="singleton", **_DEFAULTS)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


def _serialize(settings: BrandSettings) -> dict:
    logo_url = None
    if settings.logo_filename and os.path.isfile(os.path.join(LOGO_DIR, settings.logo_filename)):
        logo_url = f"/api/branding/logo/{settings.logo_filename}"
    return {
        "org_name": settings.org_name,
        "accent_fill": settings.accent_fill,
        "accent_fill_hover": settings.accent_fill_hover,
        "accent_text": settings.accent_text,
        "accent_ring": settings.accent_ring,
        "logo_url": logo_url,
    }


@router.get("")
async def get_branding(db: AsyncSession = Depends(get_db)):
    settings = await _get_or_create(db)
    return _serialize(settings)


@router.put("")
async def update_branding(body: BrandingUpdateRequest, db: AsyncSession = Depends(get_db), actor=Depends(_superadmin)):
    settings = await _get_or_create(db)
    if body.org_name is not None:
        if not (1 <= len(body.org_name) <= 64):
            raise HTTPException(status_code=400, detail="org_name must be 1-64 characters")
        settings.org_name = body.org_name
    for field in ("accent_fill", "accent_fill_hover", "accent_text", "accent_ring"):
        value = getattr(body, field)
        if value is not None:
            if not _HEX_COLOR_RE.match(value):
                raise HTTPException(status_code=400, detail=f"{field} must be a hex color like #d4d4d8")
            setattr(settings, field, value)
    await db.commit()
    await write_audit(db, actor.id, "update_branding")
    return _serialize(settings)


@router.post("/logo", status_code=201)
async def upload_logo(file: UploadFile = File(...), db: AsyncSession = Depends(get_db), actor=Depends(_superadmin)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_LOGO_EXT:
        raise HTTPException(status_code=400, detail=f"Logo must be one of {sorted(_ALLOWED_LOGO_EXT)}")
    data = await file.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="Logo file too large (max 2 MB)")

    settings = await _get_or_create(db)
    os.makedirs(LOGO_DIR, exist_ok=True)
    if settings.logo_filename:
        old_path = os.path.join(LOGO_DIR, settings.logo_filename)
        if os.path.isfile(old_path):
            os.remove(old_path)

    new_filename = f"logo{ext}"
    dest = os.path.join(LOGO_DIR, new_filename)
    async with aiofiles.open(dest, "wb") as f:
        await f.write(data)

    settings.logo_filename = new_filename
    await db.commit()
    await write_audit(db, actor.id, "upload_logo", new_filename)
    return _serialize(settings)


@router.delete("/logo")
async def delete_logo(db: AsyncSession = Depends(get_db), actor=Depends(_superadmin)):
    settings = await _get_or_create(db)
    if settings.logo_filename:
        old_path = os.path.join(LOGO_DIR, settings.logo_filename)
        if os.path.isfile(old_path):
            os.remove(old_path)
        settings.logo_filename = None
        await db.commit()
        await write_audit(db, actor.id, "delete_logo")
    return _serialize(settings)


@router.get("/logo/{filename}")
async def serve_logo(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(LOGO_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Logo not found")
    return FileResponse(path)
