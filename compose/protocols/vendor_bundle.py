#!/usr/bin/env python3
"""Small supervisor helpers for vendor-level protocol bundles."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _child_cmd(rel_path: str, *args: str) -> list[str]:
    return [sys.executable, str(ROOT / rel_path), *args]


def _terminate_children(children: list[subprocess.Popen[bytes]]) -> None:
    for proc in children:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.time() + 5.0
    while time.time() < deadline and any(proc.poll() is None for proc in children):
        time.sleep(0.1)
    for proc in children:
        if proc.poll() is None:
            proc.kill()


def run_bundle(bundle_name: str, child_specs: list[tuple[str, str, list[str]]]) -> None:
    children: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop(*_args: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        _terminate_children(children)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    atexit.register(stop)

    for label, rel_path, args in child_specs:
        cmd = _child_cmd(rel_path, *args)
        proc = subprocess.Popen(cmd, env=os.environ.copy())
        children.append(proc)
        print(f"{bundle_name}: started {label} pid={proc.pid}", flush=True)

    if not children:
        raise SystemExit(f"No {bundle_name} subcomponents configured")

    try:
        while True:
            for proc in children:
                rc = proc.poll()
                if rc is not None:
                    stop()
                    if rc != 0:
                        raise SystemExit(rc)
                    raise SystemExit(0)
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
