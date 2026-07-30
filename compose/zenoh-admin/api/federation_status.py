import asyncio
import json
import os
from datetime import datetime, timezone

import zenoh

from sqlalchemy import select

from .db import SessionLocal
from .deps import write_audit
from .federation_crypto import FederationVerifyError, verify_envelope
from .local_zenoh import config_fingerprint, open_local_session
from .models import ConfigRevision, FederatedChild, PkiInvitation, Revocation, TrustAuthority
from .trust_identity import router_identity
from .trust_store import TrustStoreError, authority_is_revoked, verify_trust_chain
from .trust_types import ControlAction

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
    return os.environ.get("NAMESPACE_PREFIX", "EFDI")


_STATUS_KEY_SUFFIX = "/@config/status/v1"


def _status_key_prefix() -> str:
    return _prefix().strip("/") + "/"


def _status_wildcard() -> str:
    return f"{_status_key_prefix()}**{_STATUS_KEY_SUFFIX}"


def _namespace_from_key(key_expr: str) -> str | None:
    """Extract the child namespace from a concrete status key_expr. Uses
    prefix/suffix slicing (not a `*`-segment split) because namespaces can
    themselves contain '/' (e.g. 'release/vilnius', see federation.tsx's own
    placeholder) — a single-segment wildcard would misparse those."""
    prefix = _status_key_prefix()
    if not (key_expr.startswith(prefix) and key_expr.endswith(_STATUS_KEY_SUFFIX)):
        return None
    namespace = key_expr[len(prefix):-len(_STATUS_KEY_SUFFIX)]
    return namespace or None


async def _record_status(namespace: str, version: int, health: str, error: str | None):
    async with SessionLocal() as db:
        result = await db.execute(select(FederatedChild).where(FederatedChild.namespace == namespace))
        child = result.scalar_one_or_none()
        revision_result = await db.execute(
            select(ConfigRevision)
            .where(
                ConfigRevision.target_namespace == namespace,
                ConfigRevision.version == version,
            )
            .order_by(ConfigRevision.created_at.desc())
            .limit(1)
        )
        revision = revision_result.scalar_one_or_none()
        if revision is not None:
            revision.state = "applied" if health == "ok" else health
            revision.detail = error[:1000] if error else None
            revision.completed_at = datetime.now(timezone.utc)

        if child is None and revision is None:
            # Status from a namespace we don't have a FederatedChild row for.
            # Deliberately no audit entry here (unlike a matched status) —
            # the wildcard subscription ({prefix}/**/@config/status/v1)
            # spans the whole mesh, so an unmatched namespace is the
            # expected common case, not an anomaly worth an audit-log
            # entry per occurrence. A matched status (below) is audited.
            #
            # A stderr log line IS emitted though (not an audit): #76 debugging
            # needs to see a status that arrived but matched no row — that's the
            # "same prefix, wrong leaf" mismatch (e.g. HQ registered the child
            # under a namespace string that differs from the child pod's actual
            # PARTNER_NAMESPACE). The other mismatch mode — child on a different
            # NAMESPACE_PREFIX — never reaches this callback at all (its key
            # falls outside HQ's subscription wildcard), so it shows as the
            # ABSENCE of any log line here; compare against the startup
            # "subscribed on ..." line to spot it.
            print(f"[federation-status] status received for namespace={namespace!r} "
                  f"(v{version}, {health}) but no FederatedChild row matches — ignoring", flush=True)
            return
        if child is not None:
            child.last_status = health
            child.last_status_version = version
            child.last_status_at = datetime.now(timezone.utc)
            child.last_status_error = error
        await db.commit()
        print(f"[federation-status] status matched managed target={namespace!r} "
              f"(v{version}, {health})", flush=True)
        await write_audit(
            db, None, "federation_status_received",
            f"target={namespace}, version={version}, health={health}" + (f", error={error}" if error else ""),
        )


def _handle_status_sample(loop: asyncio.AbstractEventLoop, sample):
    """Runs on zenoh's own callback thread — schedules the DB write onto the
    FastAPI event loop, same pattern as federation_apply.py's _handle_config_push.

    Status is cryptographically verified before it can update a revision or
    health badge. Descendants include their bounded delegation chain so a root
    can verify a grandchild without a direct enrollment row."""
    namespace = _namespace_from_key(str(sample.key_expr))
    if namespace is None:
        return
    raw = bytes(sample.payload)
    if len(raw) > 64 * 1024:
        return
    try:
        envelope = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return
    # json.loads() on attacker-reachable bytes can legally produce a
    # non-dict top-level value (None, a list, a string, a number) that
    # still parses successfully — the same crash class already found and
    # fixed once in federation_apply.py's envelope handling. Guard it here
    # too before calling .get() on it.
    if not isinstance(envelope, dict):
        return
    asyncio.run_coroutine_threadsafe(_verify_and_record_status(namespace, envelope), loop)


async def _verify_and_record_status(namespace: str, envelope: dict) -> None:
    async with SessionLocal() as db:
        raw_body = envelope.get("payload")
        proof = raw_body.get("trust_chain") if isinstance(raw_body, dict) else None
        authority = None
        if isinstance(proof, dict):
            anchor_data = proof.get("anchor")
            anchor_identity = anchor_data.get("identity_uri") if isinstance(anchor_data, dict) else None
            if isinstance(anchor_identity, str):
                anchor = (await db.execute(select(TrustAuthority).where(
                    TrustAuthority.identity_uri == anchor_identity,
                    TrustAuthority.parent_id.is_(None),
                ))).scalar_one_or_none()
                if anchor is not None:
                    revoked = set((await db.execute(select(Revocation.target_reference).where(
                        Revocation.state == "active"
                    ))).scalars().all())
                    try:
                        authority = verify_trust_chain(
                            proof,
                            anchor,
                            revoked_references=revoked,
                        )
                    except TrustStoreError:
                        authority = None
        if authority is None:
            invitation = (await db.execute(select(PkiInvitation).where(
                PkiInvitation.namespace == namespace,
                PkiInvitation.used_at.is_not(None),
            ).order_by(PkiInvitation.used_at.desc()).limit(1))).scalar_one_or_none()
            authority = await db.get(TrustAuthority, invitation.authority_id) if invitation else None
        if authority is None or not authority.policy_signer_cert_pem:
            return
        expected_identity = router_identity(f"{_prefix().strip('/')}/{namespace}")
        if authority.identity_uri != expected_identity:
            return
        effective_grant = getattr(authority, "effective_grant", None)
        if effective_grant is not None and ControlAction.STATUS not in effective_grant.control:
            return
        if getattr(authority, "state", "active") != "active":
            return
        if isinstance(authority, TrustAuthority) and await authority_is_revoked(db, authority):
            return
        try:
            body = verify_envelope(
                envelope,
                authority.policy_signer_cert_pem.encode(),
                purpose="status",
            )
        except FederationVerifyError:
            return
    version = body.get("version", -1)
    health = body.get("health", "unknown")
    error = body.get("error")
    if not isinstance(version, int) or isinstance(version, bool):
        return
    if health not in {"ok", "rejected", "rolled_back"}:
        return
    if error is not None and not isinstance(error, str):
        return
    if error is not None:
        error = error[:512]
    await _record_status(namespace, version, health, error)


_status_session: "zenoh.Session | None" = None


def _subscribe(loop: asyncio.AbstractEventLoop) -> "zenoh.Session":
    session = open_local_session()
    wildcard = _status_wildcard()
    session.declare_subscriber(wildcard, lambda sample: _handle_status_sample(loop, sample))
    print(f"[federation-status] subscribed on {wildcard}", flush=True)
    return session


async def _watch_status_session(loop: asyncio.AbstractEventLoop):
    global _status_session
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
                print(f"[federation-status] session reload failed: {exc}", flush=True)
                continue
            old_session = _status_session
            _status_session = replacement
            fingerprint = new_fingerprint
            if old_session is not None:
                old_session.close()
            print("[federation-status] reloaded local session after config change", flush=True)
    finally:
        if _status_session is not None:
            _status_session.close()
            _status_session = None


def start_federation_status_subscriber(loop: asyncio.AbstractEventLoop) -> "tuple[zenoh.Session | None, asyncio.Task | None]":
    global _status_session
    if not _OWN_NAMESPACE:
        print("[federation-status] PARTNER_NAMESPACE unset — status subscriber not started", flush=True)
        return None, None
    _status_session = _subscribe(loop)
    return _status_session, loop.create_task(_watch_status_session(loop))
