"""Federation topology autodiscovery. Each pod's admin periodically publishes a
self-describing fact — its own namespace, cert CN, direct parent namespace, and
directly-registered children — on {prefix}/{own-ns}/@topology/v1. Any node
subscribes with a single {prefix}/**/@topology/v1 wildcard and reconstructs the
whole tree client-side from the parent pointers. Every non-root fact carries a
bounded public delegation chain, so the root verifies descendants without
preloading every descendant certificate or trusting a child's plain claim.

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
from pathlib import Path

import zenoh
from fastapi import APIRouter, Depends
from sqlalchemy import select

from .db import SessionLocal
from .config import CONFIG_PATH, ConfigFields, _extract_fields
from .deps import require_role
from .federation_crypto import FederationVerifyError, sign_payload, verify_envelope
from .local_zenoh import config_fingerprint, open_local_session
from .models import ConfigRevision, FederatedChild, Revocation, TrustAuthority
from .pki import ensure_local_authority
from .trust_identity import router_identity
from .trust_store import TrustStoreError, export_trust_chain, verify_trust_chain
from .trust_types import ControlAction

router = APIRouter(prefix="/api/topology", tags=["topology"])

_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")
_PARENT_NAMESPACE = os.environ.get("EFDI_PARENT_NAMESPACE", "").strip("/")
_TRUST_BOOTSTRAP = Path(os.environ.get(
    "EFDI_TRUST_BOOTSTRAP_PATH", "/certs/efdi/trust/managed-bootstrap.json"
))
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_POLICY_KEY = Path(os.environ.get("EFDI_POLICY_SIGNER_KEY_PATH", "/certs/policy-signer-key.pem"))
_POLICY_CERT = Path(os.environ.get("EFDI_POLICY_SIGNER_CERT_PATH", "/certs/policy-signer-cert.pem"))

_PUBLISH_INTERVAL_S = 15
_STALE_AFTER_S = _PUBLISH_INTERVAL_S * 3
_MAX_FACT_BYTES = 256 * 1024
_MAX_NAMESPACE_LEN = 255
_MAX_NODES = 4096
_MAX_CHILDREN = 256

# namespace -> {"fact": {...}, "last_seen": monotonic_seconds}. Zenoh invokes
# the subscriber on its callback thread while FastAPI iterates the store in the
# endpoint, so both paths must hold the lock (a concurrent insertion can resize
# a dict while it is being iterated).
_TOPOLOGY: dict[str, dict] = {}
_TOPOLOGY_LOCK = threading.Lock()

_PEER_RE = re.compile(r"^@/[^/]+/router/transport/unicast/([^/]+)$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


def _prefix() -> str:
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", "LTU/CISB")


def _parent_namespace() -> str | None:
    if _PARENT_NAMESPACE:
        return _PARENT_NAMESPACE
    try:
        bootstrap = json.loads(_TRUST_BOOTSTRAP.read_text(encoding="utf-8"))
        value = bootstrap.get("parent_namespace")
        if isinstance(value, str) and _NAMESPACE_RE.fullmatch(value):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


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


def _neighbors_from_router_record(payload: object) -> list[dict]:
    """Extract router peers from Zenoh 1.9's aggregate router record."""
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        return []
    neighbors = []
    for item in payload["sessions"][:512]:
        if not isinstance(item, dict) or item.get("whatami") not in {"router", "peer"}:
            continue
        peer = item.get("peer")
        if not isinstance(peer, str) or not re.fullmatch(r"[0-9a-fA-F]{1,64}", peer):
            continue
        links = item.get("links")
        links = links if isinstance(links, list) else []
        protocols = set()
        for link in links[:128]:
            if not isinstance(link, dict):
                continue
            for locator in (link.get("src"), link.get("dst")):
                if not isinstance(locator, str):
                    continue
                protocol = locator.split("/", 1)[0].lower()
                if re.fullmatch(r"[a-z0-9-]{1,16}", protocol):
                    protocols.add(protocol)
        neighbors.append({
            "router_zid": peer,
            "whatami": item.get("whatami"),
            "link_count": len(links),
            "protocols": sorted(protocols),
        })
    return neighbors


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

    # Zenoh 1.9 exposes established sessions in the aggregate router record;
    # older versions also exposed transport/unicast subkeys. Merge both so the
    # topology map reflects actual links across the supported runtime shape.
    try:
        aggregate_replies = list(session.get(
            f"@/{zid}/router",
            target=zenoh.QueryTarget.ALL,
            timeout=5,
        ))
    except Exception:
        aggregate_replies = []
    for reply in aggregate_replies:
        if not reply.ok:
            continue
        try:
            payload = json.loads(bytes(reply.ok.payload).decode())
        except (ValueError, UnicodeDecodeError):
            continue
        for neighbor in _neighbors_from_router_record(payload):
            current = neighbors.setdefault(neighbor["router_zid"], neighbor)
            current["protocols"] = sorted(
                set(current.get("protocols", [])) | set(neighbor["protocols"])
            )
            current["link_count"] = max(
                current.get("link_count") or 0,
                neighbor["link_count"],
            )
    return zid, sorted(neighbors.values(), key=lambda item: item["router_zid"])


async def _own_fact(session: "zenoh.Session") -> dict:
    async with SessionLocal() as db:
        result = await db.execute(select(FederatedChild.namespace))
        children = sorted(result.scalars().all())[:_MAX_CHILDREN]
        authority = await ensure_local_authority(db)
        trust_chain = await export_trust_chain(db, authority)
    loop = asyncio.get_running_loop()
    zid, neighbors = await loop.run_in_executor(None, _observed_neighbors, session)
    try:
        with open(CONFIG_PATH) as handle:
            config_fields = _extract_fields(handle.read()).model_dump()
    except (OSError, ValueError, KeyError, TypeError):
        config_fields = None
    parent_namespace = _parent_namespace()
    return {
        "namespace": _OWN_NAMESPACE,
        "router_zid": zid,
        "parent_namespace": parent_namespace,
        "role": "pod" if parent_namespace else "hq",
        "children": children,
        "healthy": zid is not None,
        "neighbors": neighbors,
        "config_fields": config_fields,
        "trust_chain": trust_chain,
        "version": int(time.time()),
        "ts": time.time(),
    }


def _open_session() -> "zenoh.Session":
    return open_local_session()


def _normalize_fact(fact: dict) -> dict | None:
    namespace = fact.get("namespace")
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        return None
    router_zid = fact.get("router_zid")
    if router_zid is not None and (
        not isinstance(router_zid, str) or not re.fullmatch(r"[0-9a-fA-F]{1,64}", router_zid)
    ):
        router_zid = None
    parent = fact.get("parent_namespace")
    if parent is not None and (not isinstance(parent, str) or not _NAMESPACE_RE.fullmatch(parent)):
        parent = None
    role = fact.get("role", "pod")
    if not isinstance(role, str) or len(role) > 32:
        role = "pod"
    children = fact.get("children", [])
    if not isinstance(children, list):
        children = []
    children = [
        child for child in children
        if isinstance(child, str) and _NAMESPACE_RE.fullmatch(child)
    ][: _MAX_CHILDREN]
    version = fact.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        version = 0
    timestamp = fact.get("ts", 0.0)
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        timestamp = 0.0
    config_fields = fact.get("config_fields")
    try:
        parsed_fields = ConfigFields.model_validate(config_fields)
        config_fields = (
            parsed_fields.model_dump()
            if parsed_fields.partner_namespace == namespace
            else None
        )
    except (ValueError, TypeError):
        config_fields = None
    return {
        "namespace": namespace,
        "router_zid": router_zid,
        "parent_namespace": parent,
        "role": role,
        "children": children,
        "healthy": bool(fact.get("healthy", False)),
        "neighbors": _safe_neighbors(fact.get("neighbors")),
        "config_fields": config_fields,
        "version": version,
        "ts": float(timestamp),
    }


def _handle_topology_sample(loop: asyncio.AbstractEventLoop, sample):
    """Schedule signature verification away from Zenoh's callback thread."""
    payload = bytes(sample.payload)
    if len(payload) > _MAX_FACT_BYTES:
        return
    try:
        envelope = json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError):
        return
    # Same guard as federation_status: attacker-reachable bytes can parse to a
    # non-dict top-level value that still succeeds json.loads.
    if not isinstance(envelope, dict):
        return
    prefix = _prefix().strip("/")
    key = str(sample.key_expr)
    expected_suffix = "/@topology/v1"
    if not (key.startswith(prefix + "/") and key.endswith(expected_suffix)):
        return
    topic_namespace = key[len(prefix) + 1:-len(expected_suffix)]
    asyncio.run_coroutine_threadsafe(
        _verify_and_record_topology(topic_namespace, envelope), loop
    )


async def _verify_and_record_topology(topic_namespace: str, envelope: dict) -> None:
    async with SessionLocal() as db:
        if topic_namespace == _OWN_NAMESPACE:
            try:
                signer_pem = _POLICY_CERT.read_bytes()
            except OSError:
                return
        else:
            payload = envelope.get("payload")
            proof = payload.get("trust_chain") if isinstance(payload, dict) else None
            anchor_data = proof.get("anchor") if isinstance(proof, dict) else None
            anchor_identity = anchor_data.get("identity_uri") if isinstance(anchor_data, dict) else None
            if not isinstance(anchor_identity, str):
                return
            anchor = (await db.execute(select(TrustAuthority).where(
                TrustAuthority.identity_uri == anchor_identity,
                TrustAuthority.parent_id.is_(None),
            ))).scalar_one_or_none()
            if anchor is None:
                return
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
                return
            expected_identity = router_identity(f"{_prefix().strip('/')}/{topic_namespace}")
            if (
                authority.identity_uri != expected_identity
                or authority.effective_grant is None
                or ControlAction.TOPOLOGY not in authority.effective_grant.control
            ):
                return
            signer_pem = authority.policy_signer_cert_pem.encode()
        try:
            fact = verify_envelope(envelope, signer_pem, purpose="topology")
        except FederationVerifyError:
            return
    normalized = _normalize_fact(fact)
    if normalized is None or topic_namespace != normalized["namespace"]:
        return
    ns = normalized["namespace"]
    with _TOPOLOGY_LOCK:
        now = time.monotonic()
        for old_ns, entry in list(_TOPOLOGY.items()):
            if now - entry["last_seen"] > _STALE_AFTER_S * 4:
                _TOPOLOGY.pop(old_ns, None)
        if ns not in _TOPOLOGY and len(_TOPOLOGY) >= _MAX_NODES:
            oldest = min(_TOPOLOGY, key=lambda name: _TOPOLOGY[name]["last_seen"])
            _TOPOLOGY.pop(oldest, None)
        _TOPOLOGY[ns] = {"fact": normalized, "last_seen": now, "verified": True}


async def _publish_loop(session: "zenoh.Session"):
    """Publish and reload the local subscription when router config changes."""
    current_fingerprint = config_fingerprint()
    try:
        while True:
            try:
                new_fingerprint = config_fingerprint()
                if new_fingerprint != current_fingerprint:
                    replacement = _open_session()
                    loop = asyncio.get_running_loop()
                    replacement.declare_subscriber(
                        _wildcard(), lambda sample, current_loop=loop: _handle_topology_sample(current_loop, sample)
                    )
                    old_session = session
                    session = replacement
                    current_fingerprint = new_fingerprint
                    old_session.close()
                    print(f"[topology] reloaded local session on {_wildcard()}", flush=True)

                fact = await _own_fact(session)
                signature = sign_payload(fact, _POLICY_KEY.read_bytes(), purpose="topology")
                payload = json.dumps(
                    {"payload": fact, "signature": signature}, separators=(",", ":")
                ).encode()
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, session.put, _own_topic(), payload)
                # Feed our own fact into the local store too, so a single-node
                # deployment (or the publishing node itself) always appears.
                normalized = _normalize_fact(fact)
                if normalized is not None:
                    with _TOPOLOGY_LOCK:
                        _TOPOLOGY[_OWN_NAMESPACE] = {
                            "fact": normalized,
                            "last_seen": time.monotonic(),
                            "verified": True,
                        }
            except Exception as exc:  # never let a transient error kill the loop
                print(f"[topology] publish failed: {exc}", flush=True)
            await asyncio.sleep(_PUBLISH_INTERVAL_S)
    finally:
        session.close()


def start_topology(loop: asyncio.AbstractEventLoop) -> "tuple[zenoh.Session, asyncio.Task] | tuple[None, None]":
    """Open the shared session, subscribe to the mesh-wide topology wildcard,
    and start the periodic self-publish. Returns (session, task) for shutdown,
    or (None, None) if this pod has no namespace configured."""
    if not _OWN_NAMESPACE:
        print("[topology] PARTNER_NAMESPACE unset — topology publisher/subscriber not started", flush=True)
        return None, None
    session = _open_session()
    session.declare_subscriber(_wildcard(), lambda sample: _handle_topology_sample(loop, sample))
    task = loop.create_task(_publish_loop(session))
    print(f"[topology] publishing on {_own_topic()}, subscribed on {_wildcard()}", flush=True)
    return session, task


def _snapshot_nodes() -> list[dict]:
    now = time.monotonic()
    with _TOPOLOGY_LOCK:
        entries = [
            (namespace, entry["fact"].copy(), now - entry["last_seen"], bool(entry.get("verified")))
            for namespace, entry in _TOPOLOGY.items()
        ]

    nodes = []
    for namespace, fact, age, verified in entries:
        nodes.append({
            "namespace": namespace,
            "router_zid": fact.get("router_zid"),
            "parent_namespace": fact.get("parent_namespace"),
            "role": fact.get("role", "pod"),
            "reported": True,
            "verified": verified,
            "children": fact.get("children", []),
            "healthy": bool(fact.get("healthy", False)),
            "online": age < _STALE_AFTER_S,
            "last_seen_seconds": round(age, 1),
            "neighbors": _safe_neighbors(fact.get("neighbors")),
            "config_fields": fact.get("config_fields"),
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
                    "config_fields": None,
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
        revision_result = await db.execute(
            select(ConfigRevision).order_by(ConfigRevision.created_at.desc()).limit(_MAX_NODES)
        )
        revisions = {}
        for revision in revision_result.scalars().all():
            revisions.setdefault(revision.target_namespace, revision)
    for node in nodes:
        child = by_namespace.get(node["namespace"])
        revision = revisions.get(node["namespace"])
        if revision is not None:
            node["config_status"] = revision.state
            node["config_status_version"] = revision.version
            node["config_status_at"] = (
                revision.completed_at or revision.created_at
            ).isoformat()
        elif child is not None:
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
