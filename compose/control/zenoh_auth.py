"""Apply the router-local runtime credential without placing it in argv/env."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _credential_path() -> Path:
    explicit = os.environ.get("EFDI_LINK_SECRET_PATH")
    if explicit:
        return Path(explicit)
    state = Path(os.environ.get("POD_STATE_DIR", Path(__file__).resolve().parent / "state"))
    return state / "zenoh" / "link-credentials.json"


def apply_zenoh_auth(config) -> None:
    """Use local authentication when managed ACL state has been activated."""
    try:
        document = json.loads(_credential_path().read_text(encoding="utf-8"))
        credential = document["local"]
        username = credential["username"]
        password = credential["password"]
        if not isinstance(username, str) or not isinstance(password, str):
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    config.insert_json5("transport/auth/usrpwd", json.dumps({
        "user": username,
        "password": hashlib.sha256(password.encode()).hexdigest(),
    }))
