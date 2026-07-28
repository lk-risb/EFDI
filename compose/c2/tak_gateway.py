#!/usr/bin/env python3
"""Supervised, bidirectional TAK gateway.

The protocol implementations remain in ``layers.cot_layer`` (egress) and
``bridges.tak_bridge`` (ingress).  This process owns their lifecycle so the
launcher sees one TAK integration and, importantly, cannot report a healthy
gateway after either leg has died.
"""

from __future__ import annotations

import _thread
import argparse
import os
import queue
import signal
import threading
from collections.abc import Callable, Sequence

from bridges import tak_bridge
from layers import cot_layer

Failure = tuple[str, BaseException]


def _run_worker(
    name: str,
    target: Callable[[Sequence[str] | None], None],
    failures: queue.Queue[Failure],
    wake_main: bool,
) -> None:
    """Run one gateway leg and make every unexpected exit visible."""

    try:
        target([])
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
    """Hold an ingress-only gateway until a signal or worker failure."""

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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Supervised TAK CoT gateway (ingress + egress)"
    )
    parser.add_argument(
        "--direction",
        choices=["both", "in", "out"],
        default=os.environ.get("TAK_GATEWAY_DIRECTION", "both"),
    )
    args = parser.parse_args(argv)

    failures: queue.Queue[Failure] = queue.Queue()
    has_ingress = args.direction in ("both", "in")
    has_egress = args.direction in ("both", "out")

    if has_ingress:
        threading.Thread(
            target=_run_worker,
            args=("TAK ingress", tak_bridge.main, failures, True),
            name="tak-ingress",
            daemon=True,
        ).start()

    if has_egress:
        try:
            cot_layer.main([])
        except KeyboardInterrupt:
            # A failed ingress leg wakes the main thread this way. External
            # Ctrl-C follows the same clean shutdown path in cot_layer.
            pass
        _raise_failure(failures)
    else:
        _wait_for_ingress(failures)


if __name__ == "__main__":
    main()
