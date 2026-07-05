import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, Cookie
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db import get_db, SessionLocal
from .models import AdminUser, RefreshToken
from .schemas import LoginRequest, TokenResponse
from .deps import pwd_ctx, create_access_token, write_audit

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_TOKEN_EXPIRE_DAYS = 7
LOCKOUT_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AdminUser).where(AdminUser.username == body.username, AdminUser.is_active == True))
    user = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if user and user.locked_until and user.locked_until.replace(tzinfo=timezone.utc) > now:
        raise HTTPException(status_code=429, detail="Account temporarily locked")

    if not user or not pwd_ctx.verify(body.password, user.password_hash):
        if user:
            user.failed_logins += 1
            if user.failed_logins >= LOCKOUT_ATTEMPTS:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                user.failed_logins = 0
            await db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user.failed_logins = 0
    user.locked_until = None
    await db.commit()

    access_token = create_access_token({"sub": user.id, "role": user.role, "username": user.username})

    raw_refresh = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(RefreshToken(user_id=user.id, token_hash=token_hash, expires_at=expires))
    await db.commit()

    response.set_cookie("refresh_token", raw_refresh, httponly=True, samesite="lax", max_age=60 * 60 * 24 * REFRESH_TOKEN_EXPIRE_DAYS)
    await write_audit(db, user.id, "login")
    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(refresh_token: str = Cookie(None), db: AsyncSession = Depends(get_db)):
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,
            RefreshToken.expires_at > now,
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_result = await db.execute(select(AdminUser).where(AdminUser.id == token.user_id, AdminUser.is_active == True))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User inactive")

    access_token = create_access_token({"sub": user.id, "role": user.role, "username": user.username})
    return TokenResponse(access_token=access_token)


@router.post("/logout")
async def logout(response: Response, refresh_token: str = Cookie(None), db: AsyncSession = Depends(get_db)):
    if refresh_token:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        token = result.scalar_one_or_none()
        if token:
            token.revoked = True
            await db.commit()
    response.delete_cookie("refresh_token")
    return {"status": "logged out"}


async def _ensure_first_user():
    first_user = os.environ.get("ZENOH_ADMIN_FIRST_USER", "admin")
    first_pass = os.environ.get("ZENOH_ADMIN_FIRST_PASS", "")
    if not first_pass:
        return

    async with SessionLocal() as db:
        result = await db.execute(select(AdminUser).where(AdminUser.username == first_user))
        if result.scalar_one_or_none() is None:
            user = AdminUser(
                username=first_user,
                password_hash=pwd_ctx.hash(first_pass),
                role="superadmin",
                created_by="install",
            )
            db.add(user)
            await db.commit()
            print(f"[zenoh-admin] First superadmin created: {first_user}", flush=True)
