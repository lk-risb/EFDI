import asyncio
import json
import os
import shutil
import time

import zenoh

from .config import CONFIG_PATH, _extract_fields, atomic_write, write_config_to_disk, restart_router_container
from .db import SessionLocal
from .deps import write_audit
from .federation_crypto import verify_envelope, FederationVerifyError

_TRUSTED_PARENT_CERT_PATH = os.environ.get("ZENOH_ADMIN_TRUSTED_PARENT_CERT_PATH", "")
_TRUSTED_PARENT_CN = os.environ.get("ZENOH_ADMIN_TRUSTED_PARENT_CN", "")
# Root/HQ signers: PLURAL by design — a deep tree (HQ -> child -> grandchild
# -> ...) needs pushes from HQ to reach any descendant directly, and "HQ" may
# itself be multiple active HA replicas (e.g. behind a cloud LB), not one
# pinned box/cert. Comma-separated, parallel order (paths[i] pairs with cns[i]).
_TRUSTED_ROOT_CERT_PATHS = [p.strip() for p in os.environ.get("ZENOH_ADMIN_TRUSTED_ROOT_CERT_PATHS", "").split(",") if p.strip()]
_TRUSTED_ROOT_CNS = [c.strip() for c in os.environ.get("ZENOH_ADMIN_TRUSTED_ROOT_CNS", "").split(",") if c.strip()]
if len(_TRUSTED_ROOT_CERT_PATHS) != len(_TRUSTED_ROOT_CNS):
    print(f"[federation] ZENOH_ADMIN_TRUSTED_ROOT_CERT_PATHS ({len(_TRUSTED_ROOT_CERT_PATHS)} entries) and "
          f"ZENOH_ADMIN_TRUSTED_ROOT_CNS ({len(_TRUSTED_ROOT_CNS)} entries) have mismatched lengths — "
          "ignoring root trust entirely (immediate-parent trust, if configured, is unaffected)", flush=True)
    _TRUSTED_ROOT_CERT_PATHS, _TRUSTED_ROOT_CNS = [], []
_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")


def _prefix() -> str:
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "LTU/CISB")


def _trusted_signers() -> list[tuple[str, str]]:
    """(cert_path, expected_cn) pairs this pod accepts a config push signature
    from — its immediate parent plus every currently-active root/HQ signer."""
    signers = []
    if _TRUSTED_PARENT_CERT_PATH and _TRUSTED_PARENT_CN:
        signers.append((_TRUSTED_PARENT_CERT_PATH, _TRUSTED_PARENT_CN))
    signers.extend(zip(_TRUSTED_ROOT_CERT_PATHS, _TRUSTED_ROOT_CNS))
    return signers


_LAST_KNOWN_GOOD_PATH = CONFIG_PATH + ".last-known-good"
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

# How long to wait for the router to come back healthy after a restart before
# rolling back. Matches the dashboard's own existing health-check cadence
# (compose/zenoh-admin/ui/src/routes/index.tsx polls /api/health every 5s) —
# 30s gives 6 poll-equivalent chances, generous for a container restart.
_HEALTH_CHECK_TIMEOUT_S = 30
_HEALTH_CHECK_INTERVAL_S = 2

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
    return zenoh.open(conf)


def _status_payload(version: int, health: str, error: str | None) -> bytes:
    body = {"version": version, "applied_at": time.time(), "health": health}
    if error:
        body["error"] = error
    return json.dumps(body).encode()


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


def _router_is_healthy() -> bool:
    """Best-effort local check: is the zenoh-router container up right now?
    Uses the same Docker client pattern as restart_router_container()."""
    import docker
    from docker.errors import DockerException, NotFound
    from .config import ZENOH_ROUTER_SERVICE_LABEL
    try:
        client = docker.from_env()
        container = client.containers.get(ZENOH_ROUTER_SERVICE_LABEL)
        container.reload()
        state = container.attrs.get("State", {})
        if state.get("Status") != "running":
            return False
        health = state.get("Health", {}).get("Status")
        return health == "healthy" if health is not None else True
    except (NotFound, DockerException):
        return False


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


def _restore_last_known_good() -> tuple[bool, str | None]:
    """Attempt to restore CONFIG_PATH from the last-known-good backup and
    restart. Returns (restored, error):
    - (False, None) — there's no backup to restore (e.g. this was the pod's
      first-ever push) — the bad config is left in place, nothing to do.
    - (False, "restored backup but restart failed: ...") — backup was
      restored to disk but the router restart on the restored config itself
      failed — the router may still be down even after this call.
    - (True, None) — backup restored and the router restarted successfully.
    Callers must use `restored` (not merely "we attempted a rollback") to
    decide whether to report health="rolled_back" — reporting a rollback
    that didn't actually happen tells operators recovery occurred when the
    bad config is still live."""
    if not os.path.isfile(_LAST_KNOWN_GOOD_PATH):
        return False, None
    shutil.copyfile(_LAST_KNOWN_GOOD_PATH, CONFIG_PATH)
    restarted, restart_error = restart_router_container()
    if not restarted:
        return False, f"restored backup but restart failed: {restart_error}"
    return True, None


def _fail_and_maybe_rollback(loop: asyncio.AbstractEventLoop, version: int, reason: str):
    """Something went wrong after the new config was already written to
    disk (the write itself failed, the restart on the new config failed, or
    the post-restart health check timed out). Attempt to restore the last-
    known-good config; report health="rolled_back" ONLY if a restore
    actually happened. Otherwise report "rejected" — a bad config with no
    backup available is left in place on disk, and that must never be
    reported as a successful rollback.

    Status is published on a fresh session (not the shared subscriber one):
    this path always runs after at least one restart_router_container() call
    — either the caller's or _restore_last_known_good()'s own — so the shared
    session's link is unreliable here."""
    restored, restore_error = _restore_last_known_good()
    if restored:
        health, detail = "rolled_back", reason
    else:
        health = "rejected"
        detail = reason + (f"; {restore_error}" if restore_error else "; no backup to restore, bad config left in place")
    _publish_status_fresh(version, health, detail)
    asyncio.run_coroutine_threadsafe(
        _record_audit(
            "federation_config_rollback" if restored else "federation_config_rejected",
            f"version={version}, {detail}",
        ),
        loop,
    )


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

    signers = _trusted_signers()
    if not signers:
        print("[federation] received a config push but no trusted signers are configured "
              "(ZENOH_ADMIN_TRUSTED_PARENT_CERT_PATH/_CN, ZENOH_ADMIN_TRUSTED_ROOT_CERT_PATHS/_CNS) "
              "— rejecting (this instance accepts no federated pushes)", flush=True)
        return

    payload = None
    errors = []
    for cert_path, expected_cn in signers:
        try:
            with open(cert_path, "rb") as f:
                trusted_cert_pem = f.read()
            payload = verify_envelope(envelope, trusted_cert_pem, expected_cn)
            break
        except (FederationVerifyError, OSError) as exc:
            errors.append(f"{expected_cn} ({cert_path}): {exc}")

    if payload is None:
        version = _payload_version(envelope)
        detail = f"no trusted signer matched ({len(signers)} checked): " + "; ".join(errors)
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
        _extract_fields(rendered)
    except (ValueError, KeyError, TypeError) as exc:
        _publish_status(session, version, "rejected", f"invalid config shape: {exc}")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_rejected", f"version={version}, shape error: {exc}"), loop,
        )
        return

    # Back up current config before writing the new one — must happen before
    # write_config_to_disk() so the backup can never itself be the new
    # (possibly bad) config.
    if os.path.isfile(CONFIG_PATH):
        shutil.copyfile(CONFIG_PATH, _LAST_KNOWN_GOOD_PATH)

    try:
        write_config_to_disk(rendered)
    except OSError as exc:
        _fail_and_maybe_rollback(loop, version, f"write failed: {exc}")
        return

    restarted, restart_error = restart_router_container()
    if not restarted:
        _fail_and_maybe_rollback(loop, version, f"restart failed: {restart_error}")
        return

    deadline = time.monotonic() + _HEALTH_CHECK_TIMEOUT_S
    healthy = False
    while time.monotonic() < deadline:
        if _router_is_healthy():
            healthy = True
            break
        time.sleep(_HEALTH_CHECK_INTERVAL_S)

    if healthy:
        _publish_status_fresh(version, "ok")
        asyncio.run_coroutine_threadsafe(
            _record_audit("federation_config_applied", f"version={version}"), loop,
        )
    else:
        _fail_and_maybe_rollback(loop, version, "router did not become healthy after restart")


def start_federation_subscriber(loop: asyncio.AbstractEventLoop) -> "zenoh.Session | None":
    signers = _trusted_signers()
    if not signers:
        print("[federation] no trusted signers configured (parent or root) — "
              "federation subscriber not started, this instance accepts no pushes", flush=True)
        return None
    if not _OWN_NAMESPACE:
        print("[federation] PARTNER_NAMESPACE unset — cannot determine own config topic, "
              "federation subscriber not started", flush=True)
        return None

    session = _open_local_session()
    session.declare_subscriber(_config_topic(), lambda sample: _handle_config_push(session, loop, sample))
    print(f"[federation] subscribed on {_config_topic()}, trusted signer CNs={[cn for _, cn in signers]!r}", flush=True)
    return session
