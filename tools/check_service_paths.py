#!/usr/bin/env python3
"""Fail if a launcher starts a service whose script path does not exist.

Catches the recurring class of bug where a service is renamed or moved but a
launcher still points at the old file — e.g. `bridges/cot_bridge.py` lingering
in run.sh after the entrypoint became `layers/cot_layer.py`. Compile-checks
pass in that case because the dead file is simply never imported; only an
operator selecting the service at runtime discovers the break.

Scans start.sh and run.sh for launch commands (`start`/`_start`) and asserts
every literal `<bridges|c2|layers|protocols>/<...>.py` argument resolves under
compose/.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose"
LAUNCHERS = ("start.sh", "run.sh")

# Only inspect actual launch lines, so a comment mentioning an old filename is
# not mistaken for a live invocation.
LAUNCH_LINE = re.compile(r"^\s*_?start\s")
MODULE_PATH = re.compile(
    r"(?<![\w./])((?:bridges|c2|layers|protocols)/[A-Za-z0-9_./-]+\.py)"
)


def main() -> int:
    missing: list[str] = []
    checked = 0
    for launcher in LAUNCHERS:
        path = ROOT / launcher
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not LAUNCH_LINE.match(line):
                continue
            for match in MODULE_PATH.finditer(line):
                rel = match.group(1)
                checked += 1
                if not (COMPOSE / rel).is_file():
                    missing.append(f"{launcher}:{lineno}: {rel}")

    if missing:
        print("Service launch paths that do not resolve under compose/:")
        for entry in missing:
            print("  " + entry)
        return 1
    print(f"OK: {checked} service launch path references all resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
