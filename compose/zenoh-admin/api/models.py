import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from .db import Base


def _uuid():
    return str(uuid.uuid4())


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # superadmin|admin|readonly
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # OIDC SSO linkage — null for local password accounts. auth_provider is
    # "local" or "oidc"; oidc_subject is the IdP's stable 'sub' claim.
    auth_provider: Mapped[str] = mapped_column(String(16), default="local", nullable=False)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)

    tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["AdminUser"] = relationship(back_populates="tokens")


class BrandSettings(Base):
    __tablename__ = "brand_settings"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    org_name: Mapped[str] = mapped_column(String(64), nullable=False)
    accent_fill: Mapped[str] = mapped_column(String(7), nullable=False)
    accent_fill_hover: Mapped[str] = mapped_column(String(7), nullable=False)
    accent_text: Mapped[str] = mapped_column(String(7), nullable=False)
    accent_ring: Mapped[str] = mapped_column(String(7), nullable=False)
    logo_filename: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class FederatedChild(Base):
    __tablename__ = "federated_children"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # Populated by federation_status.py's subscriber (Task 7) as status reports
    # arrive on this child's @config/status/v1 topic — None until the first
    # push+status round-trip completes.
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_status_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
