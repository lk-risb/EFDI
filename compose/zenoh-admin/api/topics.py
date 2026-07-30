import asyncio
import json
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import require_role, write_audit
from .local_zenoh import open_local_session
from .models import TopicRegistration

router = APIRouter(prefix="/api/topics", tags=["topics"])

_KEY_EXPR_RE = re.compile(r"^[A-Za-z0-9._~*/$@?=+-]+$")
_ENCODING_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_DIRECTIONS = {"publish", "subscribe", "bidirectional"}
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_DATA_PREFIX_FILE = os.environ.get(
    "DATA_NAMESPACE_PREFIX_FILE", "/data-topic-prefix"
)
_OBSERVED_LIMIT = 2000
_OBSERVED_LOCK = threading.Lock()
_OBSERVED: OrderedDict[str, dict] = OrderedDict()


def _prefix() -> str:
    try:
        with open(_PREFIX_FILE, encoding="utf-8") as handle:
            value = handle.read().strip().strip("/")
        if value:
            return value
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "EFDI").strip("/")


def _data_prefix() -> str:
    try:
        with open(_DATA_PREFIX_FILE, encoding="utf-8") as handle:
            return handle.read().strip().strip("/")
    except OSError:
        return _prefix()


def _catalog_topic() -> str:
    namespace = os.environ.get("PARTNER_NAMESPACE", "").strip("/")
    root = "/".join(part for part in (_data_prefix(), namespace) if part)
    return f"{root}/@catalog/topics/v1" if root else "@catalog/topics/v1"


def _observe(sample) -> None:
    key_expr = str(sample.key_expr)
    now = datetime.now(timezone.utc).isoformat()
    encoding = str(getattr(sample, "encoding", "application/octet-stream"))
    with _OBSERVED_LOCK:
        current = _OBSERVED.pop(key_expr, None)
        if current is None:
            current = {
                "key_expr": key_expr,
                "encoding": encoding,
                "first_seen": now,
                "last_seen": now,
                "sample_count": 0,
            }
        current["encoding"] = encoding
        current["last_seen"] = now
        current["sample_count"] += 1
        _OBSERVED[key_expr] = current
        while len(_OBSERVED) > _OBSERVED_LIMIT:
            _OBSERVED.popitem(last=False)


def start_topic_observer():
    """Observe topic metadata without retaining or exposing message payloads."""
    try:
        session = open_local_session()
        session.declare_subscriber("**", _observe)
        return session
    except Exception as exc:
        print(f"[topics] observer not started: {exc}", flush=True)
        return None


def _observed_topics() -> list[dict]:
    with _OBSERVED_LOCK:
        return [dict(item) for item in reversed(_OBSERVED.values())]


class TopicIn(BaseModel):
    key_expr: str = Field(min_length=1, max_length=512)
    encoding: str = Field(default="application/json", min_length=3, max_length=128)
    direction: str = "publish"
    description: str = Field(default="", max_length=512)

    @field_validator("key_expr")
    @classmethod
    def validate_key_expr(cls, value: str) -> str:
        value = value.strip().strip("/")
        segments = value.split("/")
        if (
            not value
            or "//" in value
            or any(segment in {".", ".."} for segment in segments)
            or not _KEY_EXPR_RE.fullmatch(value)
        ):
            raise ValueError("invalid Zenoh key expression")
        return value

    @field_validator("encoding")
    @classmethod
    def validate_encoding(cls, value: str) -> str:
        value = value.strip().lower()
        if not _ENCODING_RE.fullmatch(value):
            raise ValueError("encoding must be a MIME media type")
        return value

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in _DIRECTIONS:
            raise ValueError("direction must be publish, subscribe, or bidirectional")
        return value

    @model_validator(mode="after")
    def validate_publish_key_is_concrete(self):
        if self.direction in {"publish", "bidirectional"} and "*" in self.key_expr:
            raise ValueError(
                "publish and bidirectional registrations require a concrete key; "
                "wildcards are subscription-only"
            )
        return self


class TopicOut(TopicIn):
    model_config = ConfigDict(from_attributes=True)

    id: str
    registered_by: str
    created_at: datetime


def _out(registration: TopicRegistration) -> TopicOut:
    return TopicOut.model_validate(registration)


def _put_catalog(payload: bytes) -> None:
    session = open_local_session()
    try:
        session.put(
            _catalog_topic(),
            payload,
            encoding="application/json",
        )
    finally:
        session.close()


async def _publish_catalog(db: AsyncSession) -> None:
    result = await db.execute(
        select(TopicRegistration).order_by(TopicRegistration.key_expr)
    )
    registrations = [_out(item).model_dump(mode="json") for item in result.scalars()]
    payload = json.dumps(
        {
            "version": 1,
            "router_namespace": os.environ.get("PARTNER_NAMESPACE", ""),
            "topics": registrations,
        },
        separators=(",", ":"),
    ).encode()
    await asyncio.to_thread(_put_catalog, payload)


@router.get("")
async def list_topics(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("readonly", "admin", "superadmin")),
):
    result = await db.execute(
        select(TopicRegistration).order_by(TopicRegistration.key_expr)
    )
    return {
        "catalog_topic": _catalog_topic(),
        "topics": [_out(item) for item in result.scalars()],
        "observed": _observed_topics(),
    }


@router.post("", response_model=TopicOut, status_code=201)
async def register_topic(
    request: TopicIn,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    registration = TopicRegistration(
        key_expr=request.key_expr,
        encoding=request.encoding,
        direction=request.direction,
        description=request.description.strip(),
        registered_by=actor.id,
    )
    db.add(registration)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="topic is already registered") from exc
    await db.refresh(registration)
    await write_audit(db, actor.id, "register_topic", request.key_expr)
    try:
        await _publish_catalog(db)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"topic saved, but catalog publication failed: {exc}",
        ) from exc
    return _out(registration)


@router.delete("/{registration_id}", status_code=204)
async def unregister_topic(
    registration_id: str,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    registration = await db.get(TopicRegistration, registration_id)
    if registration is None:
        raise HTTPException(status_code=404, detail="topic registration not found")
    key_expr = registration.key_expr
    await db.delete(registration)
    await db.commit()
    await write_audit(db, actor.id, "unregister_topic", key_expr)
    try:
        await _publish_catalog(db)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"topic removed, but catalog publication failed: {exc}",
        ) from exc
    return Response(status_code=204)
