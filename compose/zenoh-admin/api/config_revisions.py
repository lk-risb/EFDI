"""Config activation ledger. Stores hashes and outcomes, never config bodies."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import require_role
from .models import ConfigRevision


router = APIRouter(prefix="/api/config-revisions", tags=["config-revisions"])


def revision_out(revision: ConfigRevision) -> dict:
    return {
        "id": revision.id,
        "target_namespace": revision.target_namespace,
        "version": revision.version,
        "source": revision.source,
        "state": revision.state,
        "config_sha256": revision.config_sha256,
        "detail": revision.detail,
        "created_by": revision.created_by,
        "created_at": revision.created_at.isoformat(),
        "completed_at": revision.completed_at.isoformat() if revision.completed_at else None,
    }


async def create_revision(
    db: AsyncSession,
    *,
    target_namespace: str,
    version: int,
    source: str,
    state: str,
    config_sha256: str,
    created_by: str | None,
    detail: str | None = None,
) -> ConfigRevision:
    revision = ConfigRevision(
        target_namespace=target_namespace,
        version=version,
        source=source,
        state=state,
        config_sha256=config_sha256,
        created_by=created_by,
        detail=detail[:1000] if detail else None,
    )
    db.add(revision)
    await db.commit()
    await db.refresh(revision)
    return revision


async def set_revision_state(
    db: AsyncSession,
    revision: ConfigRevision,
    state: str,
    detail: str | None = None,
) -> None:
    revision.state = state
    revision.detail = detail[:1000] if detail else None
    if state not in {"pending", "validating"}:
        revision.completed_at = datetime.now(timezone.utc)
    await db.commit()


@router.get("")
async def list_revisions(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    result = await db.execute(
        select(ConfigRevision).order_by(ConfigRevision.created_at.desc()).limit(limit)
    )
    return [revision_out(item) for item in result.scalars().all()]
