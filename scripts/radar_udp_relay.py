#!/usr/bin/env python3
"""Relay a radar UDP feed to the EFDI operator laptop."""

from __future__ import annotations

import argparse
import os
import socket


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Listen for radar UDP datagrams and forward them to EFDI."
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        required=True,
        help="UDP port on this computer where the radar sends its data",
    )
    parser.add_argument(
        "--listen-host",
        default="0.0.0.0",
        help="local address to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--destination-host",
        default=os.environ.get("EFDI_DESTINATION_HOST"),
        help="EFDI router hostname or IP (or set EFDI_DESTINATION_HOST)",
    )
    parser.add_argument(
        "--destination-port",
        type=int,
        default=50000,
        help="EFDI laptop UDP port (default: 50000)",
    )
    args = parser.parse_args()
    if not args.destination_host:
        parser.error("--destination-host or EFDI_DESTINATION_HOST is required")
    for name in ("listen_port", "destination_port"):
        port = getattr(args, name)
        if not 1 <= port <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be between 1 and 65535")
    return args


def main() -> None:
    args = parse_args()
    destination = socket.getaddrinfo(
        args.destination_host,
        args.destination_port,
        type=socket.SOCK_DGRAM,
    )[0]
    family, socktype, protocol, _, destination_address = destination

    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver,
        socket.socket(family, socktype, protocol) as sender,
    ):
        receiver.bind((args.listen_host, args.listen_port))
        print(
            "Relaying UDP {}:{} -> {}:{} (Ctrl-C to stop)".format(
                args.listen_host,
                args.listen_port,
                args.destination_host,
                args.destination_port,
            ),
            flush=True,
        )
        packets = 0
        try:
            while True:
                payload, source = receiver.recvfrom(65535)
                sender.sendto(payload, destination_address)
                packets += 1
                if packets == 1 or packets % 1000 == 0:
                    print(
                        f"Forwarded {packets} datagram(s); latest source "
                        f"{source[0]}:{source[1]}",
                        flush=True,
                    )
        except KeyboardInterrupt:
            print(f"\nStopped after forwarding {packets} datagram(s).")


if __name__ == "__main__":
    main()
