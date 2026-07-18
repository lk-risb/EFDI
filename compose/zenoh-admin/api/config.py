import os
import re
import stat
import tempfile
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
import json5
import docker
from docker.errors import DockerException, NotFound
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .deps import require_role, write_audit
from .control import _control

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
# Zenoh 1.x applies one TLS identity to the whole router session.  Keep the
# credential choices server-side and named, rather than accepting paths from
# the WebUI.  A profile switch therefore changes the endpoint identity as one
# atomic configuration change; mixed-trust endpoints still require separate
# router processes.
_TLS_PROFILES = {
    "efdi": {
        "listen_certificate": "/etc/zenoh/tls/pod-cert.pem",
        "listen_private_key": "/etc/zenoh/tls/pod-key.pem",
        "connect_certificate": "/etc/zenoh/tls/pod-cert.pem",
        "connect_private_key": "/etc/zenoh/tls/pod-key.pem",
        "root_ca": "/etc/zenoh/tls/ca-roots.pem",
    },
    "sandbox": {
        # The recovered hackathon identity is a client certificate. Keep the
        # EFDI server-capable identity on the local listener and use the
        # sandbox identity only when dialing the remote router.
        "listen_certificate": "/etc/zenoh/tls/pod-cert.pem",
        "listen_private_key": "/etc/zenoh/tls/pod-key.pem",
        "connect_certificate": "/etc/zenoh/tls/zenoh-sandbox/cert.pem",
        "connect_private_key": "/etc/zenoh/tls/zenoh-sandbox/key.pem",
        "root_ca": "/etc/zenoh/tls/zenoh-sandbox/ca-roots.pem",
    },
}

# Namespace/endpoint values get embedded directly inside JSON5 string literals
# in the rendered config — restrict the charset so a value can't break out of
# the string or inject extra JSON5 structure.
_SAFE_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9._/:-]+$")
_MAX_FABRIC_ENDPOINTS = 16

# Single source of truth for the org prefix — rw-mounted here, ro-mounted into
# every bridge (see namespace_prefix.py + docker-compose.yml). Writing it +
# restarting the data plane is how a WebUI prefix change goes live without
# recreating containers (their env is baked at create time).
_PREFIX_FILE = os.environ.get("NAMESPACE_PREFIX_FILE", "/namespace-prefix")
_DEFAULT_PREFIX = "LTU/CISB"
_DATA_PREFIX_FILE = os.environ.get("DATA_NAMESPACE_PREFIX_FILE", "/data-topic-prefix")


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
        f.flush()
        os.fsync(f.fileno())


def _read_data_prefix_file() -> str:
    try:
        with open(_DATA_PREFIX_FILE) as f:
            return f.read().strip().strip("/")
    except OSError:
        pass
    if "DATA_NAMESPACE_PREFIX" in os.environ:
        return os.environ["DATA_NAMESPACE_PREFIX"].strip().strip("/")
    return _read_prefix_file()


def _write_data_prefix_file(value: str) -> None:
    with open(_DATA_PREFIX_FILE, "w") as f:
        f.write(value + "\n")
        f.flush()
        os.fsync(f.fileno())


def _data_topic_root(prefix: str, partner_namespace: str) -> str:
    return "/".join(part for part in (prefix.strip("/"), partner_namespace.strip("/")) if part)


class ConfigFields(BaseModel):
    mtls_port: int
    local_tcp_port: int
    fabric_endpoint: str
    partner_namespace: str
    inbound_namespace: str
    namespace_prefix: str
    publish_prefix: str
    verify_name_on_connect: bool
    plugins_loading_enabled: bool
    # `fabric_endpoint` remains the compatibility/primary value used by older
    # clients. New clients may submit several explicitly configured links.
    fabric_endpoints: list[str] = Field(default_factory=list)
    fabric_tls_profile: str = "efdi"

    @field_validator("partner_namespace", "inbound_namespace", "namespace_prefix")
    @classmethod
    def _check_safe_namespace(cls, v: str) -> str:
        if not _SAFE_NAMESPACE_RE.match(v):
            raise ValueError("only letters, digits, '.', '_', '/', '-' are allowed")
        return v

    @field_validator("publish_prefix")
    @classmethod
    def _check_publish_prefix(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._/-]*", v):
            raise ValueError("only letters, digits, '.', '_', '/', '-' are allowed")
        return v.strip("/")

    @field_validator("fabric_endpoint")
    @classmethod
    def _check_safe_endpoint(cls, v: str) -> str:
        if v and not _SAFE_ENDPOINT_RE.match(v):
            raise ValueError("only letters, digits, '.', '_', '/', ':', '-' are allowed")
        return v

    @field_validator("fabric_endpoints")
    @classmethod
    def _check_safe_endpoints(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_FABRIC_ENDPOINTS:
            raise ValueError(f"at most {_MAX_FABRIC_ENDPOINTS} fabric endpoints are allowed")
        if len(set(v)) != len(v):
            raise ValueError("fabric endpoints must be unique")
        for endpoint in v:
            if not endpoint or not _SAFE_ENDPOINT_RE.match(endpoint):
                raise ValueError(
                    "fabric endpoints must contain only letters, digits, '.', '_', '/', ':', '-' "
                    "and must not be empty"
                )
        return v

    @field_validator("fabric_tls_profile")
    @classmethod
    def _check_tls_profile(cls, v: str) -> str:
        if v not in _TLS_PROFILES:
            raise ValueError(f"unknown fabric TLS profile: {v}")
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

    connect_endpoints = data["connect"]["endpoints"]
    if not isinstance(connect_endpoints, list):
        raise ValueError("connect.endpoints must be a list")
    fabric_endpoint = connect_endpoints[0] if connect_endpoints else ""

    namespace_prefix = _read_prefix_file()
    publish_prefix = _read_data_prefix_file()
    storage_key_expr = data["plugins"]["storage_manager"]["storages"]["efdi_live"]["key_expr"]
    if publish_prefix:
        storage_pattern = re.escape(publish_prefix) + r"/(.+)/\*\*$"
    else:
        storage_pattern = r"([^/]+)/\*\*$"
    m = re.match(storage_pattern, storage_key_expr)
    partner_namespace = m.group(1) if m else ""

    inbound_namespace = ""
    for rule in data["access_control"]["rules"]:
        if rule.get("id") == "pod-firstparty" and rule.get("key_exprs"):
            key = rule["key_exprs"][0]
            key = key[:-3] if key.endswith("/**") else key
            if publish_prefix and key.startswith(publish_prefix + "/"):
                partner_namespace = key[len(publish_prefix) + 1:]
            elif key:
                # Keep GET usable during a rolling upgrade where the state file,
                # storage expression, and ACL were rendered by different versions.
                partner_namespace = key.rsplit("/", 1)[-1]
        if rule.get("id") == "pod-inbound":
            ke = rule["key_exprs"][0]
            inbound_namespace = ke[:-3] if ke.endswith("/**") else ke
            break

    verify_name_on_connect = bool(data["transport"]["link"]["tls"]["verify_name_on_connect"])
    plugins_loading_enabled = bool(data["plugins_loading"]["enabled"])

    tls = data["transport"]["link"]["tls"]
    tls_paths = (
        tls.get("connect_certificate"),
        tls.get("connect_private_key"),
        tls.get("root_ca_certificate"),
    )
    fabric_tls_profile = next(
        (
            name for name, profile in _TLS_PROFILES.items()
            if tls_paths == (profile["connect_certificate"], profile["connect_private_key"], profile["root_ca"])
        ),
        None,
    )
    if fabric_tls_profile is None:
        raise ValueError("transport.link.tls uses an unknown fabric TLS profile")

    return ConfigFields(
        mtls_port=mtls_port,
        local_tcp_port=local_tcp_port,
        fabric_endpoint=fabric_endpoint,
        partner_namespace=partner_namespace,
        inbound_namespace=inbound_namespace,
        namespace_prefix=namespace_prefix,
        publish_prefix=publish_prefix,
        verify_name_on_connect=verify_name_on_connect,
        plugins_loading_enabled=plugins_loading_enabled,
        fabric_endpoints=connect_endpoints,
        fabric_tls_profile=fabric_tls_profile,
    )


def _render_config(fields: ConfigFields) -> str:
    if not os.path.isfile(TEMPLATE_PATH):
        raise HTTPException(status_code=500, detail=f"Template not found at {TEMPLATE_PATH}")
    with open(TEMPLATE_PATH, "r") as f:
        rendered = f.read()

    fabric_endpoints = fields.fabric_endpoints or ([fields.fabric_endpoint] if fields.fabric_endpoint else [])
    tls_profile = _TLS_PROFILES[fields.fabric_tls_profile]
    subs = {
        "ZENOH_LISTEN_PORT": str(fields.mtls_port),
        "ZENOH_LOCAL_TCP_PORT": str(fields.local_tcp_port),
        "ZENOH_CONNECT_ENDPOINTS": json5.dumps(fabric_endpoints),
        "PARTNER_NAMESPACE": fields.partner_namespace,
        "INBOUND_NAMESPACE": fields.inbound_namespace,
        "NAMESPACE_PREFIX": fields.namespace_prefix,
        "NAMESPACE_ROOT": fields.namespace_prefix.split("/")[0],
        "DATA_TOPIC_ROOT": _data_topic_root(fields.publish_prefix, fields.partner_namespace),
        "ZENOH_VERIFY_NAME_ON_CONNECT": "true" if fields.verify_name_on_connect else "false",
        "ZENOH_PLUGINS_LOADING_ENABLED": "true" if fields.plugins_loading_enabled else "false",
        "LISTEN_CERT_PEM": tls_profile["listen_certificate"],
        "LISTEN_KEY_PEM": tls_profile["listen_private_key"],
        "CONNECT_CERT_PEM": tls_profile["connect_certificate"],
        "CONNECT_KEY_PEM": tls_profile["connect_private_key"],
        "CA_ROOTS_PEM": tls_profile["root_ca"],
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


def atomic_write(path: str, content: str) -> None:
    """Durably replace a state file without exposing readers to partial data."""
    directory = os.path.dirname(path) or "."
    fd, temporary_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        if os.path.exists(path):
            os.fchmod(fd, stat.S_IMODE(os.stat(path).st_mode))
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def write_config_to_disk(rendered: str) -> None:
    atomic_write(CONFIG_PATH, rendered)


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


def restart_native_processes() -> list[str]:
    """Restart every currently running host process after a topic-root change."""
    try:
        runtime = _control("/v1/runtime")
    except Exception as exc:  # control failures are reported, not hidden
        return [f"host control unavailable: {exc}"]
    failures = []
    for service in runtime.get("services", []):
        name = service.get("name")
        if name in {"zenoh", "admin-control"} or not service.get("running"):
            continue
        try:
            result = _control(f"/v1/services/{name}/restart", method="POST")
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            continue
        if not result.get("ok", False):
            failures.append(f"{name}: {result.get('output', 'restart failed')}")
    return failures


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
    previous_prefix = _read_prefix_file()
    previous_publish_prefix = _read_data_prefix_file()
    rendered = _render_config(fields)
    write_config_to_disk(rendered)
    _write_prefix_file(fields.namespace_prefix)
    _write_data_prefix_file(fields.publish_prefix)
    restarted, restart_error = restart_router_container()
    native_restart_required = (
        fields.namespace_prefix != previous_prefix
        or fields.publish_prefix != previous_publish_prefix
    )
    native_restart_failures = restart_native_processes() if native_restart_required else []

    await write_audit(db, actor.id, "update_zenoh_config",
                       ("restarted" if restarted else f"write ok, restart failed: {restart_error}")
                       + (", native processes restarted"
                          if native_restart_required and not native_restart_failures
                          else ", native process restart failures: " + "; ".join(native_restart_failures)
                          if native_restart_failures else ""))

    return {"status": "written", "restarted": restarted,
            "restart_error": restart_error,
            "native_process_restart_required": native_restart_required,
            "native_process_restart_failures": native_restart_failures}
