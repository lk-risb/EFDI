import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope
from .db import engine, Base
from . import models  # noqa: F401 — ensures models register with Base
from .auth import router as auth_router, _ensure_first_user
from .admin_users import router as admin_users_router
from .status import router as status_router
from .data_stats import router as data_stats_router
from .config import router as config_router
from .certs_bootstrap import router as certs_bootstrap_router
from .backbone_bootstrap import router as backbone_bootstrap_router
from .netbird_instances import router as netbird_instances_router
from .tak_package import router as tak_package_router
from .health import router as health_router
from .branding import router as branding_router
from .federation_apply import start_federation_subscriber
from .federation_status import start_federation_status_subscriber
from .federation_relay import start_relay_subscriber
from .federation import router as federation_router
from .publish_script import router as publish_script_router
from .oidc import router as oidc_router, OIDC_ENABLED
from .topology import router as topology_router, start_topology
from .control import router as control_router
from .audit import router as audit_router
from .logs import router as logs_router
from .shell import router as shell_router
from .config_revisions import router as config_revisions_router
from .pki import router as pki_router
from .managed_acl import router as managed_acl_router
from .trust_api import router as trust_router
from .topics import router as topics_router, start_topic_observer
from .sitaware_targets import router as sitaware_targets_router
from .streams import router as streams_router
from .deps import SECRET_KEY


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_first_user()

    loop = asyncio.get_running_loop()
    _, federation_task = start_federation_subscriber(loop)
    _, federation_status_task = start_federation_status_subscriber(loop)
    _, federation_relay_task = start_relay_subscriber(loop)
    topology_session, topology_task = start_topology(loop)
    topic_session = start_topic_observer()

    yield

    if topology_task is not None:
        topology_task.cancel()
        try:
            await topology_task
        except asyncio.CancelledError:
            pass
    for task in (federation_task, federation_status_task, federation_relay_task):
        if task is not None:
            task.cancel()
    for task in (federation_task, federation_status_task, federation_relay_task):
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
    if topology_session is not None:
        topology_session.close()
    if topic_session is not None:
        topic_session.close()


app = FastAPI(title="Zenoh Admin API", version="1.0.0", lifespan=lifespan)

# Prod serves the UI from the same origin (no CORS needed at all). This origin
# is for the Vite dev server only — unset ZENOH_ADMIN_DEV_CORS_ORIGIN in prod so
# no cross-origin, credentialed requests are ever accepted.
_dev_cors_origin = os.environ.get("ZENOH_ADMIN_DEV_CORS_ORIGIN", "")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_dev_cors_origin] if _dev_cors_origin else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Signed session cookie — authlib stores the OAuth state/nonce here across the
# IdP redirect round-trip. Added ONLY when OIDC is actually enabled: SessionMiddleware
# pulls in itsdangerous, an OIDC-only dependency, so importing it unconditionally
# would break boot on a deployment (e.g. a local dev venv) that skipped the
# optional OIDC deps. Reuses the JWT secret. https_only matches the secure
# refresh cookie — OIDC is prod-only (needs a real IdP + an https redirect URL).
if OIDC_ENABLED:
    from starlette.middleware.sessions import SessionMiddleware
    app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=True)

app.include_router(auth_router)
app.include_router(admin_users_router)
app.include_router(status_router)
app.include_router(data_stats_router)
app.include_router(config_router)
app.include_router(certs_bootstrap_router)
app.include_router(backbone_bootstrap_router)
app.include_router(netbird_instances_router)
app.include_router(tak_package_router)
app.include_router(health_router)
app.include_router(branding_router)
app.include_router(federation_router)
app.include_router(publish_script_router)
app.include_router(oidc_router)
app.include_router(topology_router)
app.include_router(control_router)
app.include_router(audit_router)
app.include_router(logs_router)
app.include_router(shell_router)
app.include_router(config_revisions_router)
app.include_router(pki_router)
app.include_router(managed_acl_router)
app.include_router(trust_router)
app.include_router(topics_router)
app.include_router(sitaware_targets_router)
app.include_router(streams_router)


class SPAStaticFiles(StaticFiles):
    """Fall back to index.html for unknown client-side routes so the router
    can take over on direct navigation, refresh, or browser back/forward —
    otherwise a missing static file 404s as raw JSON instead of loading the app."""

    async def get_response(self, path: str, scope: Scope):
        is_index = path in ("", "index.html")
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # StaticFiles.get_response raises starlette.exceptions.HTTPException
            # directly — fastapi.HTTPException is a SUBCLASS of it, so catching
            # fastapi's version here never matches and every client-side route
            # (e.g. /config) 404s as raw JSON instead of falling back to the SPA.
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                is_index = True
                response = await super().get_response("index.html", scope)
            else:
                raise
        # index.html has to be revalidated on every load — it's the only thing
        # naming which hashed asset/index-*.js is current. Without this,
        # nothing here ever told the browser it COULDN'T just reuse its own
        # heuristic cache of index.html indefinitely, so a redeploy could
        # silently keep serving a stale page referencing old, still-present
        # (but now-superseded) hashed assets — confirmed live: a shipped fix
        # sat unreachable in exactly this way until a manual hard-refresh.
        # Every other file here is content-hashed by Vite (a real code change
        # always gets a new filename), so those are safe to cache forever.
        response.headers["Cache-Control"] = (
            "no-store" if is_index else "public, max-age=31536000, immutable"
        )
        return response


STATIC_DIR = "/app/static"
if os.path.isdir(STATIC_DIR):
    app.mount("/", SPAStaticFiles(directory=STATIC_DIR, html=True), name="static")
