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
import socketserver
import ssl
import subprocess
import tempfile
import threading
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
    ("aisstream", "Open-data bridges", "AISstream vessels"),
    ("aprs", "Open-data bridges", "APRS-IS"),
    ("openmeteo", "Open-data bridges", "Open-Meteo weather"),
    ("meteolt", "Open-data bridges", "meteo.lt weather"),
    ("dronuradaras", "Sensor bridges", "dronuradaras.lt sensors"),
    ("dji-cloud", "Sensor bridges", "DJI Cloud MQTT"),
    ("utm-ans", "Open-data bridges", "Oro navigacija UTM"),
    ("asterix-udp", "Sensor bridges", "Mixed ASTERIX UDP ingress"),
    ("track-fusion", "Sensor bridges", "Track correlation"),
    ("asterix-cat10", "Protocols", "ASTERIX CAT-010"),
    ("asterix-cat20", "Protocols", "ASTERIX CAT-020"),
    ("asterix-cat21", "Protocols", "ASTERIX CAT-021"),
    ("asterix-cat34", "Protocols", "ASTERIX CAT-034"),
    ("asterix-cat48", "Protocols", "ASTERIX CAT-048"),
    ("asterix-cat62", "Protocols", "ASTERIX CAT-062"),
    ("link16", "Protocols", "Link-16 JREAP-C"),
    ("mavlink", "Protocols", "MAVLink / Remote ID"),
    ("opendroneid", "Protocols", "OpenDroneID translator"),
    ("vmf", "Protocols", "VMF MIL-STD-47001C"),
    ("nffi", "Protocols", "NFFI / STANAG 4677"),
    ("sapient", "Protocols", "SAPIENT / FLEX 335"),
    ("stanag4586", "Protocols", "STANAG 4586"),
    ("cap", "Protocols", "CAP 1.2 alerts"),
    ("geojson", "Protocols", "GeoJSON / OGC Features"),
    ("ais-nmea", "Protocols", "AIS NMEA"),
    ("spectrum", "Protocols", "Spectrum observations"),
    ("sensor-health", "Protocols", "Sensor health"),
    ("mission-route", "Protocols", "Mission routes"),
    ("mavlink-raw", "Raw ingress", "MAVLink raw socket"),
    ("link16-raw", "Raw ingress", "Link-16 raw socket"),
    ("vmf-raw", "Raw ingress", "VMF raw socket"),
    ("sapient-raw", "Raw ingress", "SAPIENT raw socket"),
    ("stanag4586-raw", "Raw ingress", "STANAG 4586 raw socket"),
    ("cot-udp", "C2 outputs", "CoT multicast"),
    ("cot-udp-tak", "C2 outputs", "CoT UDP client"),
    ("cot-bridge", "C2 outputs", "TAK Server CoT TCP"),
    ("sitaware", "C2 outputs", "SitaWare HQ REST input"),
    ("sitaware-hq-nvg", "C2 outputs", "SitaWare HQ NVG feed"),
]
SERVICE_NAMES = {name for name, _, _ in SERVICE_SPECS}

_SERVICE_REQUIRED_KEYS = {
    "aisstream": ("AISSTREAM_KEY",),
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
    "TAK_UDP_HOST", "TAK_UDP_HOST_FALLBACK", "TAK_UDP_PORT",
    "SITAWARE_URL", "SITAWARE_URL_FALLBACK", "SITAWARE_API_PATH", "SITAWARE_USER", "SITAWARE_PASS", "SITAWARE_POLL_S",
    "SITAWARE_TLS_VERIFY", "SITAWARE_DISCOVER",
    "SITAWARE_HQ_NVG_ENABLE", "SITAWARE_HQ_NVG_BIND", "SITAWARE_HQ_NVG_PORT", "SITAWARE_HQ_NVG_PATH",
    "SITAWARE_HQ_NVG_USER", "SITAWARE_HQ_NVG_PASS",
    "SITAWARE_HQ_NVG_TLS_CERT", "SITAWARE_HQ_NVG_TLS_KEY", "SITAWARE_HQ_NVG_ALLOW_ANONYMOUS",
    "SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP", "SITAWARE_HQ_NVG_STALE_S", "SITAWARE_HQ_NVG_MAX_TRACKS",
    "UTM_ANS_API_URL", "UTM_ANS_API_TOKEN", "UTM_ANS_POLL_S", "UTM_ANS_TLS_VERIFY",
    "AISSTREAM_KEY", "APRSIS_HOST", "APRSIS_PORT", "APRSIS_FILTER",
    "DJI_MQTT_HOST", "DJI_MQTT_PORT", "DJI_MQTT_TOPIC", "DJI_MQTT_TLS",
    "DJI_MQTT_USERNAME", "DJI_MQTT_PASSWORD", "DJI_MQTT_CA", "DJI_MQTT_CERT", "DJI_MQTT_KEY", "DJI_MQTT_CLIENT_ID",
    "OPENDRONEID_INPUT_TOPIC", "OPENDRONEID_FRIENDLY_IDS", "OPENDRONEID_STALE_S",
    "CAP_INPUT_TOPIC", "CAP_ACTIVE_ONLY", "GEOJSON_INPUT_TOPIC", "AIS_NMEA_INPUT_TOPIC",
    "SPECTRUM_INPUT_TOPIC", "SENSOR_HEALTH_INPUT_TOPIC", "MISSION_ROUTE_INPUT_TOPIC",
    "MAVLINK_PORT", "MAVLINK_TCP", "MAVLINK_ZENOH_RAW", "MAVLINK_RAW_PORT", "MAVLINK_RAW_TOPIC",
    "LINK16_PORT", "LINK16_ZENOH_RAW", "LINK16_RAW_PORT", "LINK16_RAW_TOPIC",
    "VMF_PORT", "VMF_TCP", "VMF_ZENOH_RAW", "VMF_RAW_PORT", "VMF_RAW_TOPIC",
    "SAPIENT_HOST", "SAPIENT_PORT", "SAPIENT_LISTEN_PORT", "SAPIENT_BIND", "SAPIENT_ALLOW_PEER",
    "SAPIENT_ZENOH_RAW", "SAPIENT_RAW_PORT", "SAPIENT_RAW_TOPIC",
    "STANAG4586_HOST", "STANAG4586_PORT", "STANAG4586_ZENOH_RAW", "STANAG4586_RAW_PORT", "STANAG4586_RAW_TOPIC",
    "VMF_RAW_PORT", "STANAG4586_RAW_PORT", "LINK16_RAW_PORT", "MAVLINK_RAW_PORT",
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


def _service_status(name: str) -> dict:
    pid, pid_file_present = _pid_record(name)
    values = _read_env()
    required = _SERVICE_REQUIRED_KEYS.get(name, ())
    if name == "sitaware" and values.get("SITAWARE_DISCOVER", "") == "1":
        required = ()
    if required and not all(values.get(key, "") for key in required):
        return {"name": name, "running": False, "status": "needs-config", "pid": None}
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
    pid_alive = pid is not None and Path(f"/proc/{pid}").exists()
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
    if name == "admin-control" and action == "restart":
        return {"ok": False, "returncode": 409, "output": "restart the control agent from the host launcher"}
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
        return header == f"Bearer {CONTROL_TOKEN}"

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                {"name": name, "group": group, "description": description}
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
        match = re.fullmatch(r"/v1/logs/([a-z0-9-]+)", path)
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
        match = re.fullmatch(r"/v1/services/([a-z0-9-]+)/(start|stop|restart)", path)
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if shell_server is not None:
            shell_server.shutdown()
            shell_server.server_close()


if __name__ == "__main__":
    main()
