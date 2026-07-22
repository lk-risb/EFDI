#!/usr/bin/env python3
"""ASTERIX vendor bundle: mixed UDP ingress + category translators."""

from __future__ import annotations

import os

from protocols.vendor_bundle import run_bundle


def _category_uses_raw(wanted: int) -> bool:
    if not os.environ.get("ASTERIX_PORT", "").strip():
        return False
    categories = [item.strip() for item in os.environ.get("ASTERIX_CATEGORIES", "34,48").split(",")]
    return str(wanted) in categories


def main() -> None:
    children: list[tuple[str, str, list[str]]] = []
    if os.environ.get("ASTERIX_PORT", "").strip():
        children.append(("asterix-udp", "bridges/asterix_udp_bridge.py", []))

    for cat_num in (10, 20, 21, 34, 48):
        script = f"protocols/_vendor/asterix/asterix_cat{cat_num}.py"
        port = os.environ.get(f"CAT{cat_num}_PORT", "").strip()
        tcp = os.environ.get(f"CAT{cat_num}_TCP", "") == "1"
        if _category_uses_raw(cat_num):
            children.append((f"asterix-cat{cat_num}", script, ["--zenoh-raw"]))
        elif port:
            args = ["--port", port]
            if tcp:
                args.append("--tcp")
            children.append((f"asterix-cat{cat_num}", script, args))

    if _category_uses_raw(62):
        children.append(("asterix-cat62", "protocols/_vendor/asterix/asterix_cat62.py", ["--zenoh-raw"]))
    elif os.environ.get("CAT62_UDP", "") == "1":
        children.append(("asterix-cat62", "protocols/_vendor/asterix/asterix_cat62.py", ["--udp", "--port", os.environ.get("CAT62_PORT", "50062")]))
    elif os.environ.get("CAT62_HOST", "").strip() or os.environ.get("RADAR_HOST", "").strip():
        children.append((
            "asterix-cat62",
            "protocols/_vendor/asterix/asterix_cat62.py",
            [
                "--host",
                os.environ.get("CAT62_HOST", "").strip() or os.environ.get("RADAR_HOST", "").strip(),
                "--port",
                os.environ.get("CAT62_PORT", "").strip() or os.environ.get("RADAR_PORT", "").strip() or "50062",
            ],
        ))

    run_bundle("asterix", children)


if __name__ == "__main__":
    main()
