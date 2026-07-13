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
from .federation import router as federation_router
from .publish_script import router as publish_script_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_database()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_first_user()

    loop = asyncio.get_running_loop()
    federation_session = start_federation_subscriber(loop)
    federation_status_session = start_federation_status_subscriber(loop)

    yield

    if federation_session is not None:
        federation_session.close()
    if federation_status_session is not None:
        federation_status_session.close()


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

app.include_router(auth_router)
app.include_router(admin_users_router)
app.include_router(status_router)
app.include_router(config_router)
app.include_router(health_router)
app.include_router(branding_router)
app.include_router(federation_router)
app.include_router(publish_script_router)


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
