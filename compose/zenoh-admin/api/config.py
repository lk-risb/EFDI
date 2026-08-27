import asyncio
import errno
import hashlib
import logging
import os
import re
import stat
import tempfile
import threading
import time

import docker
import json5
import zenoh
from docker.errors import DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from .config_revisions import create_revision, set_revision_state
from .control import _control
from .db import get_db
from .deps import require_role, write_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])

# Mounted read-write from ${POD_STATE_DIR}/zenoh — same file zenoh-router itself
# reads from (see compose/docker-compose.yml).
CONFIG_PATH = os.environ.get("ZENOH_CONFIG_PATH", "/zenoh-config/config.json5")
# Read-only mount of the repo's own examples/zenoh-router.json5.tmpl — the single
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
# never user-editable (see examples/zenoh-router.json5.tmpl header + first-boot.sh).
# Zenoh 1.x applies one TLS identity to the whole router session.  Keep the
# credential choices server-side and named, rather than accepting paths from
# the WebUI.  A profile switch therefore changes the endpoint identity as one
# atomic configuration change; mixed-trust endpoints still require separate
# router processes.
_TLS_PROFILES = {
    "efdi": {
        "label": "Local mesh (EFDI CA)",
        "publish_cert_dir": "efdi",
        "publish_root_ca": "efdi-ca-root.pem",
        "publish_client_cert": "{client_cn}-cert.pem",
        "publish_client_key": "{client_cn}-key.pem",
        "listen_certificate": "/etc/zenoh/tls/pod-cert.pem",
        "listen_private_key": "/etc/zenoh/tls/pod-key.pem",
        "connect_certificate": "/etc/zenoh/tls/pod-cert.pem",
        "connect_private_key": "/etc/zenoh/tls/pod-key.pem",
        "root_ca": "/etc/zenoh/tls/ca-roots.pem",
    },
    "backbone": {
        "label": "Backbone (Desert Bread CA)",
        "publish_cert_dir": "efdi-backbone",
        "publish_root_ca": "ca-roots.pem",
        "publish_client_cert": "cert.pem",
        "publish_client_key": "key.pem",
        # The recovered hackathon identity is a client certificate. Keep the
        # EFDI server-capable identity on the local listener and use the
        # backbone identity only when dialing the remote router.
        # host/first-boot.sh stages the fixed-name source bundle into the
        # router's protected runtime directory.
        "listen_certificate": "/etc/zenoh/tls/pod-cert.pem",
        "listen_private_key": "/etc/zenoh/tls/pod-key.pem",
        "connect_certificate": "/etc/zenoh/tls/backbone/cert.pem",
        "connect_private_key": "/etc/zenoh/tls/backbone/key.pem",
        "root_ca": "/etc/zenoh/tls/backbone/ca-roots.pem",
    },
    "ltu-local": {
        "label": "LTU sandbox (EFDI LTU CA)",
        # Source bundle: compose/certs/efdi-ltu/. connect-ltu.sh validates and
        # stages the fixed-name client identity and LTU trust root.
        "publish_cert_dir": "efdi-ltu",
        "publish_root_ca": "ca.crt",
        "publish_client_cert": "client.pem",
        "publish_client_key": "client.key",
        # This certificate permits both TLS serverAuth and clientAuth. Use it
        # in both directions: LTU peers that dial this router otherwise reject
        # the unrelated pod-local EFDI listener certificate with UnknownCA.
        "listen_certificate": "/etc/zenoh/tls/ltu/client-chain.pem",
        "listen_private_key": "/etc/zenoh/tls/ltu/client.key",
        # connect-ltu.sh prepares a complete leaf+intermediate chain and a
        # runtime-only unencrypted key. Zenoh has no private-key passphrase
        # setting, so pointing it at the source bundle's encrypted key can
        # never establish a link.
        "connect_certificate": "/etc/zenoh/tls/ltu/client-chain.pem",
        "connect_private_key": "/etc/zenoh/tls/ltu/client.key",
        "root_ca": "/etc/zenoh/tls/ltu/ca.crt",
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
_DEFAULT_PREFIX = "EFDI"
_DATA_PREFIX_FILE = os.environ.get("DATA_NAMESPACE_PREFIX_FILE", "/data-topic-prefix")
_CONFIG_APPLY_LOCK = threading.RLock()
_LAST_KNOWN_GOOD_PATH = CONFIG_PATH + ".last-known-good"
_HEALTH_CHECK_TIMEOUT_S = 30
_HEALTH_CHECK_INTERVAL_S = 2
_MANAGEMENT_LINK_TIMEOUT_S = 60


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
    atomic_write(_PREFIX_FILE, value + "\n")


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
    atomic_write(_DATA_PREFIX_FILE, value + "\n")


def _data_topic_root(prefix: str, partner_namespace: str) -> str:
    return "/".join(part for part in (prefix.strip("/"), partner_namespace.strip("/")) if part)


def _requires_remote_link(fields: "ConfigFields") -> bool:
    return bool(fields.fabric_endpoints or fields.fabric_endpoint)


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


class RenderedConfigRequest(BaseModel):
    rendered: str = Field(min_length=1, max_length=512_000)


def _fabric_presets() -> list[dict[str, object]]:
    presets: list[dict[str, object]] = []
    for env_prefix in ("EFDI_LOCAL_FABRIC", "EFDI_BACKBONE_FABRIC"):
        raw = os.environ.get(f"{env_prefix}_ENDPOINTS", "[]")
        try:
            endpoints = json5.loads(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"{env_prefix}_ENDPOINTS is not a JSON array: {exc}",
            ) from exc
        if not isinstance(endpoints, list) or any(not isinstance(item, str) for item in endpoints):
            raise HTTPException(
                status_code=500,
                detail=f"{env_prefix}_ENDPOINTS must be a JSON array of endpoint strings",
            )
        if not endpoints:
            continue
        profile = os.environ.get(f"{env_prefix}_PROFILE", "efdi")
        if profile not in _TLS_PROFILES:
            raise HTTPException(
                status_code=500,
                detail=f"{env_prefix}_PROFILE names an unknown TLS profile",
            )
        ConfigFields._check_safe_endpoints(endpoints)
        presets.append(
            {
                "label": os.environ.get(f"{env_prefix}_LABEL", env_prefix),
                "endpoints": endpoints,
                "profile": profile,
            }
        )
    return presets


def _is_bootstrap_config(raw: str) -> bool:
    """True for the plaintext no-mTLS config install.sh always writes first.

    The bootstrap config has no connect/access_control/transport.link.tls
    sections — _extract_fields would raise on it. Detect this cheaply before
    attempting the full parse so GET /api/config can report a clean "not yet
    secured" state instead of a 500.
    """
    try:
        data = json5.loads(raw)
    except ValueError:
        return False
    return isinstance(data, dict) and "connect" not in data


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
        if rule.get("id") == "pod-inbound" and rule.get("key_exprs"):
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
            name
            for name, profile in _TLS_PROFILES.items()
            if tls_paths
            == (
                profile["connect_certificate"],
                profile["connect_private_key"],
                profile["root_ca"],
            )
            or tls_paths in profile.get("legacy_tls_paths", [])
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
    if os.path.isdir(path):
        # Docker silently creates an empty directory here instead of erroring
        # when a bind-mount source file didn't exist at container start (see
        # docker-compose.yml — config.json5/namespace-prefix/data-topic-prefix
        # are all individually bind-mounted). Clear that stray artifact so the
        # real file this function is about to write can take its place.
        try:
            os.rmdir(path)
        except OSError as exc:
            raise OSError(f"{path} is a non-empty directory, not the expected file — refusing to write") from exc
    directory = os.path.dirname(path) or "."
    try:
        fd, temporary_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    except PermissionError:
        # Some mounted state files live at the filesystem root (for compatibility
        # with older deployments), but the container user cannot create temp files
        # in "/". Fall back to an in-place write when the target file already
        # exists and is writable.
        if not os.path.exists(path):
            raise
        with open(path, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        return
    try:
        if os.path.exists(path):
            os.fchmod(fd, stat.S_IMODE(os.stat(path).st_mode))
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(temporary_path, path)
        except OSError as exc:
            # A single-file bind-mount cannot be renamed over — its target is a
            # mountpoint, so os.replace() raises EBUSY. mkstemp already proved
            # the directory is writable, so fall back to an in-place rewrite of
            # the mounted file (not atomic, but the only option for a mount).
            if exc.errno != errno.EBUSY or not os.path.exists(path):
                raise
            with open(path, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.unlink(temporary_path)
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


def router_is_healthy() -> bool:
    """Return true only when the managed router is running and healthy."""
    try:
        client = docker.from_env()
        container = client.containers.get(ZENOH_ROUTER_SERVICE_LABEL)
        container.reload()
        state = container.attrs.get("State", {})
        if state.get("Status") != "running":
            return False
        health = state.get("Health", {}).get("Status")
        return health == "healthy" if health is not None else True
    except (NotFound, DockerException):
        return False


def wait_for_router_health(timeout_s: int = _HEALTH_CHECK_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if router_is_healthy():
            return True
        time.sleep(_HEALTH_CHECK_INTERVAL_S)
    return False


def router_has_remote_router_link(fields: ConfigFields) -> bool:
    """Prove that the restarted router still has at least one router link.

    Container health alone only proves that Zenoh parsed and started. A child
    could be healthy while disconnected from its parent. Query the local
    router's own admin record and require a live router/peer session before a
    federated activation is committed.
    """
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json5.dumps([f"tcp/127.0.0.1:{fields.local_tcp_port}"]))
    session = None
    try:
        session = zenoh.open(conf)
        zids = list(session.info.routers_zid())
        if not zids:
            return False
        replies = list(session.get(f"@/{zids[0]}/router", target=zenoh.QueryTarget.ALL, timeout=3))
        for reply in replies:
            if not reply.ok:
                continue
            payload = json5.loads(bytes(reply.ok.payload).decode("utf-8"))
            sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
            if any(
                isinstance(item, dict) and item.get("whatami") in {"router", "peer"}
                for item in sessions
            ):
                return True
    except Exception:
        return False
    finally:
        if session is not None:
            session.close()
    return False


def wait_for_remote_router_link(
    fields: ConfigFields,
    timeout_s: int = _MANAGEMENT_LINK_TIMEOUT_S,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if router_has_remote_router_link(fields):
            return True
        time.sleep(_HEALTH_CHECK_INTERVAL_S)
    return False


def validate_rendered_config(rendered: str) -> tuple[bool, str]:
    """Run the pinned Zenoh binary against a disconnected candidate config."""
    try:
        result = _control("/v1/router/validate-config", method="POST", body={"config": rendered})
    except HTTPException as exc:
        return False, str(exc.detail)
    if not isinstance(result, dict):
        return False, "Host control agent returned an unexpected response to the config preflight"
    ok = result.get("ok") is True
    detail = result.get("output")
    if not isinstance(detail, str):
        detail = "Zenoh config preflight returned an invalid response"
    return ok, detail[:8000]


def apply_rendered_config(
    rendered: str,
    fields: ConfigFields,
    *,
    restart_native: bool,
    preserve_management: bool = False,
) -> dict:
    """Validate, atomically activate, health-check, and roll back a config.

    This is the one local activation path used by the WebUI and federation
    subscriber. The previous config and both namespace state files remain
    available until the new router is proven healthy. A child therefore never
    loses its last-known-good local configuration merely because its parent
    sent an invalid or non-starting candidate.
    """
    with _CONFIG_APPLY_LOCK:
        valid, validation_detail = validate_rendered_config(rendered)
        if not valid:
            return {
                "status": "rejected",
                "restarted": False,
                "rolled_back": False,
                "error": validation_detail,
                "native_process_restart_required": False,
                "native_process_restart_failures": [],
            }

        try:
            with open(CONFIG_PATH) as handle:
                previous_config = handle.read()
        except OSError as exc:
            return {
                "status": "rejected",
                "restarted": False,
                "rolled_back": False,
                "error": f"could not read current config: {exc}",
                "native_process_restart_required": False,
                "native_process_restart_failures": [],
            }
        previous_prefix = _read_prefix_file()
        previous_publish_prefix = _read_data_prefix_file()
        native_restart_required = (
            fields.namespace_prefix != previous_prefix
            or fields.publish_prefix != previous_publish_prefix
        )

        atomic_write(_LAST_KNOWN_GOOD_PATH, previous_config)
        try:
            write_config_to_disk(rendered)
            _write_prefix_file(fields.namespace_prefix)
            _write_data_prefix_file(fields.publish_prefix)
        except OSError as exc:
            atomic_write(CONFIG_PATH, previous_config)
            _write_prefix_file(previous_prefix)
            _write_data_prefix_file(previous_publish_prefix)
            return {
                "status": "rejected",
                "restarted": False,
                "rolled_back": True,
                "error": f"candidate write failed: {exc}",
                "native_process_restart_required": False,
                "native_process_restart_failures": [],
            }

        restarted, restart_error = restart_router_container()
        healthy = restarted and wait_for_router_health()
        management_connected = (
            not preserve_management
            or (healthy and wait_for_remote_router_link(fields))
        )
        if not healthy or not management_connected:
            atomic_write(CONFIG_PATH, previous_config)
            _write_prefix_file(previous_prefix)
            _write_data_prefix_file(previous_publish_prefix)
            rollback_restarted, rollback_error = restart_router_container()
            rollback_healthy = rollback_restarted and wait_for_router_health()
            reason = restart_error or (
                "router did not re-establish a remote management link after restart"
                if healthy and not management_connected
                else "router did not become healthy after restart"
            )
            if not rollback_healthy:
                reason += f"; rollback restart failed: {rollback_error or 'router unhealthy'}"
            return {
                "status": "rolled_back" if rollback_healthy else "failed",
                "restarted": False,
                "rolled_back": rollback_healthy,
                "error": reason,
                "native_process_restart_required": False,
                "native_process_restart_failures": [],
            }

        native_failures = restart_native_processes() if restart_native and native_restart_required else []
        return {
            "status": "applied",
            "restarted": True,
            "rolled_back": False,
            "error": None,
            "validation": validation_detail,
            "native_process_restart_required": native_restart_required,
            "native_process_restart_failures": native_failures,
        }


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
    common = {
        "path": CONFIG_PATH,
        "fabric_presets": _fabric_presets(),
        "tls_profiles": {
            name: profile["label"] for name, profile in _TLS_PROFILES.items()
        },
    }
    if _is_bootstrap_config(raw):
        return {"bootstrap": True, "fields": None, **common}
    try:
        fields = _extract_fields(raw)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not parse current config: {exc}")
    return {"bootstrap": False, "fields": fields, **common}


@router.get("/rendered")
async def get_rendered_config(_=Depends(require_role("admin", "superadmin"))):
    if not os.path.isfile(CONFIG_PATH):
        raise HTTPException(status_code=404, detail=f"Config file not found at {CONFIG_PATH}")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            rendered = handle.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read current config: {exc}") from exc
    return {"rendered": rendered, "path": CONFIG_PATH}


@router.post("/render")
async def render_config(fields: ConfigFields, _=Depends(require_role("admin", "superadmin"))):
    rendered = _render_config(fields)
    return {"rendered": rendered}


@router.post("/validate")
async def validate_config(
    fields: ConfigFields,
    _=Depends(require_role("superadmin")),
):
    rendered = _render_config(fields)
    valid, detail = validate_rendered_config(rendered)
    if not valid:
        raise HTTPException(status_code=422, detail=detail)
    return {"valid": True, "detail": detail}


@router.put("")
async def put_config(
    fields: ConfigFields,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    rendered = _render_config(fields)
    version = int(time.time() * 1000)
    revision = await create_revision(
        db,
        target_namespace=fields.partner_namespace,
        version=version,
        source="local",
        state="validating",
        config_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
        created_by=actor.id,
    )
    try:
        result = await asyncio.to_thread(
            apply_rendered_config,
            rendered,
            fields,
            restart_native=True,
            preserve_management=_requires_remote_link(fields),
        )
    except Exception as exc:
        logger.exception("apply_rendered_config crashed applying config for %s", fields.partner_namespace)
        await set_revision_state(db, revision, "failed", str(exc))
        raise HTTPException(status_code=500, detail=f"Unexpected error applying config: {exc}") from exc
    await set_revision_state(db, revision, result["status"], result.get("error"))

    await write_audit(
        db,
        actor.id,
        "update_zenoh_config",
        f"status={result['status']}"
        + (f", error={result['error']}" if result.get("error") else "")
        + (", native processes restarted"
           if result["native_process_restart_required"] and not result["native_process_restart_failures"]
           else ", native process restart failures: " + "; ".join(result["native_process_restart_failures"])
           if result["native_process_restart_failures"] else ""),
    )

    if result["status"] in {"rejected", "rolled_back", "failed"}:
        status_code = (
            422
            if result["status"] == "rejected"
            else 409
            if result["status"] == "rolled_back"
            else 500
        )
        raise HTTPException(status_code=status_code, detail=result["error"])
    return {**result, "version": version, "revision_id": revision.id}


@router.put("/rendered")
async def put_rendered_config(
    body: RenderedConfigRequest,
    db: AsyncSession = Depends(get_db),
    actor=Depends(require_role("superadmin")),
):
    try:
        fields = _extract_fields(body.rendered)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse rendered config: {exc}") from exc

    version = int(time.time() * 1000)
    revision = await create_revision(
        db,
        target_namespace=fields.partner_namespace,
        version=version,
        source="raw",
        state="validating",
        config_sha256=hashlib.sha256(body.rendered.encode()).hexdigest(),
        created_by=actor.id,
    )
    try:
        result = await asyncio.to_thread(
            apply_rendered_config,
            body.rendered,
            fields,
            restart_native=True,
        )
    except Exception as exc:
        logger.exception("apply_rendered_config crashed applying raw config for %s", fields.partner_namespace)
        await set_revision_state(db, revision, "failed", str(exc))
        raise HTTPException(status_code=500, detail=f"Unexpected error applying config: {exc}") from exc
    await set_revision_state(db, revision, result["status"], result.get("error"))

    await write_audit(
        db,
        actor.id,
        "update_zenoh_config_raw",
        f"status={result['status']}"
        + (f", error={result['error']}" if result.get("error") else "")
        + (", native processes restarted"
           if result["native_process_restart_required"] and not result["native_process_restart_failures"]
           else ", native process restart failures: " + "; ".join(result["native_process_restart_failures"])
           if result["native_process_restart_failures"] else ""),
    )

    if result["status"] in {"rejected", "failed"}:
        raise HTTPException(status_code=422 if result["status"] == "rejected" else 500, detail=result["error"])
    return {**result, "version": version, "revision_id": revision.id}
