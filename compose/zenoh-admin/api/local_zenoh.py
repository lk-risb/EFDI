"""Build a client session from the router's current on-disk configuration.

The admin process stays alive while the router is restarted and its config is
edited.  Reading the rendered config for each new session keeps local clients
aligned with the active listener port and TLS verification setting instead of
freezing values from the container environment at import time.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import zenoh
from .zenoh_auth import apply_zenoh_auth

from .config import CONFIG_PATH, _extract_fields


def _fields():
    try:
        return _extract_fields(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _endpoint(fields) -> str:
    if fields is not None:
        return f"tcp/127.0.0.1:{fields.local_tcp_port}"
    return os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")


def config_fingerprint() -> str:
    """Return a cheap change token for the rendered config and prefix file."""
    parts: list[str] = []
    for path in (CONFIG_PATH, os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")):
        try:
            stat = os.stat(path)
            parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
            if path.endswith("namespace-prefix"):
                parts.append(Path(path).read_text(encoding="utf-8").strip())
        except OSError:
            parts.append(f"{path}:missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def local_connection_details() -> tuple[str, bool]:
    fields = _fields()
    return _endpoint(fields), bool(fields.verify_name_on_connect) if fields is not None else True


def open_local_session() -> "zenoh.Session":
    fields = _fields()
    endpoint = _endpoint(fields)
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([endpoint]))
    apply_zenoh_auth(conf)

    # The local listener always uses the EFDI pod identity.  The selected
    # federation profile is the router's outbound identity and must not be
    # substituted for a local mTLS client certificate.
    if endpoint.startswith("tls"):
        cert_dir = os.environ.get("EFDI_CERT_DIR", "")
        namespace = os.environ.get("PARTNER_NAMESPACE", "")
        verify = bool(fields.verify_name_on_connect) if fields is not None else True
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(cert_dir, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(cert_dir, namespace + "-cert.pem"),
            "connect_private_key": os.path.join(cert_dir, namespace + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": verify,
        }))
    return zenoh.open(conf)
