import asyncio
import json
import os

import zenoh
from sqlalchemy import select

from .db import SessionLocal
from .deps import write_audit
from .models import FederatedChild

_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
_STATUS_KEY_PREFIX = "LTU/CISB/"
_STATUS_KEY_SUFFIX = "/@config/status/v1"
_STATUS_WILDCARD = f"{_STATUS_KEY_PREFIX}**{_STATUS_KEY_SUFFIX}"


def _namespace_from_key(key_expr: str) -> str | None:
    """Extract the child namespace from a concrete status key_expr. Uses
    prefix/suffix slicing (not a `*`-segment split) because namespaces can
    themselves contain '/' (e.g. 'release/vilnius', see federation.tsx's own
    placeholder) — a single-segment wildcard would misparse those."""
    if not (key_expr.startswith(_STATUS_KEY_PREFIX) and key_expr.endswith(_STATUS_KEY_SUFFIX)):
        return None
    namespace = key_expr[len(_STATUS_KEY_PREFIX):-len(_STATUS_KEY_SUFFIX)]
    return namespace or None


async def _record_status(namespace: str, version: int, health: str, error: str | None):
    async with SessionLocal() as db:
        result = await db.execute(select(FederatedChild).where(FederatedChild.namespace == namespace))
        child = result.scalar_one_or_none()
        if child is None:
            # Status from a namespace we don't have a FederatedChild row for.
            # Deliberately no audit entry here (unlike a matched status) —
            # the wildcard subscription (LTU/CISB/**/@config/status/v1)
            # spans the whole mesh, so an unmatched namespace is the
            # expected common case, not an anomaly worth an audit-log
            # entry per occurrence. A matched status (below) is audited.
            return
        from datetime import datetime, timezone
        child.last_status = health
        child.last_status_version = version
        child.last_status_at = datetime.now(timezone.utc)
        child.last_status_error = error
        await db.commit()
        await write_audit(
            db, None, "federation_status_received",
            f"child={namespace}, version={version}, health={health}" + (f", error={error}" if error else ""),
        )


def _handle_status_sample(loop: asyncio.AbstractEventLoop, sample):
    """Runs on zenoh's own callback thread — schedules the DB write onto the
    FastAPI event loop, same pattern as federation_apply.py's _handle_config_push.

    Note on trust: unlike @config/v1 pushes (federation_apply.py, verified
    against a trusted parent's signature via verify_envelope), status
    samples here are NOT signature-verified — unauthenticated because the
    worst case of a spoofed status is a misleading badge/audit entry (an
    operator sees an inaccurate "ok"/"rejected" label), not an unauthorized
    config change: applying a config still requires passing
    federation_apply.py's verify_envelope on the RECEIVING pod, which this
    module has no ability to influence. If that risk calculus ever changes
    (e.g. status becomes a trigger for further automated action), this
    needs the same envelope signing config-push already has."""
    namespace = _namespace_from_key(str(sample.key_expr))
    if namespace is None:
        return
    try:
        body = json.loads(bytes(sample.payload).decode())
    except (ValueError, UnicodeDecodeError):
        return
    # json.loads() on attacker-reachable bytes can legally produce a
    # non-dict top-level value (None, a list, a string, a number) that
    # still parses successfully — the same crash class already found and
    # fixed once in federation_apply.py's envelope handling. Guard it here
    # too before calling .get() on it.
    if not isinstance(body, dict):
        return
    version = body.get("version", -1)
    health = body.get("health", "unknown")
    error = body.get("error")
    asyncio.run_coroutine_threadsafe(_record_status(namespace, version, health, error), loop)


def start_federation_status_subscriber(loop: asyncio.AbstractEventLoop) -> "zenoh.Session | None":
    if not _OWN_NAMESPACE:
        print("[federation-status] PARTNER_NAMESPACE unset — status subscriber not started", flush=True)
        return None

    endpoint = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
    cert_dir = os.environ.get("EFDI_CERT_DIR", "")
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([endpoint]))
    if endpoint.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(cert_dir, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(cert_dir, _OWN_NAMESPACE + "-cert.pem"),
            "connect_private_key": os.path.join(cert_dir, _OWN_NAMESPACE + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))

    session = zenoh.open(conf)
    session.declare_subscriber(_STATUS_WILDCARD, lambda sample: _handle_status_sample(loop, sample))
    print(f"[federation-status] subscribed on {_STATUS_WILDCARD}", flush=True)
    return session
