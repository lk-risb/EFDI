"""Mode-0600 runtime link credentials; database retains hashes only."""

import json
import os
import stat
import tempfile
import threading
import secrets
from pathlib import Path


_PATH = Path(os.environ.get("EFDI_LINK_SECRET_PATH", "/zenoh-config/link-credentials.json"))
_LOCK = threading.RLock()


def _read() -> dict:
    try:
        value = json.loads(_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(value: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".link-credentials.", dir=str(_PATH.parent), text=True)
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


def put_child_secret(authority_id: str, username: str, password: str) -> None:
    with _LOCK:
        value = _read()
        children = value.setdefault("children", {})
        children[authority_id] = {"username": username, "password": password}
        _write(value)


def get_child_secret(authority_id: str) -> dict | None:
    with _LOCK:
        item = _read().get("children", {}).get(authority_id)
        return item.copy() if isinstance(item, dict) else None


def remove_child_secret(authority_id: str) -> None:
    with _LOCK:
        value = _read()
        children = value.get("children", {})
        if isinstance(children, dict):
            children.pop(authority_id, None)
        _write(value)


def set_parent_secret(username: str, password: str) -> None:
    with _LOCK:
        value = _read()
        value["parent"] = {"username": username, "password": password}
        _write(value)


def ensure_local_secret() -> dict:
    with _LOCK:
        value = _read()
        item = value.get("local")
        if isinstance(item, dict) and item.get("username") and item.get("password"):
            return item.copy()
        item = {"username": "local-" + secrets.token_hex(8), "password": secrets.token_urlsafe(32)}
        value["local"] = item
        _write(value)
        return item.copy()


def all_secrets() -> dict:
    with _LOCK:
        return _read()
