import asyncio
import json
import os
import re
from fastapi import APIRouter, Depends
import zenoh

from .deps import require_role

router = APIRouter(prefix="/api/status", tags=["status"])

_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", "")
_ORG = os.environ.get("PARTNER_NAMESPACE", "")

# One persistent session for the lifetime of the process — same pattern every
# bridge in this repo already uses, just held open here instead of per-request.
_session: "zenoh.Session | None" = None


def _make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, _ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, _ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


def _get_session() -> "zenoh.Session":
    global _session
    if _session is None:
        _session = zenoh.open(_make_config())
    return _session


# Parses "@/<zid>/router/subscriber/<key-expr>" → the key-expr part.
_SUBSCRIBER_RE = re.compile(r"^@/[^/]+/router/subscriber/(.+)$")
_QUERYABLE_RE = re.compile(r"^@/[^/]+/router/queryable/(.+)$")


def _query_router_admin_space() -> dict:
    """Blocking — runs off the event loop via run_in_executor. Queries the
    router's own admin space (requires the pod-admin-introspect ACL rule;
    see host/zenoh-router.json5.tmpl). Returns whatever the router actually
    exposes — connection health always works, the subscriber/queryable/storage
    breakdown depends on the ACL rule being present."""
    session = _get_session()
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
        except Exception:
            pass

    return {
        "connected": connected,
        "router_zid": router_zid,
        "endpoint": _ENDPOINT,
        "admin_space_reachable": admin_space_reachable,
        "subscriber_count": len(subscribers),
        "subscribers": sorted(set(subscribers)),
        "queryable_count": len(queryables),
        "queryables": sorted(set(queryables)),
        "storages": sorted(set(storages)),
    }


@router.get("")
async def get_status(_=Depends(require_role("admin", "superadmin", "readonly"))):
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _query_router_admin_space)
    return data
