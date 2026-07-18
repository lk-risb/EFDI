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
restart. Each fact also reports the router's currently observed transport
neighbors from Zenoh's local admin space. Those neighbors are informational
only: this module never scans IPs, opens a new router link, or treats a
topology fact as an authorization decision."""

import asyncio
import json
import os
import re
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

_PEER_RE = re.compile(r"^@/[^/]+/router/transport/unicast/([^/]+)$")


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


def _peer_protocols(payload: object) -> list[str]:
    """Extract bounded transport protocol names from an admin-space record."""
    if not isinstance(payload, dict):
        return []
    links = payload.get("links")
    if not isinstance(links, list):
        return []
    protocols: set[str] = set()
    for link in links:
        if not isinstance(link, dict):
            continue
        protocol = link.get("protocol")
        if not isinstance(protocol, str):
            locator = link.get("locator")
            protocol = locator.get("protocol") if isinstance(locator, dict) else None
        if isinstance(protocol, str) and re.fullmatch(r"[a-z0-9-]{1,16}", protocol.lower()):
            protocols.add(protocol.lower())
    return sorted(protocols)


def _observed_neighbors(session: "zenoh.Session") -> tuple[str | None, list[dict]]:
    """Read established router links from this router's admin space.

    This is a read-only query over the local session. Discovery metadata is
    intentionally limited to peer ZID, whatami, link count, and protocol; IP
    addresses and arbitrary admin-space payloads are not republished mesh-wide.
    """
    zid = _router_zid(session)
    if not zid:
        return None, []
    neighbors: dict[str, dict] = {}
    try:
        replies = list(session.get(
            f"@/{zid}/router/transport/unicast/**",
            target=zenoh.QueryTarget.ALL,
            timeout=5,
        ))
    except Exception:
        return zid, []
    for reply in replies:
        if not reply.ok:
            continue
        match = _PEER_RE.match(str(reply.ok.key_expr))
        if not match:
            continue
        peer_zid = match.group(1)
        if not re.fullmatch(r"[0-9a-fA-F]{1,64}", peer_zid):
            continue
        try:
            payload = json.loads(bytes(reply.ok.payload).decode())
        except (ValueError, UnicodeDecodeError):
            payload = {}
        whatami = payload.get("whatami", "unknown") if isinstance(payload, dict) else "unknown"
        if not isinstance(whatami, str) or len(whatami) > 16:
            whatami = "unknown"
        links = payload.get("links") if isinstance(payload, dict) else None
        link_count = len(links) if isinstance(links, list) else None
        current = neighbors.setdefault(peer_zid, {
            "router_zid": peer_zid,
            "whatami": whatami,
            "link_count": link_count,
            "protocols": [],
        })
        current["protocols"] = sorted(set(current["protocols"]) | set(_peer_protocols(payload)))
    return zid, sorted(neighbors.values(), key=lambda item: item["router_zid"])


async def _own_fact(session: "zenoh.Session") -> dict:
    async with SessionLocal() as db:
        result = await db.execute(select(FederatedChild.namespace))
        children = sorted(result.scalars().all())
    loop = asyncio.get_running_loop()
    zid, neighbors = await loop.run_in_executor(None, _observed_neighbors, session)
    return {
        "namespace": _OWN_NAMESPACE,
        "router_zid": zid,
        "parent_namespace": _PARENT_NAMESPACE or None,
        "role": "pod" if _PARENT_NAMESPACE else "hq",
        "children": children,
        "healthy": zid is not None,
        "neighbors": neighbors,
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
            "reported": True,
            "children": fact.get("children", []),
            "healthy": bool(fact.get("healthy", False)),
            "online": age < _STALE_AFTER_S,
            "last_seen_seconds": round(age, 1),
            "neighbors": _safe_neighbors(fact.get("neighbors")),
            "config_status": None,
            "config_status_version": None,
            "config_status_at": None,
        })
    nodes.sort(key=lambda node: node["namespace"])
    return nodes


def _safe_neighbors(value: object) -> list[dict]:
    """Keep only bounded, display-safe neighbor metadata from a fact."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:128]:
        if not isinstance(item, dict):
            continue
        zid = item.get("router_zid")
        if not isinstance(zid, str) or not re.fullmatch(r"[0-9a-fA-F]{1,64}", zid):
            continue
        whatami = item.get("whatami", "unknown")
        if not isinstance(whatami, str) or len(whatami) > 16:
            whatami = "unknown"
        link_count = item.get("link_count")
        if not isinstance(link_count, int) or not 0 <= link_count <= 128:
            link_count = None
        protocols = item.get("protocols")
        if not isinstance(protocols, list):
            protocols = []
        protocols = sorted({
            protocol.lower() for protocol in protocols[:8]
            if isinstance(protocol, str) and re.fullmatch(r"[a-z0-9-]{1,16}", protocol.lower())
        })
        result.append({
            "router_zid": zid,
            "whatami": whatami,
            "link_count": link_count,
            "protocols": protocols,
        })
    return result


def _transport_edges(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return transport edges and synthetic nodes for observed unknown peers."""
    by_zid = {
        node["router_zid"]: node["namespace"]
        for node in nodes
        if isinstance(node.get("router_zid"), str)
    }
    existing = {node["namespace"] for node in nodes}
    for node in list(nodes):
        for neighbor in node.get("neighbors", []):
            peer_zid = neighbor["router_zid"]
            if peer_zid in by_zid:
                continue
            synthetic = f"router:{peer_zid[:16]}"
            if synthetic not in existing:
                nodes.append({
                    "namespace": synthetic,
                    "router_zid": peer_zid,
                    "parent_namespace": None,
                    "role": "peer",
                    "children": [],
                    "healthy": True,
                    "online": True,
                    "last_seen_seconds": 0,
                    "neighbors": [],
                    "reported": False,
                    "config_status": None,
                    "config_status_version": None,
                    "config_status_at": None,
                })
                existing.add(synthetic)
                by_zid[peer_zid] = synthetic

    edge_by_pair: dict[tuple[str, str], dict] = {}
    for node in nodes:
        source = node["namespace"]
        for neighbor in node.get("neighbors", []):
            target = by_zid.get(neighbor["router_zid"])
            if not target or target == source:
                continue
            pair = tuple(sorted((source, target)))
            edge = edge_by_pair.setdefault(pair, {
                "source": pair[0],
                "target": pair[1],
                "protocols": [],
                "observers": [],
            })
            edge["protocols"] = sorted(set(edge["protocols"]) | set(neighbor["protocols"]))
            if source not in edge["observers"]:
                edge["observers"].append(source)
    return nodes, sorted(edge_by_pair.values(), key=lambda edge: (edge["source"], edge["target"]))


@router.get("")
async def get_topology(_=Depends(require_role("readonly", "admin", "superadmin"))):
    """All known topology facts, newest-seen state, with a staleness flag so the
    UI can grey out nodes that stopped publishing."""
    nodes = _snapshot_nodes()
    nodes, transport_edges = _transport_edges(nodes)
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
        "transport_edges": transport_edges,
        "generated_at": time.time(),
        "publish_interval_s": _PUBLISH_INTERVAL_S,
        "stale_after_s": _STALE_AFTER_S,
    }
