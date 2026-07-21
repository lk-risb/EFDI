"""Apply the mounted router-local runtime credential to admin Zenoh clients."""

import hashlib
import json
import os
from pathlib import Path


_PATH = Path(os.environ.get("EFDI_LINK_SECRET_PATH", "/zenoh-config/link-credentials.json"))


def apply_zenoh_auth(config) -> None:
    try:
        credential = json.loads(_PATH.read_text(encoding="utf-8"))["local"]
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
