#!/usr/bin/env python3
"""STANAG vendor bundle: 4586 feed plus 4609 SRT/KLV pipeline."""

from __future__ import annotations

import os

from protocols.vendor_bundle import run_bundle


def main() -> None:
    children: list[tuple[str, str, list[str]]] = []

    if os.environ.get("STANAG4586_ZENOH_RAW", "") == "1":
        raw_topic = os.environ.get("STANAG4586_RAW_TOPIC", "").strip()
        args = ["--zenoh-raw"]
        if raw_topic:
            args.extend(["--raw-topic", raw_topic])
        children.append(("stanag4586", "protocols/_vendor/stanag/stanag4586.py", args))
    elif os.environ.get("STANAG4586_HOST", "").strip():
        children.append((
            "stanag4586",
            "protocols/_vendor/stanag/stanag4586.py",
            [
                "--host",
                os.environ.get("STANAG4586_HOST", "").strip(),
                "--port",
                os.environ.get("STANAG4586_PORT", "").strip() or "4586",
            ],
        ))

    if os.environ.get("STANAG4609_SRT_URL", "").strip():
        children.append(("stanag4609", "protocols/_vendor/stanag/stanag4609.py", []))

    run_bundle("stanag", children)


if __name__ == "__main__":
    main()
