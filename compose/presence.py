#!/usr/bin/env python3
"""presence.py — declare Zenoh liveliness tokens for the pod's live feeds.

Panoscope (and any fabric inspection surface) draws a NODE from a liveliness
token, not from data traffic: a producer that is merely quiet looks the same as
one that died unless it declares presence.  This service is the pod's producer
side of the reference `examples/liveliness_presence.py` pattern — it declares one
token per running data service under the slot root:

    <PARTNER_NAMESPACE>/_meta/alive/<service>

The source of truth for "which feeds are live" is the launcher's pidfile dir
(the same files start.sh writes).  Each sweep declares a token for every service
whose pidfile names a live PID and withdraws the token for any that vanished, so
presence tracks real process liveness.  A hard crash of this process withdraws
every token for free — liveliness is tied to the session — and the supervisor
restarts it within its sweep interval.
"""

from __future__ import annotations

import os
import time

import zenoh
from protocols.translation_common import make_config

SLOT = os.environ.get("PARTNER_NAMESPACE", "")
PID_DIR = os.path.join(os.environ.get("POD_STATE_DIR", os.path.join(os.path.dirname(__file__), "state")), ".pids")
SWEEP_SECONDS = int(os.environ.get("PRESENCE_SWEEP_SECONDS", "10"))

# Infrastructure processes are not data feeds; keep the node graph to producers.
_EXCLUDE = {"presence", "admin-control", "supervisor"}


def _live_services() -> set[str]:
    """Names of services whose pidfile names a running process."""
    live = set()
    try:
        entries = os.listdir(PID_DIR)
    except FileNotFoundError:
        return live
    for entry in entries:
        if not entry.endswith(".pid"):
            continue
        name = entry[: -len(".pid")]
        if name in _EXCLUDE:
            continue
        try:
            pid = int(open(os.path.join(PID_DIR, entry)).read().strip())
            os.kill(pid, 0)  # signal 0 = liveness probe, sends nothing
        except (ValueError, OSError):
            continue
        live.add(name)
    return live


def main() -> None:
    if not SLOT:
        print("presence: PARTNER_NAMESPACE unset — no slot to announce, exiting", flush=True)
        return
    session = zenoh.open(make_config())
    tokens: dict[str, object] = {}
    print("presence: announcing live feeds under {}/_meta/alive/*".format(SLOT), flush=True)
    try:
        while True:
            live = _live_services()
            for name in live - tokens.keys():
                key = "{}/_meta/alive/{}".format(SLOT, name)
                tokens[name] = session.liveliness().declare_token(key)
                print("presence: UP   {}".format(key), flush=True)
            for name in tokens.keys() - live:
                tokens.pop(name).undeclare()
                print("presence: DOWN {}/_meta/alive/{}".format(SLOT, name), flush=True)
            time.sleep(SWEEP_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        for token in tokens.values():
            token.undeclare()
        session.close()


if __name__ == "__main__":
    main()
