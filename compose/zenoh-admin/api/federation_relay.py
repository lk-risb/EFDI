"""Immediate-parent verified, hop-by-hop re-signed config relay."""

import asyncio
import json
import os
import time

import zenoh
from sqlalchemy import select

from .db import SessionLocal
from .federation_apply import _record_audit
from .federation_crypto import FederationVerifyError, sign_payload, verify_envelope
from .local_zenoh import config_fingerprint, open_local_session
from .models import FederatedChild


_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
_TRUSTED_PARENT_CERT_PATH = os.environ.get(
    "EFDI_TRUSTED_PARENT_POLICY_CERT_PATH", "/certs/efdi/trust/trusted-parent-policy.pem"
)
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_MAX_ENVELOPE_BYTES = 512 * 1024
_MAX_RELAY_HOPS = 64


def _prefix() -> str:
    try:
        with open(_PREFIX_FILE) as handle:
            value = handle.read().strip()
        if value:
            return value
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "LTU/CISB")


def relay_topic(namespace: str) -> str:
    return f"{_prefix()}/{namespace}/@config/relay/v1"


def config_topic(namespace: str) -> str:
    return f"{_prefix()}/{namespace}/@config/v1"


async def _direct_children() -> set[str]:
    async with SessionLocal() as db:
        result = await db.execute(select(FederatedChild.namespace))
        return set(result.scalars().all())


def _own_signing_key() -> bytes:
    path = os.environ.get("EFDI_POLICY_SIGNER_KEY_PATH", "/certs/policy-signer-key.pem")
    with open(path, "rb") as handle:
        return handle.read()


def _audit(loop: asyncio.AbstractEventLoop, action: str, detail: str) -> None:
    asyncio.run_coroutine_threadsafe(_record_audit(action, detail), loop)


def _handle_relay(session: "zenoh.Session", loop: asyncio.AbstractEventLoop, sample) -> None:
    raw = bytes(sample.payload)
    if len(raw) > _MAX_ENVELOPE_BYTES:
        _audit(loop, "federation_relay_rejected", "relay envelope exceeded size limit")
        return
    try:
        envelope = json.loads(raw.decode("utf-8"))
        with open(_TRUSTED_PARENT_CERT_PATH, "rb") as handle:
            payload = verify_envelope(envelope, handle.read(), purpose="relay")
    except (ValueError, UnicodeDecodeError, OSError, FederationVerifyError) as exc:
        _audit(loop, "federation_relay_rejected", f"parent verification failed: {exc}")
        return

    path = payload.get("path")
    rendered = payload.get("config")
    version = payload.get("version")
    if (
        not isinstance(path, list)
        or not 2 <= len(path) <= _MAX_RELAY_HOPS
        or any(not isinstance(item, str) or not item for item in path)
        or path[0] != _OWN_NAMESPACE
        or not isinstance(rendered, str)
        or len(rendered.encode("utf-8")) > _MAX_ENVELOPE_BYTES
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
    ):
        _audit(loop, "federation_relay_rejected", f"version={version!r}, invalid relay payload")
        return

    try:
        children = asyncio.run_coroutine_threadsafe(_direct_children(), loop).result(timeout=5)
    except Exception as exc:
        _audit(loop, "federation_relay_rejected", f"version={version}, child lookup failed: {exc}")
        return
    next_hop = path[1]
    if next_hop not in children:
        _audit(
            loop,
            "federation_relay_rejected",
            f"version={version}, next hop {next_hop!r} is not a registered direct child",
        )
        return

    try:
        key_pem = _own_signing_key()
        if len(path) == 2:
            forwarded = {"config": rendered, "version": version, "signed_at": time.time()}
            topic = config_topic(next_hop)
            kind = "apply"
        else:
            forwarded = {
                "path": path[1:],
                "config": rendered,
                "version": version,
                "signed_at": time.time(),
            }
            topic = relay_topic(next_hop)
            kind = "relay"
        outgoing = {"payload": forwarded, "signature": sign_payload(
            forwarded, key_pem, purpose="config" if kind == "apply" else "relay"
        )}
        session.put(topic, json.dumps(outgoing, separators=(",", ":")).encode())
    except Exception as exc:
        _audit(loop, "federation_relay_failed", f"version={version}, next={next_hop}, error={exc}")
        return
    _audit(
        loop,
        "federation_relay_forwarded",
        f"version={version}, next={next_hop}, kind={kind}, remaining_hops={len(path) - 1}",
    )


_relay_session: "zenoh.Session | None" = None


def _subscribe(loop: asyncio.AbstractEventLoop) -> "zenoh.Session":
    session = open_local_session()
    topic = relay_topic(_OWN_NAMESPACE)
    session.declare_subscriber(topic, lambda sample, current=session: _handle_relay(current, loop, sample))
    print(f"[federation-relay] subscribed on {topic} with managed parent policy trust", flush=True)
    return session


async def _watch_relay_session(loop: asyncio.AbstractEventLoop) -> None:
    global _relay_session
    fingerprint = config_fingerprint()
    try:
        while True:
            await asyncio.sleep(2)
            updated = config_fingerprint()
            if updated == fingerprint:
                continue
            try:
                replacement = _subscribe(loop)
            except Exception as exc:
                print(f"[federation-relay] session reload failed: {exc}", flush=True)
                continue
            old = _relay_session
            _relay_session = replacement
            fingerprint = updated
            if old is not None:
                old.close()
    finally:
        if _relay_session is not None:
            _relay_session.close()
            _relay_session = None


def start_relay_subscriber(
    loop: asyncio.AbstractEventLoop,
) -> "tuple[zenoh.Session | None, asyncio.Task | None]":
    global _relay_session
    if not _OWN_NAMESPACE or not os.path.isfile(_TRUSTED_PARENT_CERT_PATH):
        print("[federation-relay] no managed parent configured — relay receiver disabled", flush=True)
        return None, None
    _relay_session = _subscribe(loop)
    return _relay_session, loop.create_task(_watch_relay_session(loop))
