#!/usr/bin/env python3
"""Passively summarize ASTERIX categories and SAC/SIC values on one UDP port."""

import argparse
import collections
import ipaddress
import socket
import struct
import time


def split_frames(datagram: bytes) -> list[tuple[int, bytes]]:
    frames = []
    offset = 0
    while offset < len(datagram):
        if len(datagram) - offset < 3:
            raise ValueError("trailing bytes")
        category = datagram[offset]
        length = struct.unpack_from(">H", datagram, offset + 1)[0]
        if length < 3 or offset + length > len(datagram):
            raise ValueError("invalid frame length")
        frames.append((category, datagram[offset:offset + length]))
        offset += length
    if not frames:
        raise ValueError("empty datagram")
    return frames


def extract_sac_sic(frame: bytes) -> tuple[int, int] | None:
    """Read first-FRN Data Source Identifier when the category carries it."""
    if len(frame) < 6:
        return None
    pos = 3
    first_fspec = frame[pos]
    pos += 1
    while first_fspec & 0x01:
        if pos >= len(frame):
            return None
        first_fspec = frame[pos]
        pos += 1
    if not frame[3] & 0x80 or pos + 2 > len(frame):
        return None
    return frame[pos], frame[pos + 1]


def open_socket(bind: str, port: int, multicast_group: str, multicast_interface: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    if multicast_group:
        group = ipaddress.ip_address(multicast_group)
        interface = ipaddress.ip_address(multicast_interface)
        if not isinstance(group, ipaddress.IPv4Address) or not group.is_multicast:
            sock.close()
            raise ValueError("multicast group must be an IPv4 multicast address")
        membership = socket.inet_aton(str(group)) + socket.inet_aton(str(interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    return sock


def main() -> None:
    parser = argparse.ArgumentParser(description="Observe an ASTERIX UDP stream")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--multicast-group", default="")
    parser.add_argument("--multicast-interface", default="0.0.0.0")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if args.interval <= 0:
        parser.error("--interval must be positive")

    try:
        sock = open_socket(
            args.bind,
            args.port,
            args.multicast_group,
            args.multicast_interface,
        )
    except ValueError as exc:
        parser.error(str(exc))
    sock.settimeout(min(args.interval, 1.0))
    counts = collections.Counter()
    malformed = 0
    started = last_report = time.monotonic()
    print("Listening on UDP {}:{}; Ctrl-C to stop".format(args.bind, args.port), flush=True)
    try:
        while True:
            try:
                datagram, address = sock.recvfrom(65_535)
            except TimeoutError:
                datagram = None
            if datagram is not None:
                try:
                    for category, frame in split_frames(datagram):
                        sac_sic = extract_sac_sic(frame)
                        counts[(address[0], category, sac_sic)] += 1
                except ValueError:
                    malformed += 1
            now = time.monotonic()
            if now - last_report < args.interval:
                continue
            elapsed = now - started
            if not counts and not malformed:
                print("No ASTERIX frames observed", flush=True)
            for (source, category, sac_sic), count in sorted(counts.items()):
                identity = "SAC/SIC unknown"
                if sac_sic is not None:
                    identity = "SAC/SIC {}/{}".format(*sac_sic)
                print(
                    "src={} dst-port={} CAT-{} {} frames={} rate={:.2f}/s".format(
                        source,
                        args.port,
                        category,
                        identity,
                        count,
                        count / elapsed,
                    ),
                    flush=True,
                )
            if malformed:
                print("malformed datagrams={}".format(malformed), flush=True)
            last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
