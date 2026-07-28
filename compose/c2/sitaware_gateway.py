#!/usr/bin/env python3
"""Supervised, bidirectional SitaWare gateway.

Only configured legs are started:

* NVG feed: Zenoh -> SitaWare HQ (``layers.nvg_layer``)
* NVG export: SitaWare HQ -> Zenoh (``bridges.nvg_bridge``)
* REST units: SitaWare HQ -> Zenoh (``bridges.sitaware_bridge``)

The protocol implementations remain independently runnable.  This process owns
their lifecycle and exits non-zero if a selected leg dies.
"""

from __future__ import annotations

import _thread
import argparse
import os
import queue
import signal
import threading
from collections.abc import Callable, Sequence

from bridges import nvg_bridge, sitaware_bridge
from layers import nvg_layer

Failure = tuple[str, BaseException]
_TRUE = frozenset({"1", "true", "yes", "on"})


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def configured_legs() -> dict[str, bool]:
    """Return the SitaWare legs that have enough runtime configuration."""

    base = bool(
        os.environ.get("SITAWARE_URL", "").strip()
        or os.environ.get("SITAWARE_URL_FALLBACK", "").strip()
    )
    api_path = bool(os.environ.get("SITAWARE_API_PATH", "").strip())
    discover = _enabled("SITAWARE_DISCOVER")
    nvg_url = bool(os.environ.get("SITAWARE_NVG_IMPORT_URL", "").strip())

    enable_value = os.environ.get("SITAWARE_HQ_NVG_ENABLE")
    if enable_value is None:
        feed = _enabled("SITAWARE_HQ_NVG_ALLOW_ANONYMOUS") or bool(
            os.environ.get("SITAWARE_HQ_NVG_USER", "").strip()
            and os.environ.get("SITAWARE_HQ_NVG_PASS", "")
        )
    else:
        feed = enable_value.strip().lower() in _TRUE

    return {
        "feed": feed,
        "nvg_ingress": nvg_url or (base and api_path),
        "rest_ingress": base and (api_path or discover),
    }


def _run_worker(
    name: str,
    target: Callable[[Sequence[str] | None], None],
    argv: Sequence[str],
    failures: queue.Queue[Failure],
    wake_main: bool,
) -> None:
    try:
        target(argv)
    except BaseException as exc:
        failures.put((name, exc))
    else:
        failures.put((name, RuntimeError("leg exited unexpectedly")))
    if wake_main:
        _thread.interrupt_main()


def _raise_failure(failures: queue.Queue[Failure]) -> None:
    try:
        name, exc = failures.get_nowait()
    except queue.Empty:
        return
    if isinstance(exc, SystemExit) and isinstance(exc.code, str):
        detail = exc.code
    else:
        detail = str(exc) or type(exc).__name__
    raise RuntimeError("{} failed: {}".format(name, detail)) from exc


def _wait_for_ingress(failures: queue.Queue[Failure]) -> None:
    stopped = threading.Event()

    def stop(_signum, _frame) -> None:
        stopped.set()

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, stop)
        except (OSError, ValueError):
            pass
    try:
        stopped.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    _raise_failure(failures)


def _interruptible_feed() -> None:
    """Let SIGTERM use nvg_layer's KeyboardInterrupt cleanup path."""

    def interrupt(_signum, _frame) -> None:
        raise KeyboardInterrupt

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous[signum] = signal.signal(signum, interrupt)
        except (OSError, ValueError):
            pass
    try:
        nvg_layer.main([])
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Supervised SitaWare gateway (NVG/REST ingress + NVG egress)"
    )
    parser.add_argument(
        "--direction",
        choices=["both", "in", "out"],
        default=os.environ.get("SITAWARE_GATEWAY_DIRECTION", "both"),
    )
    args = parser.parse_args(argv)

    configured = configured_legs()
    want_ingress = args.direction in ("both", "in")
    want_egress = args.direction in ("both", "out")
    run_feed = want_egress and configured["feed"]
    workers: list[tuple[str, Callable[[Sequence[str] | None], None], list[str]]] = []

    if want_ingress and configured["nvg_ingress"]:
        workers.append(("SitaWare NVG ingress", nvg_bridge.main, []))
    if want_ingress and configured["rest_ingress"]:
        rest_argv = ["--discover"] if _enabled("SITAWARE_DISCOVER") else []
        workers.append(("SitaWare REST ingress", sitaware_bridge.main, rest_argv))

    if not run_feed and not workers:
        raise SystemExit(
            "no configured SitaWare gateway legs; enable the HQ NVG feed or "
            "configure an NVG/REST ingress endpoint"
        )
    if want_egress and not run_feed:
        print("SitaWare gateway: NVG feed disabled; running configured ingress only", flush=True)
    if want_ingress and not workers:
        print("SitaWare gateway: no ingress endpoint configured; running NVG feed only", flush=True)

    failures: queue.Queue[Failure] = queue.Queue()
    for name, target, leg_argv in workers:
        threading.Thread(
            target=_run_worker,
            args=(name, target, leg_argv, failures, True),
            name=name.lower().replace(" ", "-"),
            daemon=True,
        ).start()

    if run_feed:
        try:
            _interruptible_feed()
        except KeyboardInterrupt:
            pass
        _raise_failure(failures)
    else:
        _wait_for_ingress(failures)


if __name__ == "__main__":
    main()
