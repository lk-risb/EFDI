import asyncio
import json
import re
from fastapi import APIRouter, Depends
import zenoh

from .deps import require_role
from .local_zenoh import local_connection_details, open_local_session

router = APIRouter(prefix="/api/status", tags=["status"])

# Parses "@/<zid>/router/subscriber/<key-expr>" → the key-expr part.
_SUBSCRIBER_RE = re.compile(r"^@/[^/]+/router/subscriber/(.+)$")
_QUERYABLE_RE = re.compile(r"^@/[^/]+/router/queryable/(.+)$")
# Parses "@/<zid>/router/transport/unicast/<peer-zid>" — one entry per live
# link to another zenoh instance (router or peer), whatever this fabric is
# federated with.
_PEER_RE = re.compile(r"^@/[^/]+/router/transport/unicast/([^/]+)$")


def _query_router_admin_space() -> dict:
    """Blocking — runs off the event loop via run_in_executor. Queries the
    router's own admin space (requires the pod-admin-introspect ACL rule;
    see host/zenoh-router.json5.tmpl). Returns whatever the router actually
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
    admin_space_reachable = False

    if router_zid:
        try:
            replies = list(session.get(
                "@/{}/router/**".format(router_zid),
                target=zenoh.QueryTarget.ALL,
                timeout=5,
            ))
            admin_space_reachable = len(replies) > 0
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
        }
    finally:
        session.close()


@router.get("")
async def get_status(_=Depends(require_role("admin", "superadmin", "readonly"))):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _query_router_admin_space)
    return data
