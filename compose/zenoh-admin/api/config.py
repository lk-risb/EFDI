import os
import re
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
import json5
import docker
from docker.errors import DockerException, NotFound
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import require_role, write_audit

router = APIRouter(prefix="/api/config", tags=["config"])

# Mounted read-write from ${POD_STATE_DIR}/zenoh — same file zenoh-router itself
# reads from (see compose/docker-compose.yml).
CONFIG_PATH = os.environ.get("ZENOH_CONFIG_PATH", "/zenoh-config/config.json5")
# Read-only mount of the repo's own host/zenoh-router.json5.tmpl — the single
# source of truth first-boot.sh also renders from. Re-rendering from here
# (instead of hand-editing the live JSON5) means a saved config can never
# drift from the template's structure — only the 5 templated values change.
TEMPLATE_PATH = os.environ.get("ZENOH_TEMPLATE_PATH", "/zenoh-config-template/zenoh-router.json5.tmpl")
# Name of THIS pod's zenoh-router container — the one this admin instance
# restarts + health-checks. Env-configurable (not hardcoded) so multiple pods
# can share one Docker host/daemon without cross-restarting each other's router
# (e.g. a multi-pod-per-host test rig, or HA co-location). Defaults to the
# standard single-pod-per-host name.
ZENOH_ROUTER_SERVICE_LABEL = os.environ.get("ZENOH_ROUTER_CONTAINER", "efdi-pod-zenoh-router")

# Paths inside the zenoh-router container — fixed by the compose volume layout,
# never user-editable (see host/zenoh-router.json5.tmpl header + first-boot.sh).
_FIXED_SUBSTITUTIONS = {
    "POD_CERT_PEM": "/etc/zenoh/tls/pod-cert.pem",
    "POD_KEY_PEM": "/etc/zenoh/tls/pod-key.pem",
    "CA_ROOTS_PEM": "/etc/zenoh/tls/ca-roots.pem",
}

# Namespace/endpoint values get embedded directly inside JSON5 string literals
# in the rendered config — restrict the charset so a value can't break out of
# the string or inject extra JSON5 structure.
_SAFE_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9._/:-]+$")

# Single source of truth for the org prefix — rw-mounted here, ro-mounted into
# every bridge (see namespace_prefix.py + docker-compose.yml). Writing it +
# restarting the data plane is how a WebUI prefix change goes live without
# recreating containers (their env is baked at create time).
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_DEFAULT_PREFIX = "LTU/CISB"


def _read_prefix_file() -> str:
    try:
        with open(_PREFIX_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("NAMESPACE_PREFIX", _DEFAULT_PREFIX)


def _write_prefix_file(value: str) -> None:
    with open(_PREFIX_FILE, "w") as f:
        f.write(value + "\n")


class ConfigFields(BaseModel):
    mtls_port: int
    local_tcp_port: int
    fabric_endpoint: str
    partner_namespace: str
    inbound_namespace: str
    namespace_prefix: str
    verify_name_on_connect: bool
    plugins_loading_enabled: bool

    @field_validator("partner_namespace", "inbound_namespace")
    @classmethod
    def _check_safe_namespace(cls, v: str) -> str:
        if not _SAFE_NAMESPACE_RE.match(v):
            raise ValueError("only letters, digits, '.', '_', '/', '-' are allowed")
        return v

    @field_validator("fabric_endpoint")
    @classmethod
    def _check_safe_endpoint(cls, v: str) -> str:
        if not _SAFE_ENDPOINT_RE.match(v):
            raise ValueError("only letters, digits, '.', '_', '/', ':', '-' are allowed")
        return v

    @field_validator("mtls_port", "local_tcp_port")
    @classmethod
    def _check_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("port must be between 1 and 65535")
        return v


def _extract_fields(raw: str) -> ConfigFields:
    data = json5.loads(raw)

    mtls_port = None
    local_tcp_port = None
    for ep in data["listen"]["endpoints"]:
        m = re.match(r"tls/[^:]+:(\d+)$", ep)
        if m:
            mtls_port = int(m.group(1))
        m = re.match(r"tcp/[^:]+:(\d+)$", ep)
        if m:
            local_tcp_port = int(m.group(1))

    fabric_endpoint = data["connect"]["endpoints"][0]

    storage_key_expr = data["plugins"]["storage_manager"]["storages"]["efdi_live"]["key_expr"]
    m = re.match(r"LTU/CISB/(.+)/\*\*$", storage_key_expr)
    partner_namespace = m.group(1) if m else ""

    inbound_namespace = ""
    for rule in data["access_control"]["rules"]:
        if rule.get("id") == "pod-inbound":
            ke = rule["key_exprs"][0]
            inbound_namespace = ke[:-3] if ke.endswith("/**") else ke
            break

    verify_name_on_connect = bool(data["transport"]["link"]["tls"]["verify_name_on_connect"])
    plugins_loading_enabled = bool(data["plugins_loading"]["enabled"])

    return ConfigFields(
        mtls_port=mtls_port,
        local_tcp_port=local_tcp_port,
        fabric_endpoint=fabric_endpoint,
        partner_namespace=partner_namespace,
        inbound_namespace=inbound_namespace,
        verify_name_on_connect=verify_name_on_connect,
        plugins_loading_enabled=plugins_loading_enabled,
    )


def _render_config(fields: ConfigFields) -> str:
    if not os.path.isfile(TEMPLATE_PATH):
        raise HTTPException(status_code=500, detail=f"Template not found at {TEMPLATE_PATH}")
    with open(TEMPLATE_PATH, "r") as f:
        rendered = f.read()

    subs = {
        "ZENOH_LISTEN_PORT": str(fields.mtls_port),
        "ZENOH_LOCAL_TCP_PORT": str(fields.local_tcp_port),
        "ZENOH_FABRIC_ENDPOINT": fields.fabric_endpoint,
        "PARTNER_NAMESPACE": fields.partner_namespace,
        "INBOUND_NAMESPACE": fields.inbound_namespace,
        "ZENOH_VERIFY_NAME_ON_CONNECT": "true" if fields.verify_name_on_connect else "false",
        "ZENOH_PLUGINS_LOADING_ENABLED": "true" if fields.plugins_loading_enabled else "false",
        **_FIXED_SUBSTITUTIONS,
    }
    for key, val in subs.items():
        rendered = rendered.replace("${%s}" % key, val)

    # Only check the keys we actually substitute — the template's header comment
    # documents first-boot.sh's own envsubst target (${POD_STATE_DIR}) and is
    # never meant to be substituted here either, same as real envsubst leaves it.
    for key in subs:
        if "${%s}" % key in rendered:
            raise HTTPException(status_code=500, detail=f"Template has unresolved placeholder: \\${{{key}}}")

    try:
        json5.loads(rendered)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"Rendered config is invalid JSON5: {exc}")

    return rendered


def write_config_to_disk(rendered: str) -> None:
    with open(CONFIG_PATH, "w") as f:
        f.write(rendered)


def restart_router_container() -> tuple[bool, str | None]:
    """Returns (restarted, restart_error) — restart_error is None on success."""
    try:
        client = docker.from_env()
        container = client.containers.get(ZENOH_ROUTER_SERVICE_LABEL)
        container.restart(timeout=15)
        return True, None
    except NotFound:
        return False, f"Container '{ZENOH_ROUTER_SERVICE_LABEL}' not found"
    except DockerException as exc:
        return False, str(exc)


@router.get("")
async def get_config(_=Depends(require_role("admin", "superadmin"))):
    if not os.path.isfile(CONFIG_PATH):
        raise HTTPException(status_code=404, detail=f"Config file not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r") as f:
        raw = f.read()
    try:
        fields = _extract_fields(raw)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not parse current config: {exc}")
    return {"fields": fields, "path": CONFIG_PATH}


@router.put("")
async def put_config(
    fields: ConfigFields,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    rendered = _render_config(fields)
    write_config_to_disk(rendered)
    restarted, restart_error = restart_router_container()

    await write_audit(db, actor.id, "update_zenoh_config",
                       "restarted" if restarted else f"write ok, restart failed: {restart_error}")

    return {"status": "written", "restarted": restarted, "restart_error": restart_error}
