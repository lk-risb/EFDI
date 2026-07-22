import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, BigInteger, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator
from .db import Base


def _uuid():
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    """Store UTC as DATETIME and restore timezone awareness on every read."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime values must include a timezone")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


UUID_STRING = String(36, collation="ascii_bin")
ASCII_REFERENCE = String(768, collation="ascii_bin")
LONG_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # superadmin|admin|readonly
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # OIDC SSO linkage — null for local password accounts. auth_provider is
    # "local" or "oidc"; oidc_subject is the IdP's stable 'sub' claim.
    auth_provider: Mapped[str] = mapped_column(String(16), default="local", nullable=False)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)

    tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
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

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))


class FederatedChild(Base):
    __tablename__ = "federated_children"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    # Populated by federation_status.py's subscriber (Task 7) as status reports
    # arrive on this child's @config/status/v1 topic — None until the first
    # push+status round-trip completes.
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_status_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_status_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_status_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    transport_cert_pem: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    cert_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    max_delegation_depth: Mapped[int] = mapped_column(default=0, nullable=False)


class ConfigRevision(Base):
    __tablename__ = "config_revisions"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    target_namespace: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class PkiInvitation(Base):
    __tablename__ = "pki_invitations"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    child_name: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    max_delegation_depth: Mapped[int] = mapped_column(nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(ForeignKey("admin_users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    ca_csr_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transport_csr_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_csr_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ca_cert_pem: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    transport_cert_pem: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    policy_cert_pem: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    chain_pem: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    transport_chain_pem: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    issued_serials: Mapped[str | None] = mapped_column(String(256), nullable=True)
    grant_envelope_json: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    link_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    link_password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    authority_id: Mapped[str | None] = mapped_column(ForeignKey("trust_authorities.id"), nullable=True, index=True)


class TrustAuthority(Base):
    __tablename__ = "trust_authorities"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    identity_uri: Mapped[str] = mapped_column(ASCII_REFERENCE, unique=True, nullable=False, index=True)
    namespace_scope: Mapped[str] = mapped_column(String(1024), nullable=False)
    ca_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    ca_cert_pem: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    policy_signer_fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    policy_signer_cert_pem: Mapped[str | None] = mapped_column(LONG_TEXT, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("trust_authorities.id"), nullable=True, index=True)
    max_delegation_depth: Mapped[int] = mapped_column(nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    not_after: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class Delegation(Base):
    __tablename__ = "delegations"
    __table_args__ = (
        UniqueConstraint("issuer_authority_id", "sequence", name="uq_delegation_issuer_sequence"),
    )

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    grant_id: Mapped[str] = mapped_column(UUID_STRING, unique=True, nullable=False, index=True)
    issuer_authority_id: Mapped[str] = mapped_column(ForeignKey("trust_authorities.id"), nullable=False, index=True)
    subject_authority_id: Mapped[str] = mapped_column(ForeignKey("trust_authorities.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    envelope_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    grant_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    not_after: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))


class IssuedIdentity(Base):
    __tablename__ = "issued_identities"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    authority_id: Mapped[str] = mapped_column(ForeignKey("trust_authorities.id"), nullable=False, index=True)
    identity_uri: Mapped[str] = mapped_column(ASCII_REFERENCE, nullable=False, index=True)
    profile: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    serial: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    cert_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    certificate_pem: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    not_after: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    replaced_by_id: Mapped[str | None] = mapped_column(ForeignKey("issued_identities.id"), nullable=True)


class Revocation(Base):
    __tablename__ = "revocations"
    __table_args__ = (
        UniqueConstraint("target_type", "target_reference", name="uq_revocation_target"),
    )

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_reference: Mapped[str] = mapped_column(ASCII_REFERENCE, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))


class LinkCredential(Base):
    __tablename__ = "link_credentials"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    authority_id: Mapped[str] = mapped_column(ForeignKey("trust_authorities.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class AclRevision(Base):
    __tablename__ = "acl_revisions"

    id: Mapped[str] = mapped_column(UUID_STRING, primary_key=True, default=_uuid)
    sequence: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    policy_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    policy_json: Mapped[str] = mapped_column(LONG_TEXT, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=lambda: datetime.now(timezone.utc))
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
