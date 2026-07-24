#!/usr/bin/env python3
"""supervisor.py — restart bridges, protocols and layers that died on their own.

Watches the host-managed services and brings back the ones that crashed. It
deliberately does NOT re-run anything that was stopped on purpose, so it can be
left running alongside stop.sh and the admin web UI without fighting either.

The distinction it keys off already exists in admin_control._service_status():

  crashed  pidfile present, process gone   -> the service died; restart it
  stopped  no pidfile                      -> stop.sh / the UI removed it; leave it
  running  pidfile present, process alive  -> nothing to do

stop.sh deletes the pidfile as part of stopping, which is what makes a clean
stop indistinguishable from "never started" and distinguishable from a crash.
Restarting on "stopped" would mean a `./stop.sh all` immediately came back up.
Use --start-stopped only for a boot-time sweep, where that is the intent.

Only bridges, protocols and layers are supervised. Infrastructure (the Zenoh
router and the control agent itself) is excluded: restarting the agent from
underneath the web UI would drop the connection the request arrived on.

Run:
    venv/bin/python3 compose/supervisor.py                  # crash recovery, every 15s
    venv/bin/python3 compose/supervisor.py --once           # single pass, for cron
    venv/bin/python3 compose/supervisor.py --start-stopped   # also start selected-but-down
    venv/bin/python3 compose/supervisor.py --dry-run -v      # report, change nothing
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admin_control as ac

# A service that has just been restarted is given this long to prove it can
# stay up. Dying sooner counts as a failed attempt and widens the backoff,
# which is what stops a misconfigured service from being restarted forever.
STABLE_AFTER_S = 60.0

# Backoff after consecutive failed restarts: 15s, 30s, 60s ... capped. Without
# it, a service that exits instantly (missing credentials, port already bound)
# would be respawned every tick and bury its own error in the log.
BACKOFF_BASE_S = 15.0
BACKOFF_MAX_S = 600.0

SUPERVISED_KINDS = frozenset({"bridge", "protocol", "layer"})


def supervisable() -> list[str]:
    """Service names this tool is willing to restart, in catalog order."""
    names = []
    for name in ac.SERVICE_NAMES:
        kind = ac._service_kind(name, ac.SERVICE_SOURCES.get(name, ""))
        if kind in SUPERVISED_KINDS:
            names.append(name)
    return sorted(names)


def _backoff(failures: int) -> float:
    return min(BACKOFF_BASE_S * (2 ** max(0, failures - 1)), BACKOFF_MAX_S)


def orphan_pids(name: str) -> list[int]:
    """PIDs running this service's script that its pidfile does not account for.

    A pidfile can go stale while the process it named is still alive — a crashed
    launcher, a hand-started run, a pidfile overwritten by a second start. The
    status probe only validates the PID it was given, so it reports "crashed"
    and a restart would add a SECOND copy. That is actively harmful for the C2
    layers: two cot_layer instances open two TLS sessions with the same client
    certificate, and TAK Server drops one session per new one, so the pair
    reconnect-loop against each other indefinitely.
    """
    source = ac.SERVICE_SOURCES.get(name, "")
    if not source.endswith(".py"):
        return []
    known = None
    try:
        known = int((ac.PID_DIR / f"{name}.pid").read_text().strip())
    except (OSError, ValueError):
        pass
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == known or pid == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        # Split on the real NUL separators rather than substring-matching the
        # flattened string. A shell invoked as `bash -c '<whole script>'` carries
        # that entire script as ONE argv element, so any script mentioning both
        # "python" and this path would otherwise look like a running instance —
        # and get "adopted", or worse, killed as a stray.
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]
        if len(argv) < 2:
            continue
        if not Path(argv[0]).name.startswith("python"):
            continue
        if any(arg == source or arg.endswith("/" + source) for arg in argv[1:]):
            found.append(pid)
    return found


def _wait_running(name: str, timeout_s: float = 8.0) -> bool:
    """Poll until the service reports running, or the grace period expires.

    start.sh backgrounds a subshell that only then exec()s the interpreter, and
    writes the pidfile before that exec has happened. Probing the instant the
    script returns therefore reads a /proc entry that is still the shell, and
    the cmdline check correctly says "that is not the service" — a healthy
    start looks like an immediate failure. Give the exec a moment to land.
    """
    deadline = time.time() + timeout_s
    while True:
        if ac._service_status(name)["running"]:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def log(message: str) -> None:
    print("[{}] {}".format(time.strftime("%Y-%m-%dT%H:%M:%S"), message), flush=True)


def sweep(state: dict, start_stopped: bool, dry_run: bool, verbose: bool) -> None:
    selected = set(ac._selected())
    now = time.time()

    for name in supervisable():
        if name not in selected:
            continue

        status = ac._service_status(name)
        entry = state.setdefault(name, {"failures": 0, "next_attempt": 0.0, "started_at": 0.0})

        if status["running"]:
            # Survived the probation window, so the last restart worked and the
            # backoff must be cleared — otherwise a service that crashes once a
            # day would inherit an ever-growing delay.
            if entry["failures"] and entry["started_at"] and now - entry["started_at"] >= STABLE_AFTER_S:
                log("{} stable again, clearing backoff".format(name))
                entry["failures"] = 0
                entry["started_at"] = 0.0
            continue

        wanted = status["status"] == "crashed" or (start_stopped and status["status"] == "stopped")
        if not wanted:
            if verbose:
                log("{} is {} — not a crash, leaving alone".format(name, status["status"]))
            continue

        if now < entry["next_attempt"]:
            if verbose:
                log("{} backing off {:.0f}s more".format(name, entry["next_attempt"] - now))
            continue

        orphans = orphan_pids(name)
        if orphans:
            log("{} reports {} but pid(s) {} are still running it — "
                "adopting, not starting a duplicate".format(
                    name, status["status"], ", ".join(str(p) for p in orphans)))
            if not dry_run:
                (ac.PID_DIR / f"{name}.pid").write_text(f"{orphans[0]}\n")
            continue

        if dry_run:
            log("{} is {} — would restart".format(name, status["status"]))
            continue

        log("{} is {} — restarting".format(name, status["status"]))
        result = ac._run_script(ac.START_SCRIPT, ["--service", name])
        # start.sh reporting success is not proof the process stayed up; it
        # skips services whose prerequisites are unset and still exits 0.
        if result["ok"] and _wait_running(name):
            entry["failures"] = 0
            entry["next_attempt"] = 0.0
            entry["started_at"] = time.time()
            log("{} restarted".format(name))
        else:
            entry["failures"] += 1
            delay = _backoff(entry["failures"])
            entry["next_attempt"] = time.time() + delay
            entry["started_at"] = 0.0
            detail = (result.get("output") or "").strip().splitlines()
            reason = detail[-1] if detail else "no output"
            log("{} restart failed (attempt {}, next try in {:.0f}s): {}".format(
                name, entry["failures"], delay, reason))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restart crashed EFDI bridges, protocols and layers")
    parser.add_argument("--interval", type=float, default=15.0,
                        help="seconds between sweeps (default: 15)")
    parser.add_argument("--once", action="store_true",
                        help="run a single sweep and exit (for cron/systemd timers)")
    parser.add_argument("--start-stopped", action="store_true",
                        help="also start selected services that have no pidfile — a "
                             "boot-time sweep, NOT safe to leave on alongside stop.sh")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be restarted, change nothing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    watched = supervisable()
    log("supervising {} services every {:.0f}s (crash-only={})".format(
        len(watched), args.interval, not args.start_stopped))
    if args.verbose:
        log("watching: {}".format(", ".join(watched)))

    state: dict = {}
    while True:
        try:
            sweep(state, args.start_stopped, args.dry_run, args.verbose)
        except Exception as exc:  # a bad sweep must not kill the supervisor
            log("sweep error: {!r}".format(exc))
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
