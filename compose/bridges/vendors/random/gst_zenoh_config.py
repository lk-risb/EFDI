"""Shared helper for the zenoh-native video prototype (video_zenoh_bridge.py
and scripts/video-zenoh-publish-test.sh): renders a Zenoh client config file
that GStreamer's zenohsrc/zenohsink elements load via their own `config=`
property.

Those elements open their OWN Zenoh session in Rust — they have no way to
reuse this pod's already-authenticated Python session from protocols/gateway.py
— so if the local router has ACL activated (see zenoh_auth.apply_zenoh_auth),
this session needs the same transport/auth/usrpwd credential or the router
just rejects it. The sha256(password) transform below is copied from
compose/control/zenoh_auth.py; it has to match exactly, since usrpwd auth
compares hashes, not plaintext.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def _credential_path() -> Path:
    explicit = os.environ.get("EFDI_LINK_SECRET_PATH")
    if explicit:
        return Path(explicit)
    # Always overridden by POD_STATE_DIR in every real launch (start.sh/run.sh
    # both export it unconditionally) — this fallback only matters for an
    # ad-hoc/interactive run. parents[3] reaches compose/ from this file's
    # current depth (bridges/vendors/random/); update the index if this file
    # moves again, same as it needed updating when it moved here.
    state = Path(os.environ.get("POD_STATE_DIR", Path(__file__).resolve().parents[3] / "state"))
    return state / "zenoh" / "link-credentials.json"


def render(endpoint: str, out_path: str | None = None) -> str:
    """Write a Zenoh client config (JSON5-compatible) to `out_path` (or a
    fixed temp-dir path) and return it. Rewritten on every call rather than
    cached — this only runs once per gst-launch subprocess start, not per
    frame, and the credential can rotate between restarts."""
    config: dict = {"mode": "client", "connect": {"endpoints": [endpoint]}}
    try:
        document = json.loads(_credential_path().read_text(encoding="utf-8"))
        credential = document["local"]
        username, password = credential["username"], credential["password"]
        if isinstance(username, str) and isinstance(password, str):
            config["transport"] = {"auth": {"usrpwd": {
                "user": username,
                "password": hashlib.sha256(password.encode()).hexdigest(),
            }}}
    except (OSError, ValueError, KeyError, TypeError):
        pass  # No ACL activated yet — same silent-skip as apply_zenoh_auth.

    path = out_path or os.path.join(tempfile.gettempdir(), "efdi-gst-zenoh-config.json5")
    with open(path, "w") as f:
        json.dump(config, f)
    os.chmod(path, 0o600)
    return path


if __name__ == "__main__":
    import sys
    print(render(sys.argv[1] if len(sys.argv) > 1 else "tcp/127.0.0.1:7448"))
