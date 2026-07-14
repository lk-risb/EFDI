"""Federation topology autodiscovery. Each pod's admin periodically publishes a
self-describing fact — its own namespace, cert CN, direct parent namespace, and
directly-registered children — on {prefix}/{own-ns}/@topology/v1. Any node
subscribes with a single {prefix}/**/@topology/v1 wildcard and reconstructs the
whole tree client-side from the parent pointers. Same scaling reasoning as the
@config mesh-wide wildcard: no per-hop relay logic, works to thousands of nodes.

The aggregator keeps the latest fact per namespace in memory with a last-seen
timestamp, so a pod that stops publishing (offline/partitioned) shows up as
stale rather than silently vanishing. Nothing here is persisted — topology is
live-derived state, rebuilt from the mesh within one publish interval of a
restart."""

import asyncio
import json
import os
import threading
import time

import zenoh
from fastapi import APIRouter, Depends
from sqlalchemy import select

from .db import SessionLocal
from .deps import require_role
from .models import FederatedChild

router = APIRouter(prefix="/api/topology", tags=["topology"])

_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
# Best-available parent pointer: the trusted-parent cert CN. EFDI certs are
# named per namespace ({ns}-cert.pem, CN={ns}), so the parent's CN IS its
# namespace. Unset on HQ/root (no parent) — such a node is a tree root.
_PARENT_NAMESPACE = os.environ.get("ZENOH_ADMIN_TRUSTED_PARENT_CN", "")
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")

_PUBLISH_INTERVAL_S = 15
_STALE_AFTER_S = _PUBLISH_INTERVAL_S * 3

# namespace -> {"fact": {...}, "last_seen": monotonic_seconds}. Zenoh invokes
# the subscriber on its callback thread while FastAPI iterates the store in the
# endpoint, so both paths must hold the lock (a concurrent insertion can resize
# a dict while it is being iterated).
_TOPOLOGY: dict[str, dict] = {}
_TOPOLOGY_LOCK = threading.Lock()


def _prefix() -> str:
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "LTU/CISB")


def _own_topic() -> str:
    return f"{_prefix()}/{_OWN_NAMESPACE}/@topology/v1"


def _wildcard() -> str:
    return f"{_prefix()}/**/@topology/v1"


def _router_zid(session: "zenoh.Session") -> str | None:
    try:
        zids = list(session.info.routers_zid())
        return str(zids[0]) if zids else None
    except Exception:
        return None


async def _own_fact(session: "zenoh.Session") -> dict:
    async with SessionLocal() as db:
        result = await db.execute(select(FederatedChild.namespace))
        children = sorted(result.scalars().all())
    zid = _router_zid(session)
    return {
        "namespace": _OWN_NAMESPACE,
        "router_zid": zid,
        "parent_namespace": _PARENT_NAMESPACE or None,
        "role": "pod" if _PARENT_NAMESPACE else "hq",
        "children": children,
        "healthy": zid is not None,
        "version": int(time.time()),
        "ts": time.time(),
    }


def _open_session() -> "zenoh.Session":
    """Client session to this pod's own local router — same connection config
    as federation_status's subscriber."""
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


def _handle_topology_sample(sample):
    """Runs on zenoh's callback thread — record the latest fact per namespace."""
    try:
        fact = json.loads(bytes(sample.payload).decode())
    except (ValueError, UnicodeDecodeError):
        return
    # Same guard as federation_status: attacker-reachable bytes can parse to a
    # non-dict top-level value that still succeeds json.loads.
    if not isinstance(fact, dict):
        return
    ns = fact.get("namespace")
    if not isinstance(ns, str) or not ns:
        return
    with _TOPOLOGY_LOCK:
        _TOPOLOGY[ns] = {"fact": fact, "last_seen": time.monotonic()}


async def _publish_loop(session: "zenoh.Session"):
    """Publish this pod's own fact every interval until cancelled."""
    while True:
        try:
            fact = await _own_fact(session)
            payload = json.dumps(fact).encode()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, session.put, _own_topic(), payload)
            # Feed our own fact into the local store too, so a single-node
            # deployment (or the publishing node itself) always appears.
            with _TOPOLOGY_LOCK:
                _TOPOLOGY[_OWN_NAMESPACE] = {"fact": fact, "last_seen": time.monotonic()}
        except Exception as exc:  # never let a transient error kill the loop
            print(f"[topology] publish failed: {exc}", flush=True)
        await asyncio.sleep(_PUBLISH_INTERVAL_S)


def start_topology(loop: asyncio.AbstractEventLoop) -> "tuple[zenoh.Session, asyncio.Task] | tuple[None, None]":
    """Open the shared session, subscribe to the mesh-wide topology wildcard,
    and start the periodic self-publish. Returns (session, task) for shutdown,
    or (None, None) if this pod has no namespace configured."""
    if not _OWN_NAMESPACE:
        print("[topology] PARTNER_NAMESPACE unset — topology publisher/subscriber not started", flush=True)
        return None, None
    session = _open_session()
    session.declare_subscriber(_wildcard(), _handle_topology_sample)
    task = loop.create_task(_publish_loop(session))
    print(f"[topology] publishing on {_own_topic()}, subscribed on {_wildcard()}", flush=True)
    return session, task


def _snapshot_nodes() -> list[dict]:
    now = time.monotonic()
    with _TOPOLOGY_LOCK:
        entries = [
            (namespace, entry["fact"].copy(), now - entry["last_seen"])
            for namespace, entry in _TOPOLOGY.items()
        ]

    nodes = []
    for namespace, fact, age in entries:
        nodes.append({
            "namespace": namespace,
            "router_zid": fact.get("router_zid"),
            "parent_namespace": fact.get("parent_namespace"),
            "role": fact.get("role", "pod"),
            "children": fact.get("children", []),
            "healthy": bool(fact.get("healthy", False)),
            "online": age < _STALE_AFTER_S,
            "last_seen_seconds": round(age, 1),
            "config_status": None,
            "config_status_version": None,
            "config_status_at": None,
        })
    nodes.sort(key=lambda node: node["namespace"])
    return nodes


@router.get("")
async def get_topology(_=Depends(require_role("readonly", "admin", "superadmin"))):
    """All known topology facts, newest-seen state, with a staleness flag so the
    UI can grey out nodes that stopped publishing."""
    nodes = _snapshot_nodes()
    async with SessionLocal() as db:
        result = await db.execute(select(FederatedChild))
        by_namespace = {child.namespace: child for child in result.scalars().all()}
    for node in nodes:
        child = by_namespace.get(node["namespace"])
        if child is None:
            continue
        node["config_status"] = child.last_status
        node["config_status_version"] = child.last_status_version
        node["config_status_at"] = (
            child.last_status_at.isoformat() if child.last_status_at else None
        )
    return {
        "nodes": nodes,
        "generated_at": time.time(),
        "publish_interval_s": _PUBLISH_INTERVAL_S,
        "stale_after_s": _STALE_AFTER_S,
    }
