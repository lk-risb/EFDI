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

import json
import os
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("POD_STATE_DIR", str(ROOT / "compose" / "state")))
ENV_FILE = Path(os.environ.get("EFDI_ENV_FILE", str(ROOT / "compose" / ".env")))
START_SCRIPT = ROOT / "start.sh"
STOP_SCRIPT = ROOT / "stop.sh"
CONTROL_HOST = os.environ.get("EFDI_CONTROL_BIND", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("EFDI_CONTROL_PORT", "18896"))
CONTROL_TOKEN = os.environ.get("EFDI_CONTROL_TOKEN", "")
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


class Handler(BaseHTTPRequestHandler):
    server_version = "EFDIControl/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _authorized(self) -> bool:
        if not CONTROL_TOKEN:
            return True
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
        match = re.fullmatch(r"/v1/logs/([a-z0-9-]+)", path)
        if match and match.group(1) in SERVICE_NAMES:
            try:
                lines = (LOG_DIR / f"{match.group(1)}.log").read_text(errors="replace").splitlines()
                self._json(200, {"name": match.group(1), "lines": lines[-200:]})
            except OSError:
                self._json(200, {"name": match.group(1), "lines": []})
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
        match = re.fullmatch(r"/v1/services/([a-z0-9-]+)/(start|stop|restart)", path)
        if not match:
            self._json(404, {"detail": "not found"})
            return
        name, action = match.groups()
        result = _action(unquote(name), action)
        self._json(200 if result["ok"] else 409, {**result, "service": name, "action": action,
                                                   "status": _service_status(name)})


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), Handler)
    print(f"[admin-control] listening on http://{CONTROL_HOST}:{CONTROL_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
