import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope
from sqlalchemy import text

from .db import engine, Base, ensure_database
from . import models  # noqa: F401 — ensures models register with Base
from .auth import router as auth_router, _ensure_first_user
from .admin_users import router as admin_users_router
from .status import router as status_router
from .config import router as config_router
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
from .deps import SECRET_KEY


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_database()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column-add for the OIDC linkage fields — create_all only
        # creates missing tables, never alters an existing admin_users, so a
        # pod upgraded from a pre-OIDC schema needs this explicit migration.
        await conn.execute(text(
            "ALTER TABLE admin_users "
            "ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(16) NOT NULL DEFAULT 'local', "
            "ADD COLUMN IF NOT EXISTS oidc_subject VARCHAR(255)"
        ))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_admin_users_oidc_subject "
            "ON admin_users (oidc_subject)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_refresh_tokens_user_id "
            "ON refresh_tokens (user_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_federated_children_created_by "
            "ON federated_children (created_by)"
        ))
        await conn.execute(text(
            "ALTER TABLE federated_children "
            "ALTER COLUMN last_status_version TYPE BIGINT, "
            "ADD COLUMN IF NOT EXISTS transport_cert_pem TEXT, "
            "ADD COLUMN IF NOT EXISTS cert_sha256 VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS max_delegation_depth INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE pki_invitations "
            "ADD COLUMN IF NOT EXISTS policy_csr_sha256 VARCHAR(64), "
            "ADD COLUMN IF NOT EXISTS policy_cert_pem TEXT, "
            "ADD COLUMN IF NOT EXISTS transport_chain_pem TEXT, "
            "ADD COLUMN IF NOT EXISTS grant_envelope_json TEXT, "
            "ADD COLUMN IF NOT EXISTS link_username VARCHAR(128), "
            "ADD COLUMN IF NOT EXISTS link_password_hash VARCHAR(255), "
            "ADD COLUMN IF NOT EXISTS authority_id UUID"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pki_invitations_authority_id "
            "ON pki_invitations (authority_id)"
        ))
    await _ensure_first_user()

    loop = asyncio.get_running_loop()
    _, federation_task = start_federation_subscriber(loop)
    _, federation_status_task = start_federation_status_subscriber(loop)
    _, federation_relay_task = start_relay_subscriber(loop)
    topology_session, topology_task = start_topology(loop)

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
app.include_router(config_router)
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


class SPAStaticFiles(StaticFiles):
    """Fall back to index.html for unknown client-side routes so the router
    can take over on direct navigation, refresh, or browser back/forward —
    otherwise a missing static file 404s as raw JSON instead of loading the app."""

    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # StaticFiles.get_response raises starlette.exceptions.HTTPException
            # directly — fastapi.HTTPException is a SUBCLASS of it, so catching
            # fastapi's version here never matches and every client-side route
            # (e.g. /config) 404s as raw JSON instead of falling back to the SPA.
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                return await super().get_response("index.html", scope)
            raise


STATIC_DIR = "/app/static"
if os.path.isdir(STATIC_DIR):
    app.mount("/", SPAStaticFiles(directory=STATIC_DIR, html=True), name="static")
