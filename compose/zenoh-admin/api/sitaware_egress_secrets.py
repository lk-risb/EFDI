"""Mode-0600 SitaWare egress feed credentials — same pattern as link_secrets.py
and sitaware_target_secrets.py (that one covers ingress).

The sitaware_egress_targets DB table (models.py) holds non-secret desired-state
(name/bind/port/path/enabled) only; the actual feed username/password for each
target lives here, in a file admin_control.py can also read on the host side
(both this container and the host process share POD_STATE_DIR via bind mount)."""

import json
import os
import stat
import tempfile
import threading
from pathlib import Path

_PATH = Path(os.environ.get(
    "EFDI_SITAWARE_EGRESS_SECRET_PATH", "/zenoh-config/sitaware-egress-secrets.json"))
_LOCK = threading.RLock()


def _read() -> dict:
    try:
        value = json.loads(_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(value: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sitaware-egress-secrets.", dir=str(_PATH.parent), text=True)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def set_target_secret(target_id: str, username: str, password: str) -> None:
    with _LOCK:
        value = _read()
        value[target_id] = {"username": username, "password": password}
        _write(value)


def get_target_secret(target_id: str) -> dict | None:
    with _LOCK:
        item = _read().get(target_id)
        return item.copy() if isinstance(item, dict) else None


def remove_target_secret(target_id: str) -> None:
    with _LOCK:
        value = _read()
        value.pop(target_id, None)
        _write(value)


def has_target_secret(target_id: str) -> bool:
    with _LOCK:
        return target_id in _read()
