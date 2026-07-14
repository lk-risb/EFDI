"""Optional OIDC / OAuth2 single sign-on (Keycloak, Authentik, any compliant
provider). Entirely gated behind environment configuration: with no issuer /
client credentials set, OIDC_ENABLED is False, the routes short-circuit, and
the app behaves exactly as before. This is infrastructure to connect an IdP
later — nothing runs unless it's configured.

Flow: /auth/oidc/login redirects to the IdP → IdP redirects back to
/auth/oidc/callback → we exchange the code, map the user's IdP groups to a
panel role, JIT-provision (or link) an AdminUser, then set the same httponly
refresh cookie a password login would and redirect to the SPA. On load the
SPA trades that cookie for an access token via /auth/refresh, so no token ever
travels in a URL."""

import hashlib
import os
import re
import secrets

# authlib is an OPTIONAL dependency — OIDC is opt-in infrastructure. If it isn't
# installed (e.g. a local dev venv that skipped it), OIDC is simply disabled and
# the rest of the admin API boots normally, rather than the whole app failing to
# import. Production images install it via requirements.txt.
try:
    from authlib.integrations.starlette_client import OAuth, OAuthError
    _AUTHLIB_AVAILABLE = True
except ImportError:
    _AUTHLIB_AVAILABLE = False
    OAuth = None
    class OAuthError(Exception):  # placeholder so the callback's except clause resolves
        pass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import pwd_ctx, write_audit
from .models import AdminUser
from .auth import create_refresh_token, set_refresh_cookie

router = APIRouter(prefix="/auth/oidc", tags=["auth"])

# --- Configuration (env-driven) ---------------------------------------------
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_PROVIDER_NAME = os.environ.get("OIDC_PROVIDER_NAME", "SSO")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email groups")
OIDC_GROUPS_CLAIM = os.environ.get("OIDC_GROUPS_CLAIM", "groups")
# Explicit redirect URL registered with the IdP. Behind a reverse proxy the
# request URL the app sees is often the internal http one, so this override is
# usually required in prod (e.g. https://efdi.example.com/auth/oidc/callback).
OIDC_REDIRECT_URL = os.environ.get("OIDC_REDIRECT_URL", "")
# Panel role granted when no group maps. Kept deliberately low.
OIDC_DEFAULT_ROLE = os.environ.get("OIDC_DEFAULT_ROLE", "readonly")
# "idp-group=panel-role" pairs, comma-separated.
# e.g. OIDC_ROLE_MAP="efdi-superadmins=superadmin,efdi-ops=admin,efdi-ro=readonly"
OIDC_ROLE_MAP_RAW = os.environ.get("OIDC_ROLE_MAP", "")
# Where to send the browser after a successful callback — the SPA root, which
# trades the refresh cookie for an access token on load.
OIDC_POST_LOGIN_URL = os.environ.get("OIDC_POST_LOGIN_URL", "/")

OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and _AUTHLIB_AVAILABLE)

_VALID_ROLES = {"superadmin", "admin", "readonly"}
# Higher wins when a user is in multiple mapped groups.
_ROLE_PRIORITY = {"superadmin": 2, "admin": 1, "readonly": 0}


def _parse_role_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        group, role = (p.strip() for p in pair.split("=", 1))
        if group and role in _VALID_ROLES:
            mapping[group] = role
    return mapping


_ROLE_MAP = _parse_role_map(OIDC_ROLE_MAP_RAW)

# Registered lazily only when enabled, so an unconfigured deployment never
# touches authlib or the IdP discovery URL.
oauth = OAuth() if _AUTHLIB_AVAILABLE else None
if OIDC_ENABLED:
    oauth.register(
        name="idp",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_ISSUER}/.well-known/openid-configuration",
        client_kwargs={"scope": OIDC_SCOPES},
    )


def _role_for_groups(groups: list[str]) -> str:
    """Highest-priority role among the user's mapped groups, else the default."""
    roles = [_ROLE_MAP[g] for g in groups if g in _ROLE_MAP]
    if not roles:
        return OIDC_DEFAULT_ROLE if OIDC_DEFAULT_ROLE in _VALID_ROLES else "readonly"
    return max(roles, key=lambda r: _ROLE_PRIORITY.get(r, 0))


async def _provision_user(db: AsyncSession, claims: dict) -> AdminUser:
    """Find by stable IdP subject or JIT-create an isolated OIDC account."""
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise HTTPException(status_code=400, detail="OIDC token missing 'sub'")

    raw_username = str(claims.get("preferred_username") or claims.get("email") or "oidc-user")
    username = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw_username).strip(".-") or "oidc-user"
    username = username[:64]
    groups = claims.get(OIDC_GROUPS_CLAIM) or []
    if not isinstance(groups, list):
        groups = [groups]
    role = _role_for_groups([str(g) for g in groups])

    # A username claim is display data, not an account-linking credential. Never
    # attach it to an existing local account: a colliding IdP username must not
    # be able to convert (and inherit) a privileged password account.
    result = await db.execute(select(AdminUser).where(AdminUser.oidc_subject == subject))
    user = result.scalar_one_or_none()
    if user:
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        user.role = role
    else:
        result = await db.execute(select(AdminUser).where(AdminUser.username == username))
        if result.scalar_one_or_none() is not None:
            suffix = hashlib.sha256(subject.encode()).hexdigest()[:10]
            username = f"{username[:48]}-oidc-{suffix}"
        user = AdminUser(
            username=username,
            # Unusable random hash — SSO accounts can never password-login.
            password_hash=pwd_ctx.hash(secrets.token_urlsafe(32)),
            role=role,
            auth_provider="oidc",
            oidc_subject=subject,
            created_by="oidc",
        )
        db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # The unique subject index also closes the concurrent first-login race.
        await db.rollback()
        result = await db.execute(select(AdminUser).where(AdminUser.oidc_subject == subject))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=409, detail="OIDC account provisioning conflict") from exc
    await db.refresh(user)
    return user


@router.get("/config")
async def oidc_config():
    """Public — lets the login page decide whether to show the SSO button."""
    return {"enabled": OIDC_ENABLED, "provider_name": OIDC_PROVIDER_NAME}


@router.get("/login")
async def oidc_login(request: Request):
    if not OIDC_ENABLED:
        raise HTTPException(status_code=404, detail="OIDC not configured")
    redirect_uri = OIDC_REDIRECT_URL or str(request.url_for("oidc_callback"))
    return await oauth.idp.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="oidc_callback")
async def oidc_callback(request: Request, db: AsyncSession = Depends(get_db)):
    if not OIDC_ENABLED:
        raise HTTPException(status_code=404, detail="OIDC not configured")
    try:
        token = await oauth.idp.authorize_access_token(request)
    except OAuthError:
        # State/nonce mismatch, user-denied consent, etc. — bounce to login.
        return RedirectResponse(url="/login?error=oidc", status_code=303)

    # userinfo enriches (and, for some IdPs, is the only source of) the groups
    # claim beyond what the id_token carries.
    claims = dict(token.get("userinfo") or {})
    id_token_subject = claims.get("sub")
    try:
        info = await oauth.idp.userinfo(token=token)
        userinfo = {k: v for k, v in dict(info).items() if v is not None}
        if id_token_subject and userinfo.get("sub") != id_token_subject:
            raise HTTPException(status_code=400, detail="OIDC userinfo subject mismatch")
        claims.update(userinfo)
    except HTTPException:
        raise
    except Exception:
        pass  # id_token claims alone are sufficient if userinfo is unavailable.

    user = await _provision_user(db, claims)

    # Mint the same refresh session a password login would (mirrors auth.login).
    refresh_row, raw_refresh = create_refresh_token(user.id)
    db.add(refresh_row)
    await db.commit()
    await write_audit(db, user.id, "login_oidc")

    response = RedirectResponse(url=OIDC_POST_LOGIN_URL, status_code=303)
    set_refresh_cookie(response, raw_refresh)
    return response
