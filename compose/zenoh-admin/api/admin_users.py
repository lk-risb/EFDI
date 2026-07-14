import re
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update

from .db import get_db
from .models import AdminUser, RefreshToken
from .deps import require_role, write_audit, pwd_ctx, get_current_user, create_access_token
from .schemas import TokenResponse

router = APIRouter(prefix="/api/admin-users", tags=["admin-users"])
_superadmin = require_role("superadmin")

VALID_ROLES = {"superadmin", "admin", "readonly"}
MIN_PASSWORD_LEN = 12
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str


class PatchUserRequest(BaseModel):
    role: str | None = None
    password: str | None = None
    is_active: bool | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangeUsernameRequest(BaseModel):
    current_password: str
    new_username: str


def _check_password(password: str):
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")


def _check_username(username: str):
    if not (1 <= len(username) <= 64) or not _USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=400, detail="Username must be 1-64 characters, letters/digits/./_/- only")


@router.get("")
async def list_users(db: AsyncSession = Depends(get_db), actor=Depends(_superadmin)):
    result = await db.execute(select(AdminUser))
    users = result.scalars().all()
    return {"users": [
        {"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active, "created_at": u.created_at}
        for u in users
    ]}


@router.post("", status_code=201)
async def create_user(body: CreateUserRequest, db: AsyncSession = Depends(get_db), actor=Depends(_superadmin)):
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")
    _check_username(body.username)
    existing = await db.execute(select(AdminUser).where(AdminUser.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")
    _check_password(body.password)
    user = AdminUser(
        username=body.username,
        password_hash=pwd_ctx.hash(body.password),
        role=body.role,
        created_by=actor.username,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    await write_audit(db, actor.id, "create_admin_user", body.username)
    return {"id": user.id, "username": user.username, "role": user.role}


@router.patch("/{user_id}")
async def patch_user(user_id: str, body: PatchUserRequest, db: AsyncSession = Depends(get_db), actor=Depends(_superadmin)):
    result = await db.execute(select(AdminUser).where(AdminUser.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.role and body.role != user.role and user.id == actor.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    if body.role:
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"role must be one of {VALID_ROLES}")
        user.role = body.role
    if body.password:
        _check_password(body.password)
        user.password_hash = pwd_ctx.hash(body.password)
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id)
            .values(revoked=True)
        )
    if body.is_active is not None:
        user.is_active = body.is_active
    await db.commit()
    await write_audit(db, actor.id, "patch_admin_user", user_id)
    return {"id": user.id, "username": user.username, "role": user.role}


@router.delete("/{user_id}")
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db), actor=Depends(_superadmin)):
    result = await db.execute(select(AdminUser).where(AdminUser.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
    await db.delete(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="User owns federated-child records; remove or reassign them before deleting the user",
        ) from exc
    await write_audit(db, actor.id, "delete_admin_user", user_id)
    return {"status": "deleted"}


@router.post("/me/change-password")
async def change_own_password(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    actor: AdminUser = Depends(get_current_user),
):
    if not pwd_ctx.verify(body.current_password, actor.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    _check_password(body.new_password)
    actor.password_hash = pwd_ctx.hash(body.new_password)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == actor.id)
        .values(revoked=True)
    )
    await db.commit()
    await write_audit(db, actor.id, "change_own_password", actor.username)
    return {"status": "ok"}


@router.post("/me/change-username", response_model=TokenResponse)
async def change_own_username(
    body: ChangeUsernameRequest,
    db: AsyncSession = Depends(get_db),
    actor: AdminUser = Depends(get_current_user),
):
    if not pwd_ctx.verify(body.current_password, actor.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    _check_username(body.new_username)

    old_username = actor.username
    actor.username = body.new_username
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Username already taken") from e

    await write_audit(db, actor.id, "change_own_username", f"{old_username} -> {actor.username}")
    access_token = create_access_token({"sub": actor.id, "role": actor.role, "username": actor.username})
    return TokenResponse(access_token=access_token)
