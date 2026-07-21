"""Stable managed-router namespace and certificate identity derivation."""

import hashlib
import os
import re


TRUST_DOMAIN = os.environ.get("EFDI_TRUST_DOMAIN", "efdi.global").lower()
_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")


def normalize_path(value: str) -> str:
    value = value.strip("/")
    if not _PATH_RE.fullmatch(value) or "//" in value:
        raise ValueError("managed router path contains unsupported characters")
    return value


def router_identity(namespace_path: str) -> str:
    path = normalize_path(namespace_path)
    return f"spiffe://{TRUST_DOMAIN}/router/{path}"


def child_namespace(parent_path: str, requested: str) -> str:
    parent = normalize_path(parent_path)
    candidate = normalize_path(requested)
    if candidate.startswith(parent + "/"):
        return candidate
    if "/" in candidate:
        raise ValueError("child namespace must be a leaf or an explicit strict descendant")
    return f"{parent}/{candidate}"


def bounded_common_name(profile: str, identity_uri: str) -> str:
    if profile not in {"router", "policy", "ca"}:
        raise ValueError("unknown identity profile")
    digest = hashlib.sha256(identity_uri.encode()).hexdigest()[:32]
    return f"efdi-{profile}-{digest}"
