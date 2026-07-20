from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import require_role
from .models import AdminUser, AuditLog


router = APIRouter(prefix="/api/audit-log", tags=["audit-log"])


@router.get("")
async def list_audit_log(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("superadmin")),
):
    result = await db.execute(
        select(AuditLog, AdminUser.username)
        .outerjoin(AdminUser, AuditLog.user_id == AdminUser.id)
        .order_by(AuditLog.timestamp.desc())
        .limit(200)
    )
    return {
        "entries": [
            {
                "id": log.id,
                "username": username or "(deleted user)",
                "action": log.action,
                "detail": log.detail,
                "timestamp": log.timestamp,
            }
            for log, username in result.all()
        ]
    }
