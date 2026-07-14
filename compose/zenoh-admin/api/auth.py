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

# Fixed bcrypt hash with no matching password, verified against on an unknown
# username so pwd_ctx.verify() always runs one bcrypt round regardless of
# whether the username exists — otherwise a known-user login incurs ~100ms of
# bcrypt work while an unknown-user login returns immediately, letting an
# attacker enumerate valid usernames by timing /auth/login responses.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEeO7oCe1cJXTh8g3wJHKfB8YkKuNAZbEUC"


def create_refresh_token(user_id: str) -> tuple[RefreshToken, str]:
    """Create a stored refresh-token row and return it with its raw cookie value."""
    raw_refresh = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires), raw_refresh


def set_refresh_cookie(response: Response, raw_refresh: str) -> None:
    response.set_cookie(
        "refresh_token",
        raw_refresh,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
        max_age=60 * 60 * 24 * REFRESH_TOKEN_EXPIRE_DAYS,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AdminUser).where(AdminUser.username == body.username, AdminUser.is_active.is_(True)))
    user = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if user and user.locked_until and user.locked_until.replace(tzinfo=timezone.utc) > now:
        raise HTTPException(status_code=429, detail="Account temporarily locked")

    password_ok = pwd_ctx.verify(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not password_ok:
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

    refresh_row, raw_refresh = create_refresh_token(user.id)
    db.add(refresh_row)
    await db.commit()

    set_refresh_cookie(response, raw_refresh)
    await write_audit(db, user.id, "login")
    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(response: Response, refresh_token: str = Cookie(None), db: AsyncSession = Depends(get_db)):
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
    )
    token = result.scalar_one_or_none()
    if not token or token.revoked or token.expires_at <= now:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_result = await db.execute(select(AdminUser).where(AdminUser.id == token.user_id, AdminUser.is_active.is_(True)))
    user = user_result.scalar_one_or_none()
    if not user:
        token.revoked = True
        await db.commit()
        raise HTTPException(status_code=401, detail="User inactive")

    # Refresh cookies are single-use. The row lock makes concurrent refreshes
    # deterministic: the first rotates successfully and every reuse is rejected.
    token.revoked = True
    refresh_row, raw_refresh = create_refresh_token(user.id)
    db.add(refresh_row)
    await db.commit()
    set_refresh_cookie(response, raw_refresh)

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
