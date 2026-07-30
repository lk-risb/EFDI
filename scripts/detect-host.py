#!/usr/bin/env python3
"""Report reusable host addresses without embedding deployment identifiers.

Stable mesh DNS names are preferred over IP addresses. The output is advisory:
operators copy the appropriate hostname into compose/.env or the WebUI.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
from typing import Any


def _run(*command: str) -> str:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _primary_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 53))
            return str(sock.getsockname()[0])
    except OSError:
        return ""


def _netbird() -> dict[str, str]:
    status = _run("netbird", "status", "-d")
    fqdn = re.search(r"^FQDN:\s*(\S+)", status, re.MULTILINE)
    address = re.search(r"^NetBird IP:\s*([0-9.]+)/", status, re.MULTILINE)
    return {
        "hostname": fqdn.group(1) if fqdn else "",
        "ip": address.group(1) if address else "",
    }


def _tailscale() -> dict[str, str]:
    address = _run("tailscale", "ip", "-4").strip().splitlines()
    status = _run("tailscale", "status", "--json")
    hostname = ""
    if status:
        try:
            hostname = str(json.loads(status).get("Self", {}).get("DNSName", "")).rstrip(".")
        except (json.JSONDecodeError, AttributeError):
            pass
    return {"hostname": hostname, "ip": address[0] if address else ""}


def detect() -> dict[str, Any]:
    hostname = socket.gethostname()
    fqdn = socket.getfqdn()
    return {
        "system": {
            "hostname": hostname,
            "fqdn": fqdn if fqdn != hostname else "",
            "primary_ip": _primary_ip(),
        },
        "netbird": _netbird(),
        "tailscale": _tailscale(),
    }


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--format",
        choices=("human", "json", "env"),
        default="human",
        help="output format (default: human)",
    )
    args = parser.parse_args()
    data = detect()
    if args.format == "json":
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    if args.format == "env":
        values = {
            "EFDI_HOSTNAME": data["system"]["fqdn"] or data["system"]["hostname"],
            "EFDI_PRIMARY_IP": data["system"]["primary_ip"],
            "EFDI_NETBIRD_HOSTNAME": data["netbird"]["hostname"],
            "EFDI_NETBIRD_IP": data["netbird"]["ip"],
            "EFDI_TAILSCALE_HOSTNAME": data["tailscale"]["hostname"],
            "EFDI_TAILSCALE_IP": data["tailscale"]["ip"],
        }
        for key, value in values.items():
            print(f"{key}={_shell_quote(str(value))}")
        return
    for network, values in data.items():
        available = ", ".join(f"{key}={value}" for key, value in values.items() if value)
        print(f"{network:9} {available or 'not detected'}")


if __name__ == "__main__":
    main()
