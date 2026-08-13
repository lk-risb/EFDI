import asyncio
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor

import zenoh
from fastapi import APIRouter, Depends
from .deps import require_role
from .local_zenoh import _fields, local_connection_details, open_local_session

router = APIRouter(prefix="/api/status", tags=["status"])

# Parses "@/<zid>/router/subscriber/<key-expr>" → the key-expr part.
_SUBSCRIBER_RE = re.compile(r"^@/[^/]+/router/subscriber/(.+)$")
_QUERYABLE_RE = re.compile(r"^@/[^/]+/router/queryable/(.+)$")
# Parses "@/<zid>/router/transport/unicast/<peer-zid>" — one entry per live
# link to another zenoh instance (router or peer), whatever this fabric is
# federated with.
_PEER_RE = re.compile(r"^@/[^/]+/router/transport/unicast/([^/]+)$")
_ENDPOINT_RE = re.compile(r"^[a-z]+/(.+):(\d+)$")
_ENDPOINT_PROBE_TIMEOUT_S = 0.75


def _split_endpoint(endpoint: str) -> tuple[str, int] | None:
    match = _ENDPOINT_RE.match(endpoint)
    if not match:
        return None
    try:
        port = int(match.group(2))
    except ValueError:
        return None
    return match.group(1), port


def _endpoint_status(endpoint: str, connected_links: set[tuple[str, int]]) -> dict:
    parsed = _split_endpoint(endpoint)
    if parsed is None:
        return {"endpoint": endpoint, "state": "disconnected", "detail": "invalid endpoint"}
    host, port = parsed
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return {"endpoint": endpoint, "state": "disconnected", "detail": "DNS resolution failed"}

    resolved = {(entry[4][0], port) for entry in addresses}
    if resolved & connected_links:
        return {
            "endpoint": endpoint,
            "state": "connected",
            "detail": "Zenoh transport established",
        }

    last_error: OSError | None = None
    for family, socktype, protocol, _, address in addresses:
        try:
            with socket.socket(family, socktype, protocol) as probe:
                probe.settimeout(_ENDPOINT_PROBE_TIMEOUT_S)
                probe.connect(address)
            return {
                "endpoint": endpoint,
                "state": "degraded",
                "detail": "endpoint reachable; Zenoh transport not established",
            }
        except socket.timeout:
            return {
                "endpoint": endpoint,
                "state": "degraded",
                "detail": "connection timed out",
            }
        except OSError as exc:
            last_error = exc
    return {
        "endpoint": endpoint,
        "state": "disconnected",
        "detail": "connection failed" if last_error is None else str(last_error),
    }


def _endpoint_statuses(
    endpoints: list[str],
    connected_links: set[tuple[str, int]],
) -> list[dict]:
    if not endpoints:
        return []
    with ThreadPoolExecutor(max_workers=min(len(endpoints), 8)) as executor:
        return list(executor.map(
            lambda endpoint: _endpoint_status(endpoint, connected_links),
            endpoints,
        ))


def _query_router_admin_space() -> dict:
    """Blocking — runs off the event loop via run_in_executor. Queries the
    router's own admin space (requires the pod-admin-introspect ACL rule;
    see examples/zenoh-router.json5.tmpl). Returns whatever the router actually
    exposes — connection health always works, the subscriber/queryable/storage
    breakdown depends on the ACL rule being present."""
    session = open_local_session()
    endpoint, _ = local_connection_details()
    connected = False
    router_zid = None
    try:
        router_zids = list(session.info.routers_zid())
        if router_zids:
            connected = True
            router_zid = str(router_zids[0])
    except Exception:
        pass

    subscribers: list[str] = []
    queryables: list[str] = []
    storages: list[str] = []
    peers: dict[str, dict] = {}   # peer zid -> {zid, whatami, link_count}
    connected_links: set[tuple[str, int]] = set()
    admin_space_reachable = False

    if router_zid:
        try:
            root_replies = list(session.get(
                "@/{}/router".format(router_zid),
                target=zenoh.QueryTarget.ALL,
                timeout=5,
            ))
            for reply in root_replies:
                if not reply.ok:
                    continue
                admin_space_reachable = True
                try:
                    payload = json.loads(bytes(reply.ok.payload).decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                for item in payload.get("sessions", []) if isinstance(payload, dict) else []:
                    if not isinstance(item, dict) or item.get("whatami") not in {"router", "peer"}:
                        continue
                    peer_zid = str(item.get("peer") or "")
                    links = item.get("links") if isinstance(item.get("links"), list) else []
                    if peer_zid:
                        peers[peer_zid] = {
                            "zid": peer_zid,
                            "whatami": item.get("whatami", "unknown"),
                            "link_count": len(links),
                        }
                    for link in links:
                        parsed = _split_endpoint(str(link.get("dst", ""))) if isinstance(link, dict) else None
                        if parsed is not None:
                            connected_links.add(parsed)

            replies = list(session.get(
                "@/{}/router/**".format(router_zid),
                target=zenoh.QueryTarget.ALL,
                timeout=5,
            ))
            admin_space_reachable = admin_space_reachable or len(replies) > 0
            for r in replies:
                if not r.ok:
                    continue
                key = str(r.ok.key_expr)
                m = _SUBSCRIBER_RE.match(key)
                if m:
                    subscribers.append(m.group(1))
                    continue
                m = _QUERYABLE_RE.match(key)
                if m:
                    queryables.append(m.group(1))
                    continue
                if "/status/plugins/storage_manager/storages/" in key:
                    storages.append(key.rsplit("/", 1)[-1])
                    continue
                m = _PEER_RE.match(key)
                if m:
                    peer_zid = m.group(1)
                    info = {"zid": peer_zid, "whatami": "unknown", "link_count": None}
                    # Best-effort — payload shape isn't ACL-guaranteed like the
                    # key-expr pattern is, so a peer still shows up (zid only)
                    # even if this router build doesn't publish transport details.
                    try:
                        payload = json.loads(bytes(r.ok.payload).decode())
                        if isinstance(payload, dict):
                            info["whatami"] = payload.get("whatami", "unknown")
                            links = payload.get("links")
                            if isinstance(links, list):
                                info["link_count"] = len(links)
                    except Exception:
                        pass
                    peers[peer_zid] = info
        except Exception:
            pass

    try:
        fields = _fields()
        endpoints = []
        if fields is not None:
            endpoints = fields.fabric_endpoints or (
                [fields.fabric_endpoint] if fields.fabric_endpoint else []
            )
        return {
            "connected": connected,
            "router_zid": router_zid,
            "endpoint": endpoint,
            "admin_space_reachable": admin_space_reachable,
            "subscriber_count": len(subscribers),
            "subscribers": sorted(set(subscribers)),
            "queryable_count": len(queryables),
            "queryables": sorted(set(queryables)),
            "storages": sorted(set(storages)),
            "peer_count": len(peers),
            "peers": sorted(peers.values(), key=lambda p: p["zid"]),
            "fabric_endpoints": _endpoint_statuses(endpoints, connected_links),
        }
    finally:
        session.close()


@router.get("")
async def get_status(_=Depends(require_role("admin", "superadmin", "readonly"))):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _query_router_admin_space)
    return data
