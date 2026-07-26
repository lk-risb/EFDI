#!/usr/bin/env python3
"""Local control plane for the Zenoh Admin UI.

The bridges and output layers intentionally run as ordinary host processes,
not as one Docker container per integration.  This small localhost service is
the safe seam between the web API and that PID-managed runtime.  It accepts
only a fixed service allow-list, updates the deployment .env as data (never
as shell), and delegates lifecycle work to the existing start.sh/stop.sh
entrypoints.
"""

from __future__ import annotations

import base64
import json
import hashlib
import hmac
import os
import pty
import re
import secrets
import signal
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urllib_request
from urllib.parse import quote, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("POD_STATE_DIR", str(ROOT / "compose" / "state")))
ENV_FILE = Path(os.environ.get("EFDI_ENV_FILE", str(ROOT / "compose" / ".env")))
START_SCRIPT = ROOT / "start.sh"
STOP_SCRIPT = ROOT / "stop.sh"
CONTROL_HOST = os.environ.get("EFDI_CONTROL_BIND", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("EFDI_CONTROL_PORT", "18896"))


def _effective_control_token() -> str:
    explicit = os.environ.get("EFDI_CONTROL_TOKEN", "")
    if explicit:
        return explicit
    secret = os.environ.get("ZENOH_ADMIN_SECRET_KEY", "")
    if not secret:
        return ""
    return hashlib.sha256(f"efdi-control-v1:{secret}".encode()).hexdigest()


CONTROL_TOKEN = _effective_control_token()
SHELL_HOST = os.environ.get("EFDI_SHELL_CONTROL_HOST", "127.0.0.1")
SHELL_PORT = int(os.environ.get("EFDI_SHELL_CONTROL_PORT", "18897"))
SHELL_CONTAINER = os.environ.get("ZENOH_ROUTER_CONTAINER", "efdi-pod-zenoh-router")
SHELL_MAX_SECONDS = 5 * 60
LOG_TAIL_BYTES = 256 * 1024
CONFIG_VALIDATE_MAX_BYTES = 256 * 1024
SITAWARE_HQ_NVG_HEALTH_MAX_BYTES = 64 * 1024
SITAWARE_HQ_NVG_PULL_STALE_SECONDS = 60.0
_ROUTER_CA_CERT_VALUE = os.environ.get("EFDI_ROUTER_CA_CERT_PATH", "")
_ROUTER_CA_KEY_VALUE = os.environ.get("EFDI_ROUTER_CA_KEY_PATH", "")
_ROUTER_CA_CHAIN_VALUE = os.environ.get("EFDI_ROUTER_CA_CHAIN_PATH", "")
_STEP_CA_STATE_VALUE = os.environ.get("EFDI_STEP_CA_STATE_PATH", "")
_STEP_CA_IMAGE = os.environ.get(
    "EFDI_STEP_CA_IMAGE",
    "smallstep/step-ca:0.30.2@sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270",
)
ROUTER_CA_CERT_PATH = Path(_ROUTER_CA_CERT_VALUE) if _ROUTER_CA_CERT_VALUE else None
ROUTER_CA_KEY_PATH = Path(_ROUTER_CA_KEY_VALUE) if _ROUTER_CA_KEY_VALUE else None
ROUTER_CA_CHAIN_PATH = Path(_ROUTER_CA_CHAIN_VALUE) if _ROUTER_CA_CHAIN_VALUE else None
STEP_CA_STATE_PATH = Path(_STEP_CA_STATE_VALUE) if _STEP_CA_STATE_VALUE else None
PID_DIR = STATE_DIR / ".pids"
LOG_DIR = STATE_DIR / "logs"
LAUNCHER_STATE_FILE = STATE_DIR / "launcher-state.env"
_ENV_LOCK = threading.Lock()


# This is deliberately duplicated as a small public catalog.  The process
# launcher remains the source of truth for command details; the catalog gives
# the UI labels, grouping, and explanations without exposing shell internals.
SERVICE_SPECS = [
    ("zenoh", "Infrastructure", "Zenoh message router"),
    ("admin-control", "Infrastructure", "Web UI host control agent"),
    ("cert-renewer", "Infrastructure", "Short-lived transport certificate renewal"),
    ("airplaneslive", "Open-data bridges", "Airplanes.live ADS-B"),
    ("adsblol", "Open-data bridges", "ADSB.lol aircraft"),
    ("aprs", "Open-data bridges", "APRS-IS"),
    ("meteolt", "Open-data bridges", "meteo.lt weather"),
    ("dronuradaras", "Sensor bridges", "dronuradaras.lt sensors"),
    ("dji-cloud", "Sensor bridges", "DJI Cloud MQTT"),
    ("utm-ans", "Open-data bridges", "Oro navigacija UTM"),
    ("asterix", "Sensor bridges", "ASTERIX family bundle"),
    ("track-fusion", "Sensor bridges", "Track correlation"),
    ("stanag5516", "Protocols", "STANAG 5516 (Link-16 J-series)"),
    ("mavlink", "Protocols", "MAVLink / Remote ID"),
    ("opendroneid", "Protocols", "OpenDroneID translator"),
    ("vmf", "Protocols", "VMF MIL-STD-47001C"),
    ("nffi", "Protocols", "NFFI / STANAG 4677"),
    ("sapient", "Protocols", "SAPIENT / FLEX 335"),
    ("stanag4586", "Protocols", "STANAG 4586 UAV control (VSM)"),
    ("stanag4609", "Protocols", "STANAG 4609 KLV decoder"),
    ("cap", "Protocols", "CAP 1.2 alerts"),
    ("geojson", "Protocols", "GeoJSON / OGC Features"),
    ("mqtt", "Protocols", "MQTT sensor JSON"),
    ("sensorthings", "Protocols", "OGC SensorThings observations"),
    ("sparkplug", "Protocols", "Eclipse Sparkplug B (MQTT)"),
    ("spectrum", "Protocols", "Spectrum observations"),
    ("sensor-health", "Protocols", "Sensor health"),
    ("mission-route", "Protocols", "Mission routes"),
    ("mavlink-raw", "Raw ingress", "MAVLink raw socket"),
    ("stanag5516-raw", "Raw ingress", "STANAG 5516 JREAP-C raw socket"),
    ("vmf-raw", "Raw ingress", "VMF raw socket"),
    ("sapient-raw", "Raw ingress", "SAPIENT raw socket"),
    ("stanag4586-raw", "Raw ingress", "STANAG 4586 raw socket"),
    ("stanag4609-raw", "Raw ingress", "STANAG 4609 SRT/KLV ingest"),
    ("mqtt-raw", "Raw ingress", "MQTT broker ingress"),
    ("sensorthings-raw", "Raw ingress", "OGC SensorThings polling ingress"),
    ("cot_layer", "C2 outputs", "CoT → TAK Server (mTLS)"),
    ("tak-bridge", "C2 inputs", "TAK Server CoT ingress"),
    ("sitaware", "C2 inputs", "SitaWare HQ REST input"),
    # A _bridge brings a C2 system's data INTO the fabric, a _layer writes the
    # fabric OUT to a C2 system. Which side opens the socket is a transport
    # detail: SitaWare polls nvg_layer's feed, and that still makes it egress.
    ("nvg_bridge", "C2 inputs", "SitaWare NVG export → Zenoh"),
    ("nvg_layer", "C2 outputs", "NVG feed → SitaWare (SitaWare polls)"),
]
SERVICE_NAMES = {name for name, _, _ in SERVICE_SPECS}

# Which file backs each service. Surfaced in the UI so a service name is never
# ambiguous: `_bridge.py` under bridges/ owns an external connection, a module
# under protocols/ decodes an already-published wire format, and layers/ writes
# out to a C2 system. Several services share one script with different
# arguments (asterix runs cat.py once per --category), which is exactly the
# case the name alone cannot express.
SERVICE_SOURCES = {
    "zenoh": "(container) efdi-pod-zenoh-router",
    "admin-control": "admin_control.py",
    "cert-renewer": "(managed) step-ca certificate renewal",
    "airplaneslive": "bridges/airplaneslive_adsb_bridge.py",
    "adsblol": "bridges/adsblol_bridge.py",
    "aprs": "bridges/aprsis_bridge.py",
    "meteolt": "bridges/meteolt_forecast_bridge.py",
    "dronuradaras": "bridges/dronuradaras_bridge.py",
    "dji-cloud": "bridges/dji_cloud_api_bridge.py",
    "utm-ans": "bridges/utm_ans_bridge.py",
    "asterix": "protocols/vendors/asterix/cat.py",
    "track-fusion": "bridges/track_fusion_bridge.py",
    "stanag5516": "protocols/vendors/stanag/5516.py",
    "mavlink": "protocols/random/mavlink.py",
    "opendroneid": "protocols/random/opendroneid.py",
    "vmf": "protocols/random/vmf.py",
    "nffi": "protocols/random/nffi.py",
    "sapient": "protocols/vendors/sapient/flex335.py",
    "stanag4586": "protocols/vendors/stanag/4586.py",
    "stanag4609": "protocols/vendors/stanag/4609.py",
    "cap": "protocols/random/cap.py",
    "geojson": "protocols/random/geojson_features.py",
    "mqtt": "protocols/random/mqtt_json.py",
    "sensorthings": "protocols/random/sensorthings.py",
    "sparkplug": "protocols/vendors/sparkplug/sparkplug.py",
    "spectrum": "protocols/random/spectrum_observation.py",
    "sensor-health": "protocols/random/sensor_health.py",
    "mission-route": "protocols/random/mission_route.py",
    "mavlink-raw": "bridges/mavlink_raw_bridge.py",
    "stanag5516-raw": "bridges/5516_bridge.py",
    "vmf-raw": "bridges/vmf_bridge.py",
    "sapient-raw": "bridges/sapient_flex335_bridge.py",
    "stanag4586-raw": "bridges/4586_bridge.py",
    "stanag4609-raw": "bridges/4609_bridge.py",
    "mqtt-raw": "bridges/mqtt_bridge.py",
    "sensorthings-raw": "bridges/sensorthings_bridge.py",
    "cot_layer": "layers/cot_layer.py",
    "tak-bridge": "bridges/tak_bridge.py",
    "sitaware": "bridges/sitaware_bridge.py",
    "nvg_bridge": "bridges/nvg_bridge.py",
    "nvg_layer": "layers/nvg_layer.py",
}


# A service's role is the DIRECTION its data flows, which its folder now
# encodes correctly:
#   bridge   — brings data INTO the fabric  (bridges/, owns an inbound source)
#   protocol — decodes one in-fabric format into another (protocols/, fabric->fabric)
#   layer    — lays data OUT to a C2 app     (layers/, owns the outbound connection)
# The two C2 layers (cot_layer -> TAK, nvg_layer -> SitaWare) live in layers/,
# so the folder is the truth; no per-name override is needed.
_C2_EGRESS: set[str] = set()


def _service_kind(name: str, source: str) -> str:
    """One-word role, by data direction (see note above)."""
    if name in _C2_EGRESS or source.startswith("layers/"):
        return "layer"
    if source.startswith("protocols/"):
        return "protocol"
    if source.startswith("bridges/"):
        return "bridge"
    return "infrastructure"

_SERVICE_REQUIRED_KEYS = {
    "sitaware": ("SITAWARE_URL", "SITAWARE_API_PATH"),
}


# Operational values are editable from the UI.  Secret values are accepted
# and written but are never returned by GET.  Prefix matching is intentionally
# narrow: arbitrary environment variables would become an injection surface.
EDITABLE_EXACT = {
    "PARTNER_NAMESPACE", "NAMESPACE_PREFIX", "NAMESPACE_PREFIX_FILE",
    "DATA_NAMESPACE_PREFIX",
    "EFDI_STEP_CA_URL", "EFDI_STEP_RENEW_CERT_PATH", "EFDI_STEP_RENEW_KEY_PATH",
    "EFDI_STEP_RENEW_ROOT_PATH", "EFDI_STEP_RENEW_RUNTIME_CERT_PATH",
    "EFDI_STEP_RENEW_CHECK_SECONDS", "EFDI_STEP_RENEW_BEFORE_SECONDS",
    "EFDI_ROUTER_CA_CERT_PATH", "EFDI_ROUTER_CA_KEY_PATH", "EFDI_ROUTER_CA_CHAIN_PATH",
    "EFDI_POLICY_SIGNER_CERT_PATH", "EFDI_POLICY_SIGNER_KEY_PATH", "EFDI_STEP_CA_STATE_PATH",
    "ZENOH_LOCAL_ENDPOINT", "ZENOH_LISTEN_PORT", "ZENOH_LOCAL_TCP_PORT",
    "ZENOH_VERIFY_NAME_ON_CONNECT", "ZENOH_PLUGINS_LOADING_ENABLED",
    "ASTERIX_PORT", "ASTERIX_BIND", "ASTERIX_CATEGORIES", "ASTERIX_MULTICAST_GROUP",
    "ASTERIX_MULTICAST_INTERFACE", "ASTERIX_ALLOW_SOURCE",
    "TAK_HOST", "TAK_HOST_FALLBACK", "TAK_PORT", "TAK_TLS", "TAK_CERT", "TAK_KEY", "TAK_CA",
    "SITAWARE_URL", "SITAWARE_URL_FALLBACK", "SITAWARE_API_PATH", "SITAWARE_USER", "SITAWARE_PASS", "SITAWARE_POLL_S",
    "SITAWARE_TLS_VERIFY", "SITAWARE_DISCOVER",
    "SITAWARE_NVG_IMPORT_URL", "SITAWARE_NVG_IMPORT_USER", "SITAWARE_NVG_IMPORT_PASS",
    "SITAWARE_NVG_IMPORT_POLL_S", "SITAWARE_NVG_IMPORT_CA",
    "SITAWARE_HQ_NVG_ENABLE", "SITAWARE_HQ_NVG_BIND", "SITAWARE_HQ_NVG_PORT", "SITAWARE_HQ_NVG_PATH",
    "SITAWARE_HQ_NVG_USER", "SITAWARE_HQ_NVG_PASS", "SITAWARE_HQ_NVG_TLS_CERT", "SITAWARE_HQ_NVG_TLS_KEY",
    "UTM_ANS_API_URL", "UTM_ANS_API_TOKEN", "UTM_ANS_POLL_S", "UTM_ANS_TLS_VERIFY",
    "APRSIS_HOST", "APRSIS_PORT", "APRSIS_FILTER",
    "MQTT_HOST", "MQTT_PORT", "MQTT_TOPIC", "MQTT_USER", "MQTT_PASS", "MQTT_TLS",
    "MQTT_QOS", "MQTT_CLIENT_ID", "MQTT_INPUT_TOPIC",
    "SENSORTHINGS_URL", "SENSORTHINGS_POLL_S", "SENSORTHINGS_PAGE_LIMIT",
    "SENSORTHINGS_TOKEN", "SENSORTHINGS_INPUT_TOPIC",
    "SPARKPLUG_INPUT_TOPIC", "SPARKPLUG_MAX_NODES",
    "DJI_MQTT_HOST", "DJI_MQTT_PORT", "DJI_MQTT_TOPIC", "DJI_MQTT_TLS",
    "DJI_MQTT_USERNAME", "DJI_MQTT_PASSWORD", "DJI_MQTT_CA", "DJI_MQTT_CERT", "DJI_MQTT_KEY", "DJI_MQTT_CLIENT_ID",
    "OPENDRONEID_INPUT_TOPIC", "OPENDRONEID_FRIENDLY_IDS", "OPENDRONEID_STALE_S",
    "STANAG4609_SRT_URL", "STANAG4609_SOURCE",
    "CAP_INPUT_TOPIC", "CAP_ACTIVE_ONLY", "GEOJSON_INPUT_TOPIC", "AIS_NMEA_INPUT_TOPIC",
    "SPECTRUM_INPUT_TOPIC", "SENSOR_HEALTH_INPUT_TOPIC", "MISSION_ROUTE_INPUT_TOPIC",
    "MAVLINK_PORT", "MAVLINK_TCP", "MAVLINK_ZENOH_RAW", "MAVLINK_RAW_PORT", "MAVLINK_RAW_TOPIC",
    "STANAG5516_PORT", "STANAG5516_ZENOH_RAW", "STANAG5516_RAW_PORT", "STANAG5516_RAW_TOPIC",
    "VMF_PORT", "VMF_TCP", "VMF_ZENOH_RAW", "VMF_RAW_PORT", "VMF_RAW_TOPIC",
    "SAPIENT_HOST", "SAPIENT_PORT", "SAPIENT_LISTEN_PORT", "SAPIENT_BIND", "SAPIENT_ALLOW_PEER",
    "SAPIENT_ZENOH_RAW", "SAPIENT_RAW_PORT", "SAPIENT_RAW_TOPIC",
    "STANAG4586_HOST", "STANAG4586_PORT", "STANAG4586_PROFILE", "STANAG4586_ZENOH_RAW", "STANAG4586_RAW_PORT", "STANAG4586_RAW_TOPIC",
    "VMF_RAW_PORT", "STANAG4586_RAW_PORT", "STANAG5516_RAW_PORT", "MAVLINK_RAW_PORT",
}
EDITABLE_PREFIXES = ("CAT10_", "CAT20_", "CAT21_", "CAT34_", "CAT48_", "CAT62_", "NFFI_")
SECRET_MARKERS = ("PASS", "PASSWORD", "TOKEN", "SECRET", "_KEY", "PRIVATE_KEY")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _is_secret(key: str) -> bool:
    return any(marker in key for marker in SECRET_MARKERS)


def _allowed_key(key: str) -> bool:
    return key in EDITABLE_EXACT or key.startswith(EDITABLE_PREFIXES)


def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with ENV_FILE.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if KEY_RE.fullmatch(key):
                    values[key] = value
    except OSError:
        pass
    return values


def _write_env(updates: dict[str, str]) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _ENV_LOCK:
        try:
            original = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            original = []
        seen: set[str] = set()
        output: list[str] = []
        for line in original:
            if "=" not in line or line.lstrip().startswith("#"):
                output.append(line)
                continue
            key = line.split("=", 1)[0]
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                output.append(line)
        for key, value in updates.items():
            if key not in seen:
                output.append(f"{key}={value}")
        fd, temporary = tempfile.mkstemp(prefix=".efdi-env.", dir=str(ENV_FILE.parent), text=True)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(output).rstrip("\n") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, ENV_FILE)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    if "NAMESPACE_PREFIX" in updates and updates["NAMESPACE_PREFIX"]:
        prefix_path = STATE_DIR / "namespace-prefix"
        temporary = prefix_path.with_name(f".{prefix_path.name}.tmp")
        temporary.write_text(updates["NAMESPACE_PREFIX"] + "\n", encoding="utf-8")
        os.replace(temporary, prefix_path)
    if "DATA_NAMESPACE_PREFIX" in updates:
        data_prefix_path = STATE_DIR / "data-topic-prefix"
        temporary = data_prefix_path.with_name(f".{data_prefix_path.name}.tmp")
        temporary.write_text(updates["DATA_NAMESPACE_PREFIX"] + "\n", encoding="utf-8")
        os.replace(temporary, data_prefix_path)


def _pid_record(name: str) -> tuple[int | None, bool]:
    try:
        value = (PID_DIR / f"{name}.pid").read_text().strip()
        pid = int(value)
    except (OSError, ValueError):
        return None, False
    if pid <= 0:
        return None, True
    return pid, True


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _classify_sitaware_hq_nvg_health(health: object) -> tuple[str, dict]:
    """Reduce the local feed health response to non-sensitive runtime details."""
    if not isinstance(health, dict) or health.get("status") != "ok":
        return "health-unavailable", {}
    feed_requests = health.get("feed_requests")
    if not isinstance(feed_requests, dict):
        return "health-unavailable", {}

    tracks = _nonnegative_int(health.get("tracks"))
    successful = _nonnegative_int(feed_requests.get("successful_requests"))
    unauthorized = _nonnegative_int(feed_requests.get("unauthorized_requests"))
    age = _nonnegative_number(feed_requests.get("seconds_since_last_success"))
    last_success = feed_requests.get("last_successful_request")
    last_unauthorized = feed_requests.get("last_unauthorized_request")
    if tracks is None or successful is None or unauthorized is None:
        return "health-unavailable", {}
    if last_success is not None and not isinstance(last_success, str):
        return "health-unavailable", {}
    if last_unauthorized is not None and not isinstance(last_unauthorized, str):
        return "health-unavailable", {}

    details = {
        "tracks": tracks,
        "successful_requests": successful,
        "unauthorized_requests": unauthorized,
        "last_successful_request": last_success,
        "last_unauthorized_request": last_unauthorized,
        "seconds_since_last_success": age,
    }
    unauthorized_is_latest = (
        last_unauthorized is not None
        and (last_success is None or last_unauthorized > last_success)
    )
    if unauthorized_is_latest:
        return "auth-failed", details
    if successful == 0:
        return "waiting-for-client", details
    if age is None:
        return "health-unavailable", {}
    if age > SITAWARE_HQ_NVG_PULL_STALE_SECONDS:
        return "client-stale", details
    return "client-connected", details


def _probe_sitaware_hq_nvg(values: dict[str, str]) -> object | None:
    """Read the authenticated feed health endpoint over host loopback only."""
    try:
        port = int(values.get("SITAWARE_HQ_NVG_PORT", "8088"))
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None

    use_tls = bool(
        values.get("SITAWARE_HQ_NVG_TLS_CERT")
        and values.get("SITAWARE_HQ_NVG_TLS_KEY")
    )
    scheme = "https" if use_tls else "http"
    headers = {"Accept": "application/json"}
    username = values.get("SITAWARE_HQ_NVG_USER", "")
    password = values.get("SITAWARE_HQ_NVG_PASS", "")
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = urllib_request.Request(
        f"{scheme}://127.0.0.1:{port}/healthz",
        headers=headers,
    )
    kwargs: dict[str, object] = {"timeout": 1.0}
    if use_tls:
        # This probe never leaves host loopback; deployment certificates usually
        # identify the externally reachable feed address rather than 127.0.0.1.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["context"] = context
    try:
        with urllib_request.urlopen(request, **kwargs) as response:
            body = response.read(SITAWARE_HQ_NVG_HEALTH_MAX_BYTES + 1)
            if response.status != 200 or len(body) > SITAWARE_HQ_NVG_HEALTH_MAX_BYTES:
                return None
    except (OSError, ValueError):
        return None
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _pid_is_service(pid: int, name: str) -> bool:
    """True only when /proc/{pid} is really THIS service's process.

    A pidfile records a PID, not an identity. When a service crashes, the OS is
    free to hand that same PID to an unrelated process; a bare /proc/{pid}
    existence check would then report the dead service as running (PID reuse).
    start.sh guards against this by matching the process cmdline against the
    service's script — the status endpoint must do the same or the UI drifts
    from the runtime. File-backed services are validated here; non-file
    services (the Docker router, the agent itself) are handled elsewhere.
    """
    source = SERVICE_SOURCES.get(name, "")
    if not source.endswith(".py"):
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return source in cmdline


_PREREQ_TTL_S = 10.0
_prereq_cache: dict = {"at": 0.0, "data": {}}


def _prereqs() -> dict:
    """{service: (ready, hint)} as reported by start.sh's own guards.

    start.sh already knows which services cannot run — svc_ready/svc_hint skip
    them with a reason instead of launching something that would exit at once.
    The UI did not, so it offered Start on an unconfigured service, the process
    died immediately, and the result showed up as CRASHED with nothing to act
    on. Asking start.sh keeps one copy of those rules; re-deriving them here
    would give two that drift.

    Cached briefly: the status endpoint is polled, and this is a subprocess.
    Failures are non-fatal — an unavailable report just means no service is
    reported blocked, which is the pre-existing behaviour.
    """
    now = time.monotonic()
    if now - _prereq_cache["at"] < _PREREQ_TTL_S:
        return _prereq_cache["data"]
    data: dict = {}
    try:
        env = os.environ.copy()
        env["EFDI_NONINTERACTIVE"] = "1"
        result = subprocess.run(
            [str(START_SCRIPT), "--check-all"], cwd=str(ROOT), env=env,
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    data[parts[0]] = (parts[1] == "ready",
                                      parts[2].strip() if len(parts) > 2 else "")
    except (OSError, subprocess.SubprocessError):
        data = {}
    _prereq_cache["at"] = now
    _prereq_cache["data"] = data
    return data


def _blocked_reason(name: str) -> str:
    """Why this service cannot start, or "" if nothing is blocking it."""
    ready, hint = _prereqs().get(name, (True, ""))
    if ready:
        return ""
    # svc_hint's text is only meaningful for a service that is NOT ready; the
    # ready-state hints describe the configured endpoint instead.
    return hint or "required configuration is missing"


def _service_status(name: str) -> dict:
    pid, pid_file_present = _pid_record(name)
    values = _read_env()
    required = _SERVICE_REQUIRED_KEYS.get(name, ())
    if name == "sitaware" and values.get("SITAWARE_DISCOVER", "") == "1":
        required = ()
    if required and not all(values.get(key, "") for key in required):
        return {"name": name, "running": False, "status": "needs-config", "pid": None}
    # A blocked service that is somehow still running is reported as running:
    # the live process is the more useful fact, and stopping it stays possible.
    reason = _blocked_reason(name)
    if reason and not (pid is not None and Path(f"/proc/{pid}").exists()):
        return {"name": name, "running": False, "status": "needs-config",
                "pid": None, "details": {"reason": reason}}
    if name == "zenoh":
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(ROOT / "compose" / "docker-compose.yml"),
                 "ps", "zenoh-router", "--format", "{{.Status}}"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            status = result.stdout.strip() or "stopped"
            running = "up" in status.lower() or "healthy" in status.lower()
            return {"name": name, "running": running, "status": status, "pid": None}
        except (OSError, subprocess.TimeoutExpired):
            return {"name": name, "running": False, "status": "unavailable", "pid": None}
    pid_alive = pid is not None and Path(f"/proc/{pid}").exists() and _pid_is_service(pid, name)
    if pid_alive:
        if name == "sitaware-hq-nvg":
            status, details = _classify_sitaware_hq_nvg_health(
                _probe_sitaware_hq_nvg(values)
            )
            return {
                "name": name,
                "running": True,
                "status": status,
                "pid": pid,
                "details": details,
            }
        return {"name": name, "running": True, "status": "running", "pid": pid}
    if pid_file_present:
        return {"name": name, "running": False, "status": "crashed", "pid": pid}
    return {"name": name, "running": False, "status": "stopped", "pid": None}


def _prometheus_metrics() -> str:
    """Prometheus text-format snapshot of host-managed service state.

    Reuses _service_status so the scrape sees exactly what Runtime Control does.
    Served behind the same bearer token as the rest of the control API — a
    scraper configures `authorization: Bearer <EFDI_CONTROL_TOKEN>`.
    """
    lines = [
        "# HELP efdi_up Control agent liveness.",
        "# TYPE efdi_up gauge",
        "efdi_up 1",
        "# HELP efdi_service_up Service running state (1=running, 0=not).",
        "# TYPE efdi_service_up gauge",
    ]
    statuses = [(name, _service_status(name)) for name, _, _ in SERVICE_SPECS]
    for name, status in statuses:
        kind = _service_kind(name, SERVICE_SOURCES.get(name, ""))
        up = 1 if status.get("running") else 0
        lines.append(f'efdi_service_up{{service="{name}",kind="{kind}"}} {up}')
    lines.append("# HELP efdi_service_tracks Live tracks reported by the service.")
    lines.append("# TYPE efdi_service_tracks gauge")
    for name, status in statuses:
        tracks = (status.get("details") or {}).get("tracks")
        if isinstance(tracks, (int, float)) and not isinstance(tracks, bool):
            lines.append(f'efdi_service_tracks{{service="{name}"}} {tracks}')
    return "\n".join(lines) + "\n"


def _selected() -> list[str]:
    try:
        lines = LAUNCHER_STATE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    raw = ""
    for line in lines:
        if line.startswith("SELECTED_SERVICES="):
            raw = line.split("=", 1)[1]
            break
    return [item for item in raw.split(",") if item in SERVICE_NAMES]


def _write_selected(selected: list[str]) -> None:
    selected = [name for name in selected if name in SERVICE_NAMES]
    try:
        lines = LAUNCHER_STATE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    seen = False
    output: list[str] = []
    for line in lines:
        if line.startswith("SELECTED_SERVICES="):
            output.append("SELECTED_SERVICES=" + ",".join(selected))
            seen = True
        else:
            output.append(line)
    if not seen:
        output.append("SELECTED_SERVICES=" + ",".join(selected))
    LAUNCHER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = LAUNCHER_STATE_FILE.with_name(f".{LAUNCHER_STATE_FILE.name}.tmp")
    temporary.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, LAUNCHER_STATE_FILE)


def _run_script(script: Path, args: list[str]) -> dict:
    env = os.environ.copy()
    env["EFDI_NONINTERACTIVE"] = "1"
    try:
        result = subprocess.run(
            [str(script), *args], cwd=str(ROOT), env=env, input="\n",
            capture_output=True, text=True, timeout=45, check=False,
        )
        output = (result.stdout + result.stderr).strip()
        return {"ok": result.returncode == 0, "returncode": result.returncode, "output": output[-8000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "output": "control command timed out"}
    except OSError as exc:
        return {"ok": False, "returncode": 127, "output": str(exc)}


def _action(name: str, action: str) -> dict:
    if name not in SERVICE_NAMES:
        return {"ok": False, "returncode": 400, "output": "unknown service"}
    # Stopping the router or the control agent severs the very connection this
    # request arrived on — you would turn yourself off and lose all control.
    # The UI hides the button; refuse it here too so a direct API call cannot.
    if action == "stop" and _service_kind(name, SERVICE_SOURCES.get(name, "")) == "infrastructure":
        return {"ok": False, "returncode": 409,
                "output": "infrastructure services cannot be stopped from the control API"}
    if name == "admin-control" and action == "restart":
        return {"ok": False, "returncode": 409, "output": "restart the control agent from the host launcher"}
    if action in ("start", "restart"):
        # Refuse rather than spawn something that exits immediately and then
        # reports CRASHED with no explanation. The reason names the missing key.
        reason = _blocked_reason(name)
        if reason:
            return {"ok": False, "returncode": 409,
                    "output": f"{name} is not configured: {reason}"}
    if action == "start":
        result = _run_script(START_SCRIPT, ["--service", name])
        if result["ok"] and _service_status(name)["running"]:
            current = _selected()
            if name not in current:
                _write_selected(current + [name])
    elif action == "stop":
        result = _run_script(STOP_SCRIPT, [name])
        if result["ok"] or not _service_status(name)["running"]:
            current = [item for item in _selected() if item != name]
            _write_selected(current)
    elif action == "restart":
        stopped = _run_script(STOP_SCRIPT, [name])
        if not stopped["ok"] and _service_status(name)["running"]:
            return stopped
        result = _run_script(START_SCRIPT, ["--service", name])
        if result["ok"] and _service_status(name)["running"]:
            current = _selected()
            if name not in current:
                _write_selected(current + [name])
        result["output"] = (stopped.get("output", "") + "\n" + result.get("output", "")).strip()[-8000:]
    else:
        return {"ok": False, "returncode": 400, "output": "action must be start, stop, or restart"}
    return result


def _tail_lines(path: Path, limit: int = 200) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            handle.seek(max(0, end - LOG_TAIL_BYTES))
            if handle.tell() > 0:
                handle.readline()
            return handle.read().decode("utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []


_ZENOH_VALIDATE_SCRIPT = r"""
set -eu
base="$(mktemp /tmp/efdi-zenoh-check.XXXXXX)"
candidate="${base}.json5"
mv "$base" "$candidate"
trap 'rm -f "$candidate"' EXIT
cat > "$candidate"
/zenohd -c "$candidate" \
  --cfg=listen/endpoints:[] \
  --cfg=connect/endpoints:[] \
  --cfg=scouting/multicast/enabled:false \
  --cfg=plugins_loading/enabled:false &
validator_pid=$!
sleep 2
if kill -0 "$validator_pid" 2>/dev/null; then
  kill "$validator_pid"
  wait "$validator_pid" || true
  exit 0
fi
wait "$validator_pid"
"""


def _validate_router_config(config: str) -> dict:
    """Run the pinned Zenoh binary as a disconnected preflight validator.

    The command, image target, and overrides are fixed here. The candidate is
    supplied only on stdin, so a WebUI value can never become a shell argument.
    Listeners, uplinks, scouting, and plugins are disabled for the probe; a
    successful two-second startup proves Zenoh accepted the complete schema
    without creating another fabric participant.
    """
    encoded = config.encode("utf-8")
    if not encoded or len(encoded) > CONFIG_VALIDATE_MAX_BYTES:
        return {
            "ok": False,
            "returncode": 400,
            "output": f"candidate must be 1-{CONFIG_VALIDATE_MAX_BYTES} UTF-8 bytes",
        }
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", SHELL_CONTAINER, "/bin/sh", "-c", _ZENOH_VALIDATE_SCRIPT],
            input=encoded,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "output": "Zenoh config preflight timed out"}
    except OSError as exc:
        return {"ok": False, "returncode": 127, "output": str(exc)}
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if result.returncode == 0:
        output = "Zenoh 1.9.0 accepted the candidate configuration"
    else:
        output = output[-8000:]
    return {"ok": result.returncode == 0, "returncode": result.returncode, "output": output}


def _pki_status() -> dict:
    configured = bool(
        ROUTER_CA_CERT_PATH
        and ROUTER_CA_KEY_PATH
        and ROUTER_CA_CERT_PATH.is_file()
        and ROUTER_CA_KEY_PATH.is_file()
    )
    if not configured:
        return {
            "configured": False,
            "available": False,
            "issuer": None,
            "expires_at": None,
            "path_length": None,
            "step_ca_available": False,
        }
    try:
        subject = subprocess.run(
            ["openssl", "x509", "-in", str(ROUTER_CA_CERT_PATH), "-noout", "-subject", "-nameopt", "RFC2253"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().removeprefix("subject=")
        expires_at = subprocess.run(
            ["openssl", "x509", "-in", str(ROUTER_CA_CERT_PATH), "-noout", "-enddate"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().removeprefix("notAfter=")
        cert_public = subprocess.run(
            ["openssl", "x509", "-in", str(ROUTER_CA_CERT_PATH), "-pubkey", "-noout"],
            capture_output=True, timeout=5, check=True,
        ).stdout
        key_public = subprocess.run(
            ["openssl", "pkey", "-in", str(ROUTER_CA_KEY_PATH), "-pubout"],
            capture_output=True, timeout=5, check=True,
        ).stdout
        certificate_text = subprocess.run(
            ["openssl", "x509", "-in", str(ROUTER_CA_CERT_PATH), "-noout", "-text"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        path_length_match = re.search(r"CA:TRUE\s*,\s*pathlen:(\d+)", certificate_text)
        path_length = int(path_length_match.group(1)) if path_length_match else None
        return {
            "configured": True,
            "available": cert_public == key_public,
            "issuer": subject,
            "expires_at": expires_at,
            "path_length": path_length,
            "step_ca_available": bool(
                STEP_CA_STATE_PATH
                and (STEP_CA_STATE_PATH / "config" / "ca.json").is_file()
                and (STEP_CA_STATE_PATH / "certs" / "intermediate_ca.crt").is_file()
                and (STEP_CA_STATE_PATH / "secrets" / "intermediate_ca_key").is_file()
            ),
        }
    except (OSError, subprocess.SubprocessError):
        return {
            "configured": True,
            "available": False,
            "issuer": None,
            "expires_at": None,
            "path_length": None,
            "step_ca_available": False,
        }


def _sign_transport_with_step_ca(csr_path: Path, cert_path: Path) -> dict | None:
    if not STEP_CA_STATE_PATH or not (STEP_CA_STATE_PATH / "config" / "ca.json").is_file():
        return None
    state = STEP_CA_STATE_PATH.resolve()
    work = csr_path.parent.resolve()
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--entrypoint", "step",
                "-e", "STEPPATH=/home/step",
                "-v", f"{state}:/home/step",
                "-v", f"{work}:/work",
                _STEP_CA_IMAGE,
                "ca", "sign", "--offline", "--force",
                "--password-file", "/home/step/secrets/password",
                "--provisioner-password-file", "/home/step/secrets/provisioner-password",
                "--not-after", "24h",
                "/work/request.pem", "/work/certificate.pem",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not cert_path.is_file():
            return {"ok": False, "output": "online leaf issuer rejected the transport CSR"}
        intermediate = (state / "certs" / "intermediate_ca.crt").read_text(encoding="utf-8")
        router_chain = (ROUTER_CA_CHAIN_PATH or ROUTER_CA_CERT_PATH).read_text(encoding="utf-8")
        certificate = cert_path.read_text(encoding="utf-8")
        serial = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-serial"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().removeprefix("serial=")
        return {
            "ok": True,
            "certificate": certificate,
            "chain": intermediate.rstrip() + "\n" + router_chain,
            "serial": serial,
            "issuer": "step-ca",
        }
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "output": "online leaf signing operation failed"}


def _sign_csr(
    csr_pem: str,
    common_name: str,
    profile: str,
    path_length: int,
    days: int,
    identity_uri: str | None = None,
) -> dict:
    status = _pki_status()
    if not status["available"]:
        return {"ok": False, "output": "router CA is unavailable or its certificate/key do not match"}
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", common_name):
        return {"ok": False, "output": "common name contains unsupported characters"}
    if profile not in {"router-ca", "transport", "policy-signer"}:
        return {"ok": False, "output": "profile must be router-ca, transport, or policy-signer"}
    if not 0 <= path_length <= 8 or not 1 <= days <= 825:
        return {"ok": False, "output": "path length or lifetime is outside policy"}
    issuer_path_length = status.get("path_length")
    if profile == "router-ca" and (
        issuer_path_length is None or path_length >= issuer_path_length
    ):
        return {
            "ok": False,
            "output": "child CA delegation depth must be lower than the issuer certificate path length",
        }
    if len(csr_pem.encode("utf-8")) > 64 * 1024 or "BEGIN CERTIFICATE REQUEST" not in csr_pem:
        return {"ok": False, "output": "invalid or oversized CSR"}
    if identity_uri is None:
        identity_uri = f"spiffe://efdi.local/router/{quote(common_name, safe='')}"
    if not re.fullmatch(r"spiffe://[a-z0-9.-]{1,253}/router/[A-Za-z0-9._~%/-]{1,512}", identity_uri):
        return {"ok": False, "output": "identity URI is outside the router SPIFFE profile"}

    with tempfile.TemporaryDirectory(prefix="efdi-pki-") as directory:
        csr_path = Path(directory) / "request.pem"
        cert_path = Path(directory) / "certificate.pem"
        ext_path = Path(directory) / "extensions.cnf"
        csr_path.write_text(csr_pem, encoding="utf-8")
        try:
            verify = subprocess.run(
                ["openssl", "req", "-in", str(csr_path), "-noout", "-verify", "-subject", "-nameopt", "RFC2253", "-text"],
                capture_output=True, text=True, timeout=8, check=False,
            )
            if verify.returncode != 0:
                return {"ok": False, "output": "CSR proof-of-possession verification failed"}
            first_line = next((line.strip() for line in verify.stdout.splitlines() if line.strip().startswith("subject=")), "")
            if first_line != f"subject=CN={common_name}":
                return {"ok": False, "output": "CSR subject must contain only the invited common name"}
            if "ASN1 OID: prime256v1" not in verify.stdout and "NIST CURVE: P-256" not in verify.stdout:
                return {"ok": False, "output": "CSR must use an ECDSA P-256 key"}

            if profile == "transport" and STEP_CA_STATE_PATH:
                san = subprocess.run(
                    ["openssl", "req", "-in", str(csr_path), "-noout", "-ext", "subjectAltName"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if san.returncode != 0 or f"URI:{identity_uri}" not in san.stdout:
                    return {"ok": False, "output": "transport CSR must contain the invited SPIFFE URI SAN"}
                step_result = _sign_transport_with_step_ca(csr_path, cert_path)
                if step_result is not None:
                    return step_result

            if profile == "router-ca":
                extensions = (
                    f"basicConstraints=critical,CA:TRUE,pathlen:{path_length}\n"
                    "keyUsage=critical,digitalSignature,keyCertSign,cRLSign\n"
                    f"subjectAltName=URI:{identity_uri}\n"
                    "subjectKeyIdentifier=hash\n"
                    "authorityKeyIdentifier=keyid,issuer\n"
                )
            elif profile == "transport":
                extensions = (
                    "basicConstraints=critical,CA:FALSE\n"
                    "keyUsage=critical,digitalSignature\n"
                    "extendedKeyUsage=serverAuth,clientAuth\n"
                    f"subjectAltName=URI:{identity_uri}\n"
                    "subjectKeyIdentifier=hash\n"
                    "authorityKeyIdentifier=keyid,issuer\n"
                )
            else:
                extensions = (
                    "basicConstraints=critical,CA:FALSE\n"
                    "keyUsage=critical,digitalSignature\n"
                    "extendedKeyUsage=1.3.6.1.4.1.55555.1.1\n"
                    f"subjectAltName=URI:{identity_uri}\n"
                    "subjectKeyIdentifier=hash\n"
                    "authorityKeyIdentifier=keyid,issuer\n"
                )
            ext_path.write_text(extensions, encoding="utf-8")
            sign = subprocess.run(
                [
                    "openssl", "x509", "-req", "-in", str(csr_path),
                    "-CA", str(ROUTER_CA_CERT_PATH), "-CAkey", str(ROUTER_CA_KEY_PATH),
                    "-set_serial", "0x" + secrets.token_hex(19), "-days", str(days), "-sha256",
                    "-extfile", str(ext_path), "-out", str(cert_path),
                ],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if sign.returncode != 0:
                return {"ok": False, "output": "router CA rejected the CSR"}
            certificate = cert_path.read_text(encoding="utf-8")
            chain_path = ROUTER_CA_CHAIN_PATH or ROUTER_CA_CERT_PATH
            chain = chain_path.read_text(encoding="utf-8")
            serial = subprocess.run(
                ["openssl", "x509", "-in", str(cert_path), "-noout", "-serial"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip().removeprefix("serial=")
            return {"ok": True, "certificate": certificate, "chain": chain, "serial": serial}
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "output": "router CA signing operation failed"}


class Handler(BaseHTTPRequestHandler):
    server_version = "EFDIControl/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _authorized(self) -> bool:
        if not CONTROL_TOKEN:
            return False
        header = self.headers.get("Authorization", "")
        # Constant-time compare, matching the shell listener's check below: a
        # plain `==` leaks how many leading bytes of the bearer token matched.
        return hmac.compare_digest(header, f"Bearer {CONTROL_TOKEN}")

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The auto-refreshing UI routinely hangs up mid-response. The server
            # survives it, but if the error reaches socketserver.handle_error it
            # dumps a full traceback per disconnect — noise that would bury the
            # real cause the next time the agent actually dies.
            self.close_connection = True

    def _text(self, status: int, body: str,
              content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > 128 * 1024:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"detail": "control authorization failed"})
            return
        path = urlparse(self.path).path
        if path == "/v1/catalog":
            self._json(200, {"services": [
                {
                    "name": name,
                    "group": group,
                    "description": description,
                    "source": SERVICE_SOURCES.get(name, ""),
                    "kind": _service_kind(name, SERVICE_SOURCES.get(name, "")),
                }
                for name, group, description in SERVICE_SPECS
            ]})
            return
        if path == "/v1/runtime":
            values = _read_env()
            config = {
                key: ({"configured": bool(value)} if _is_secret(key) else value)
                for key, value in values.items() if _allowed_key(key)
            }
            self._json(200, {
                "services": [_service_status(name) for name, _, _ in SERVICE_SPECS],
                "selected_services": _selected(),
                "config": config,
                "editable_keys": sorted(EDITABLE_EXACT),
                "env_file": str(ENV_FILE),
                "control_port": CONTROL_PORT,
            })
            return
        if path == "/v1/selection":
            self._json(200, {"selected_services": _selected()})
            return
        if path == "/v1/pki/status":
            self._json(200, _pki_status())
            return
        if path == "/metrics":
            self._text(200, _prometheus_metrics(),
                       "text/plain; version=0.0.4; charset=utf-8")
            return
        match = re.fullmatch(r"/v1/logs/([a-z0-9_-]+)", path)
        if match and match.group(1) in SERVICE_NAMES:
            lines = _tail_lines(LOG_DIR / f"{match.group(1)}.log")
            self._json(200, {"name": match.group(1), "lines": lines})
            return
        self._json(404, {"detail": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"detail": "control authorization failed"})
            return
        path = urlparse(self.path).path
        if path == "/v1/selection":
            try:
                body = self._body()
                selected = body.get("selected_services")
                if not isinstance(selected, list):
                    raise ValueError("selected_services must be a list")
                cleaned = []
                for item in selected:
                    if not isinstance(item, str) or item not in SERVICE_NAMES:
                        raise ValueError(f"invalid selected service: {item}")
                    if item not in cleaned:
                        cleaned.append(item)
                _write_selected(cleaned)
                self._json(200, {"ok": True, "selected_services": _selected()})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"detail": str(exc)})
            return
        if path != "/v1/config":
            self._json(404, {"detail": "not found"})
            return
        try:
            body = self._body()
            incoming = body.get("values")
            if not isinstance(incoming, dict) or len(incoming) > 200:
                raise ValueError("values must be an object with at most 200 keys")
            updates: dict[str, str] = {}
            for key, value in incoming.items():
                if not isinstance(key, str) or not KEY_RE.fullmatch(key) or not _allowed_key(key):
                    raise ValueError(f"unsupported configuration key: {key}")
                if value is None:
                    value = ""
                if isinstance(value, bool):
                    value = "1" if value else "0"
                elif isinstance(value, (int, float)):
                    value = str(value)
                elif not isinstance(value, str):
                    raise ValueError(f"configuration value for {key} must be scalar")
                if "\r" in value or "\n" in value:
                    raise ValueError(f"configuration value for {key} contains a newline")
                if key == "NAMESPACE_PREFIX" and not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
                    raise ValueError("NAMESPACE_PREFIX contains unsupported characters")
                updates[key] = value
            _write_env(updates)
            self._json(200, {"ok": True, "updated": sorted(updates), "secret_keys": sorted(k for k in updates if _is_secret(k))})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"detail": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"detail": "control authorization failed"})
            return
        path = urlparse(self.path).path
        if path == "/v1/router/validate-config":
            try:
                body = self._body()
                config = body.get("config")
                if not isinstance(config, str):
                    raise ValueError("config must be a string")
                result = _validate_router_config(config)
                # A rejected candidate is a successful control-agent request.
                # Keep HTTP 200 here so the admin API can distinguish a Zenoh
                # validation result from an unavailable host control agent.
                self._json(200, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"detail": str(exc)})
            return
        if path == "/v1/pki/sign-csr":
            try:
                body = self._body()
                csr = body.get("csr")
                common_name = body.get("common_name")
                profile = body.get("profile")
                path_length = body.get("path_length", 0)
                days = body.get("days", 90)
                identity_uri = body.get("identity_uri")
                if not isinstance(csr, str) or not isinstance(common_name, str) or not isinstance(profile, str):
                    raise ValueError("csr, common_name, and profile must be strings")
                if not isinstance(path_length, int) or isinstance(path_length, bool):
                    raise ValueError("path_length must be an integer")
                if not isinstance(days, int) or isinstance(days, bool):
                    raise ValueError("days must be an integer")
                if identity_uri is not None and not isinstance(identity_uri, str):
                    raise ValueError("identity_uri must be a string")
                result = _sign_csr(
                    csr,
                    common_name,
                    profile,
                    path_length,
                    days,
                    identity_uri,
                )
                self._json(200, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"detail": str(exc)})
            return
        match = re.fullmatch(r"/v1/services/([a-z0-9_-]+)/(start|stop|restart)", path)
        if not match:
            self._json(404, {"detail": "not found"})
            return
        name, action = match.groups()
        result = _action(unquote(name), action)
        self._json(200 if result["ok"] else 409, {**result, "service": name, "action": action,
                                                   "status": _service_status(name)})


def _read_shell_line(connection) -> bytes | None:
    data = bytearray()
    while len(data) < 4096:
        chunk = connection.recv(1)
        if not chunk:
            return None
        if chunk == b"\n":
            return bytes(data)
        data.extend(chunk)
    return None


class ShellHandler(socketserver.BaseRequestHandler):
    """Fixed-target, fixed-command break-glass shell for the admin UI.

    This is deliberately separate from the Docker API. The web container can
    restart/list containers through the limited Docker lifecycle proxy, while this
    host-side helper is the only component allowed to create the one approved
    `/bin/sh` exec in the configured Zenoh router.
    """

    def handle(self) -> None:
        if not CONTROL_TOKEN:
            return
        line = _read_shell_line(self.request)
        expected = f"EFDI-SHELL/1 {CONTROL_TOKEN}".encode()
        if line is None or not hmac.compare_digest(line, expected):
            return
        self.request.sendall(b"OK\n")
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                ["docker", "exec", "-i", "-t", SHELL_CONTAINER, "/bin/sh"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                bufsize=0,
            )
        except OSError:
            os.close(master_fd)
            os.close(slave_fd)
            return
        os.close(slave_fd)

        def forward_input() -> None:
            try:
                while process.poll() is None:
                    data = self.request.recv(4096)
                    if not data:
                        break
                    os.write(master_fd, data)
            except (BrokenPipeError, OSError):
                pass

        def forward_output() -> None:
            try:
                while True:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    self.request.sendall(data)
            except (BrokenPipeError, OSError):
                pass

        input_thread = threading.Thread(target=forward_input, daemon=True)
        output_thread = threading.Thread(target=forward_output, daemon=True)
        input_thread.start()
        output_thread.start()
        input_thread.join(SHELL_MAX_SECONDS)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        output_thread.join(5)
        try:
            os.close(master_fd)
        except OSError:
            pass


class ShellServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), Handler)
    shell_server = None
    shell_thread = None
    if CONTROL_TOKEN:
        shell_server = ShellServer((SHELL_HOST, SHELL_PORT), ShellHandler)
        shell_thread = threading.Thread(target=shell_server.serve_forever, daemon=True)
        shell_thread.start()
        print(f"[admin-control] shell helper listening on {SHELL_HOST}:{SHELL_PORT}", flush=True)
    else:
        print("[admin-control] EFDI_CONTROL_TOKEN is unset — control and shell endpoints disabled", flush=True)
    print(f"[admin-control] listening on http://{CONTROL_HOST}:{CONTROL_PORT}", flush=True)

    # Make every exit observable. The agent was dying silently — an external
    # SIGTERM leaves no traceback, so the log could not tell a kill from a crash,
    # which is why the outage kept recurring without explanation. Now a signal
    # logs its name and a serve-loop crash logs its exception; either way there
    # is a breadcrumb, and the control port is closed cleanly instead of leaving
    # a half-open socket behind a stale pidfile.
    stop = threading.Event()

    def _handle_signal(signum: int, _frame) -> None:
        print(f"[admin-control] received {signal.Signals(signum).name} — shutting down",
              flush=True)
        stop.set()

    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, _handle_signal)

    def _serve() -> None:
        try:
            server.serve_forever()
        except Exception as exc:  # noqa: BLE001 — a crash here must be logged, not silent
            print(f"[admin-control] control server exited abnormally: {exc!r}", flush=True)
        finally:
            stop.set()

    control_thread = threading.Thread(target=_serve, daemon=True)
    control_thread.start()

    try:
        stop.wait()
    finally:
        print("[admin-control] stopped", flush=True)
        server.shutdown()
        server.server_close()
        if shell_server is not None:
            shell_server.shutdown()
            shell_server.server_close()


if __name__ == "__main__":
    main()
