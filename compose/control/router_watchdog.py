#!/usr/bin/env python3
"""router_watchdog.py — restart the local Zenoh router when it stops accepting clients.

The router container can keep listening (and pass its own `nc -z` healthcheck) while it refuses
every new Zenoh session: seen live, every bridge looped on "Unable to connect to any of
[tcp/127.0.0.1:7448]" for about 17 minutes until a manual restart. This opens a real Zenoh
session every WATCHDOG_INTERVAL_S, the same way a bridge does. After WATCHDOG_FAILS failures in a
row it runs `docker restart <container>`, at most once per WATCHDOG_COOLDOWN_S, and logs it.
Opt-in (ROUTER_WATCHDOG=1 in compose/.env) because a restart drops every connection for a few
seconds; the bridges and layers reconnect on their own.

Env:
  ROUTER_WATCHDOG_CONTAINER   container to restart (default efdi-pod-zenoh-router)
  ROUTER_WATCHDOG_INTERVAL_S  seconds between probes (default 30)
  ROUTER_WATCHDOG_FAILS       consecutive failures before a restart (default 4)
  ROUTER_WATCHDOG_COOLDOWN_S  minimum seconds between restarts (default 600)
"""

import os
import subprocess
import time

CONTAINER = os.environ.get("ROUTER_WATCHDOG_CONTAINER", "efdi-pod-zenoh-router")
INTERVAL_S = max(5, int(os.environ.get("ROUTER_WATCHDOG_INTERVAL_S", "30")))
FAILS = max(1, int(os.environ.get("ROUTER_WATCHDOG_FAILS", "4")))
COOLDOWN_S = int(os.environ.get("ROUTER_WATCHDOG_COOLDOWN_S", "600"))


class Watchdog:
    """step(ok, now) -> True when the router should be restarted now."""

    def __init__(self, fails: int = FAILS, cooldown_s: float = COOLDOWN_S):
        self.fails, self.cooldown_s = fails, cooldown_s
        self.failures = 0
        self.last_restart = float("-inf")

    def step(self, ok: bool, now: float) -> bool:
        if ok:
            self.failures = 0
            return False
        self.failures += 1
        if self.failures >= self.fails and now - self.last_restart >= self.cooldown_s:
            self.failures = 0
            self.last_restart = now
            return True
        return False


def probe() -> bool:
    from protocols.vendors.random.gateway import open_session
    try:
        open_session().close()
        return True
    except Exception as exc:
        print("router_watchdog: probe failed: {}".format(str(exc)[:120]), flush=True)
        return False


def main() -> None:
    dog = Watchdog()
    print("router_watchdog: probing every {}s, restarting {} after {} failures in a row".format(
        INTERVAL_S, CONTAINER, FAILS), flush=True)
    while True:
        if dog.step(probe(), time.time()):
            print("router_watchdog: restarting {}".format(CONTAINER), flush=True)
            done = subprocess.run(["docker", "restart", CONTAINER], capture_output=True, text=True, timeout=120)
            print("router_watchdog: docker restart exit {} {}".format(done.returncode, done.stderr.strip()[:200]), flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
