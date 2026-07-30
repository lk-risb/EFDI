import asyncio
import json
import os
import time

import zenoh

from .config import CONFIG_PATH, _extract_fields, apply_rendered_config, atomic_write
from .db import SessionLocal
from .deps import write_audit
from .federation_crypto import sign_payload
from .federation_crypto import verify_envelope, FederationVerifyError
from .local_zenoh import config_fingerprint, open_local_session

_TRUSTED_PARENT_CERT_PATH = os.environ.get(
    "EFDI_TRUSTED_PARENT_POLICY_CERT_PATH", "/certs/efdi/trust/trusted-parent-policy.pem"
)
_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_POLICY_SIGNER_KEY_PATH = os.environ.get(
    "EFDI_POLICY_SIGNER_KEY_PATH", "/certs/policy-signer-key.pem"
)
_TRUST_BOOTSTRAP_PATH = os.environ.get(
    "EFDI_TRUST_BOOTSTRAP_PATH", "/certs/efdi/trust/managed-bootstrap.json"
)


def _prefix() -> str:
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "EFDI")


def _trusted_parent() -> str | None:
    """The only identity allowed to authorize this router: its direct parent."""
    return _TRUSTED_PARENT_CERT_PATH if os.path.isfile(_TRUSTED_PARENT_CERT_PATH) else None


_LAST_SEEN_VERSION_PATH = CONFIG_PATH + ".last-seen-version"


def _read_last_seen_version() -> int:
    """Highest signed `version` this pod has ever accepted as genuinely
    signed by its trusted parent (regardless of whether the config it
    carried was ultimately applied or rolled back) — used to reject a
    replayed envelope. -1 (accept anything) if no version has ever been
    recorded, e.g. this pod's first-ever push."""
    try:
        with open(_LAST_SEEN_VERSION_PATH) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return -1


def _write_last_seen_version(version: int) -> None:
    atomic_write(_LAST_SEEN_VERSION_PATH, str(version))

# A post-restart status ("ok"/"rolled_back") is published right after this pod
# restarts its OWN router. The local router is reachable again within a second
# (a fresh session connects), but its MESH UPLINK to the parent — and the
# parent's subscription route propagating back down — take a few more seconds
# to re-establish (Zenoh's connect retry backs off 1s→4s). A single put in that
# window reaches the local router but has no upward route and is silently
# dropped. Re-put the same status across this window so at least one lands once
# the mesh has re-formed; status puts are idempotent (parent just overwrites
# last_status with the same version+health), so duplicates are harmless.
_STATUS_PUBLISH_ATTEMPTS = 5
_STATUS_PUBLISH_INTERVAL_S = 2


def _status_topic() -> str:
    return f"{_prefix()}/{_OWN_NAMESPACE}/@config/status/v1"


def _config_topic() -> str:
    return f"{_prefix()}/{_OWN_NAMESPACE}/@config/v1"


def _open_local_session() -> "zenoh.Session":
    """Open a client session to this pod's own local router. Same connection
    config as start_federation_subscriber()'s long-lived session — factored
    out so a post-restart status publish can use a FRESH one (see
    _publish_status_fresh)."""
    return open_local_session()


def _federated_candidate_error(fields) -> str | None:
    """Protect identity and the live management seam from remote overwrite."""
    if fields.partner_namespace != _OWN_NAMESPACE:
        return "signed config cannot change this router's partner namespace"
    try:
        with open(CONFIG_PATH) as handle:
            current = _extract_fields(handle.read())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"could not read current protected settings: {exc}"

    protected = (
        ("organization namespace prefix", fields.namespace_prefix, current.namespace_prefix),
        ("local mTLS listener port", fields.mtls_port, current.mtls_port),
        ("local management listener port", fields.local_tcp_port, current.local_tcp_port),
        ("fabric TLS identity profile", fields.fabric_tls_profile, current.fabric_tls_profile),
        ("fabric certificate-name verification policy", fields.verify_name_on_connect, current.verify_name_on_connect),
    )
    for label, candidate, active in protected:
        if candidate != active:
            return f"{label} is local-only and cannot be changed by a parent push"

    active_endpoints = set(current.fabric_endpoints)
    candidate_endpoints = set(fields.fabric_endpoints)
    if active_endpoints and not active_endpoints.intersection(candidate_endpoints):
        return (
            "fabric endpoint replacement must be staged: add the new endpoint while retaining "
            "an active endpoint, verify it, then remove the old endpoint"
        )
    return None


def _status_payload(version: int, health: str, error: str | None) -> bytes:
    body = {"version": version, "applied_at": time.time(), "health": health}
    if error:
        body["error"] = error
    try:
        with open(_TRUST_BOOTSTRAP_PATH, encoding="utf-8") as handle:
            bootstrap = json.load(handle)
        trust_chain = bootstrap.get("trust_chain")
        if isinstance(trust_chain, dict):
            body["trust_chain"] = trust_chain
    except (OSError, ValueError, TypeError):
        pass
    with open(_POLICY_SIGNER_KEY_PATH, "rb") as handle:
        signature = sign_payload(body, handle.read(), purpose="status")
    return json.dumps({"payload": body, "signature": signature}, separators=(",", ":")).encode()


def _publish_status(session: "zenoh.Session", version: int, health: str, error: str | None = None):
    """Publish status on the shared subscriber session — only safe BEFORE a
    router restart (the link is still up). Post-restart, use
    _publish_status_fresh instead."""
    session.put(_status_topic(), _status_payload(version, health, error))


def _publish_status_fresh(version: int, health: str, error: str | None = None):
    """Publish status on a FRESH one-shot session, re-put across a short window.
    Used for every status reported after a router restart (ok / rolled_back /
    post-restart failures): the shared subscriber session's link to the local
    router was just dropped by the restart, AND the router's mesh uplink to the
    parent takes a few seconds more to re-form (see _STATUS_PUBLISH_ATTEMPTS).
    A one-shot session (not the shared one) guarantees a live local link via
    zenoh.open()'s blocking connect; the repeated puts cover the mesh-reform
    window so at least one reaches the parent. Only legitimate applies reach
    here, so the cost is bounded."""
    payload = _status_payload(version, health, error)  # built once: applied_at stays fixed across re-puts
    try:
        s = _open_local_session()
        try:
            for i in range(_STATUS_PUBLISH_ATTEMPTS):
                s.put(_status_topic(), payload)
                if i < _STATUS_PUBLISH_ATTEMPTS - 1:
                    time.sleep(_STATUS_PUBLISH_INTERVAL_S)
        finally:
            s.close()
    except Exception as exc:
        print(f"[federation] failed to publish post-restart {health!r} status: {exc}", flush=True)


async def _record_audit(action: str, detail: str):
    async with SessionLocal() as db:
        await write_audit(db, None, action, detail)


def _payload_version(envelope) -> int:
    """Best-effort version extraction for status/audit reporting on a
    verify-failure path — `envelope` itself and `envelope["payload"]` are
    BOTH untrusted at this point (verify_envelope hasn't run yet, or just
    failed): `json.loads()` on attacker-controlled bytes can legally
    produce `None`, a list, a string, or a number at the top level (e.g. the
    single-byte payload `null`), not just a dict with a wrong-typed
    "payload" key. `.get(k, default)` only substitutes the default when the
    KEY is missing, not when the value has the wrong type, so an unguarded
    `envelope.get("payload", {})` raises AttributeError the moment
    `envelope` itself isn't a dict — reachable pre-verification from a
    single malicious payload. Guard both levels."""
    if not isinstance(envelope, dict):
        return -1
    raw_payload = envelope.get("payload")
    return raw_payload.get("version", -1) if isinstance(raw_payload, dict) else -1


def _handle_config_push(session: "zenoh.Session", loop: asyncio.AbstractEventLoop, sample):
    """Runs on zenoh's own callback thread — schedules the audit-log write
    (async/DB) onto the FastAPI event loop via run_coroutine_threadsafe."""
    try:
        envelope = json.loads(bytes(sample.payload).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"[federation] malformed envelope, ignoring: {exc}", flush=True)
        _publish_status(session, -1, "rejected", f"malformed envelope: {exc}")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", f"malformed envelope: {exc}"), loop,
        )
        return

    parent = _trusted_parent()
    if parent is None:
        print("[federation] received a config push but no trusted parent is configured — rejecting", flush=True)
        return

    try:
        with open(parent, "rb") as f:
            trusted_cert_pem = f.read()
        payload = verify_envelope(envelope, trusted_cert_pem, purpose="config")
    except (FederationVerifyError, OSError) as exc:
        version = _payload_version(envelope)
        detail = f"trusted parent verification failed: {exc}"
        _publish_status(session, version, "rejected", detail)
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", f"version={version}, error={detail}"), loop,
        )
        return

    version = payload.get("version")
    rendered = payload.get("config")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        _publish_status(session, -1, "rejected", "signed payload has invalid version")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", "signed payload has invalid version"), loop,
        )
        return
    if not isinstance(rendered, str):
        _publish_status(session, version, "rejected", "signed payload config is not text")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", f"version={version}, config is not text"), loop,
        )
        return

    last_seen = _read_last_seen_version()
    if version <= last_seen:
        _publish_status(session, version, "rejected", f"replay rejected: version {version} <= last seen {last_seen}")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", f"version={version}, replay rejected (last seen {last_seen})"), loop,
        )
        return
    _write_last_seen_version(version)

    # The pushed config is already-rendered config.json5 text, not raw
    # ConfigFields — validate its shape the same way the local structured
    # editor's PUT route does today (compose/zenoh-admin/api/config.py's
    # put_config, via the same _extract_fields used by its GET route).
    try:
        fields = _extract_fields(rendered)
    except (ValueError, KeyError, TypeError) as exc:
        _publish_status(session, version, "rejected", f"invalid config shape: {exc}")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", f"version={version}, shape error: {exc}"), loop,
        )
        return

    protected_error = _federated_candidate_error(fields)
    if protected_error:
        _publish_status(session, version, "rejected", protected_error)
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", f"version={version}, {protected_error}"), loop,
        )
        return

    result = apply_rendered_config(
        rendered,
        fields,
        restart_native=True,
        preserve_management=True,
    )
    if result["status"] == "applied":
        _publish_status_fresh(version, "ok")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_applied", f"version={version}"), loop,
        )
    else:
        health = "rolled_back" if result["status"] == "rolled_back" else "rejected"
        detail = str(result.get("error") or result["status"])
        _publish_status_fresh(version, health, detail)
        asyncio.run_coroutine_threadsafe(
            _record_audit(
                "federation_config_rollback" if health == "rolled_back" else "federation_config_rejected",
                f"version={version}, {detail}",
            ),
            loop,
        )


_federation_session: "zenoh.Session | None" = None


def _subscribe(loop: asyncio.AbstractEventLoop) -> "zenoh.Session":
    session = _open_local_session()
    topic = _config_topic()
    session.declare_subscriber(topic, lambda sample, current=session: _handle_config_push(current, loop, sample))
    print(f"[federation] subscribed on {topic}", flush=True)
    return session


async def _watch_federation_session(loop: asyncio.AbstractEventLoop):
    global _federation_session
    fingerprint = config_fingerprint()
    try:
        while True:
            await asyncio.sleep(2)
            new_fingerprint = config_fingerprint()
            if new_fingerprint == fingerprint:
                continue
            try:
                replacement = _subscribe(loop)
            except Exception as exc:
                print(f"[federation] session reload failed: {exc}", flush=True)
                continue
            old_session = _federation_session
            _federation_session = replacement
            fingerprint = new_fingerprint
            if old_session is not None:
                old_session.close()
            print("[federation] reloaded local session after config change", flush=True)
    finally:
        if _federation_session is not None:
            _federation_session.close()
            _federation_session = None


def start_federation_subscriber(loop: asyncio.AbstractEventLoop) -> "tuple[zenoh.Session | None, asyncio.Task | None]":
    global _federation_session
    parent = _trusted_parent()
    if parent is None:
        print("[federation] no trusted parent configured — federation subscriber not started", flush=True)
        return None, None
    if not _OWN_NAMESPACE:
        print("[federation] PARTNER_NAMESPACE unset — cannot determine own config topic, "
              "federation subscriber not started", flush=True)
        return None, None

    _federation_session = _subscribe(loop)
    print("[federation] trusted managed-parent policy signer configured", flush=True)
    return _federation_session, loop.create_task(_watch_federation_session(loop))
