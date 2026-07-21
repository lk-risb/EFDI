"""Strict, bounded trust and delegation contracts."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


_IDENTITY_RE = re.compile(r"^spiffe://[a-z0-9.-]{1,253}/router/[A-Za-z0-9._~%/-]{1,512}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RouterRole(StrEnum):
    ROUTER_CA = "router-ca"
    ROUTER = "router"
    WORKLOAD_ISSUER = "workload-issuer"


class ControlAction(StrEnum):
    TOPOLOGY = "topology"
    STATUS = "status"
    CONFIG_FROM_PARENT = "config-from-parent"
    MANAGE_CHILDREN = "manage-children"


def parse_key_scope(expression: str) -> tuple[tuple[str, ...], bool]:
    """Parse the deliberately small ACL subset used by delegation grants."""
    if not isinstance(expression, str) or not 1 <= len(expression) <= 1024:
        raise ValueError("key scope must be a non-empty string of at most 1024 characters")
    if expression.startswith("/") or expression.endswith("/"):
        raise ValueError("key scope must not start or end with '/'")
    parts = expression.split("/")
    wildcard = parts[-1] == "**"
    literal = parts[:-1] if wildcard else parts
    if not literal or any(not _SEGMENT_RE.fullmatch(part) for part in literal):
        raise ValueError("key scope contains an unsupported segment")
    if any(part in {"*", "**"} for part in literal):
        raise ValueError("only one terminal '/**' wildcard is supported")
    return tuple(literal), wildcard


def scope_contains(parent: str, child: str) -> bool:
    parent_parts, parent_wildcard = parse_key_scope(parent)
    child_parts, child_wildcard = parse_key_scope(child)
    if not parent_wildcard:
        return not child_wildcard and child_parts == parent_parts
    return len(child_parts) >= len(parent_parts) and child_parts[:len(parent_parts)] == parent_parts


class DelegationGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["efdi.delegation/v1"] = Field(
        default="efdi.delegation/v1", alias="schema", serialization_alias="schema"
    )
    id: UUID
    issuer_identity: str
    subject_ca_sha256: str
    subject_identity: str
    namespace: str
    roles: list[RouterRole] = Field(min_length=1, max_length=3)
    publish: list[str] = Field(default_factory=list, max_length=128)
    subscribe: list[str] = Field(default_factory=list, max_length=128)
    control: list[ControlAction] = Field(default_factory=list, max_length=4)
    max_delegation_depth: StrictInt = Field(ge=0, le=32)
    sequence: StrictInt = Field(ge=0)
    not_before: datetime
    not_after: datetime

    @field_validator("issuer_identity", "subject_identity")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not _IDENTITY_RE.fullmatch(value):
            raise ValueError("identity must be a bounded SPIFFE router URI")
        return value

    @field_validator("subject_ca_sha256")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        value = value.lower()
        if not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("CA fingerprint must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        _, wildcard = parse_key_scope(value)
        if not wildcard:
            raise ValueError("delegated namespace must end in '/**'")
        return value

    @field_validator("publish", "subscribe")
    @classmethod
    def validate_scopes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("duplicate key scopes are not allowed")
        for value in values:
            parse_key_scope(value)
        return values

    @field_validator("roles", "control")
    @classmethod
    def validate_unique_enums(cls, values: list) -> list:
        if len(values) != len(set(values)):
            raise ValueError("duplicate grant entries are not allowed")
        return values

    @field_validator("not_before", "not_after")
    @classmethod
    def validate_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("grant timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_contract(self):
        if self.not_after <= self.not_before:
            raise ValueError("not_after must be later than not_before")
        if self.issuer_identity == self.subject_identity:
            raise ValueError("a router cannot delegate to itself")
        if not self.subject_identity.startswith(self.issuer_identity.rstrip("/") + "/"):
            raise ValueError("subject identity must be a strict descendant of issuer identity")
        if any(not scope_contains(self.namespace, expression) for expression in self.publish):
            raise ValueError("publish scope escapes the delegated namespace")
        return self


class DelegationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["efdi.delegation-envelope/v1"] = Field(
        default="efdi.delegation-envelope/v1",
        alias="schema",
        serialization_alias="schema",
    )
    payload: dict
    signature: str = Field(min_length=8, max_length=1024)
    signer_certificate: str = Field(min_length=64, max_length=65536)
    signer_sha256: str

    @field_validator("signer_sha256")
    @classmethod
    def validate_signer_fingerprint(cls, value: str) -> str:
        value = value.lower()
        if not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("signer fingerprint must be lowercase SHA-256")
        return value


def grant_is_subset(child: DelegationGrant, parent: DelegationGrant) -> bool:
    """Return true only when every child authority is provably narrower."""
    if child.issuer_identity != parent.subject_identity:
        return False
    if not scope_contains(parent.namespace, child.namespace):
        return False
    if child.namespace == parent.namespace:
        return False
    if not set(child.roles).issubset(parent.roles):
        return False
    if not set(child.control).issubset(parent.control):
        return False
    if child.max_delegation_depth >= parent.max_delegation_depth:
        return False
    if child.not_before < parent.not_before or child.not_after > parent.not_after:
        return False
    for requested, allowed in ((child.publish, parent.publish), (child.subscribe, parent.subscribe)):
        if any(not any(scope_contains(parent_scope, scope) for parent_scope in allowed) for scope in requested):
            return False
    return True
