#!/usr/bin/env python3
"""Mixed ASTERIX UDP ingress -> category-specific raw Zenoh topics.

One UDP datagram may contain one or more complete ASTERIX frames and may mix
any configured categories. This bridge owns the socket, validates frame
boundaries, and publishes each frame unchanged to:

  <PREFIX>/<ORG>/raw/asterix/cat<category>

Category-specific protocol translators subscribe to their corresponding raw
topic. The current EFDI decoders are CAT-010, CAT-020, CAT-021, CAT-034,
CAT-048, and CAT-062; other valid categories can still be forwarded as raw
frames, but need their own category translator before they become tracks.
This bridge intentionally does not interpret any category UAP.
"""

import argparse
import ipaddress
import json
import os
import socket
import struct

import zenoh
from namespace_prefix import prefix


ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG
HERE = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

MAX_DATAGRAM = 65_535


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


def split_asterix_datagram(datagram: bytes) -> list[tuple[int, bytes]]:
    """Return ``(category, complete_frame)`` entries or reject the datagram."""
    if not datagram:
        raise ValueError("empty datagram")
    frames = []
    offset = 0
    while offset < len(datagram):
        if len(datagram) - offset < 3:
            raise ValueError("trailing bytes shorter than an ASTERIX header")
        category = datagram[offset]
        length = struct.unpack_from(">H", datagram, offset + 1)[0]
        if length < 3:
            raise ValueError("invalid ASTERIX frame length {}".format(length))
        end = offset + length
        if end > len(datagram):
            raise ValueError("ASTERIX frame length exceeds datagram")
        frames.append((category, datagram[offset:end]))
        offset = end
    return frames


def parse_categories(value: str) -> frozenset[int]:
    categories = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            category = int(item, 10)
        except ValueError as exc:
            raise ValueError("invalid ASTERIX category {!r}".format(item)) from exc
        if not 0 <= category <= 255:
            raise ValueError("ASTERIX category outside 0..255: {}".format(category))
        categories.add(category)
    if not categories:
        raise ValueError("at least one ASTERIX category is required")
    return frozenset(categories)


def parse_source_networks(values: list[str]) -> tuple[ipaddress.IPv4Network, ...]:
    networks = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError("invalid allowed source {!r}".format(item)) from exc
            if not isinstance(network, ipaddress.IPv4Network):
                raise ValueError("only IPv4 sources are supported: {}".format(item))
            networks.append(network)
    return tuple(networks)


def source_allowed(source_ip: str, networks: tuple[ipaddress.IPv4Network, ...]) -> bool:
    if not networks:
        return True
    address = ipaddress.ip_address(source_ip)
    return any(address in network for network in networks)


def open_udp_socket(bind: str, port: int, multicast_group: str, multicast_interface: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    if multicast_group:
        group = ipaddress.ip_address(multicast_group)
        interface = ipaddress.ip_address(multicast_interface)
        if not isinstance(group, ipaddress.IPv4Address) or not group.is_multicast:
            sock.close()
            raise ValueError("multicast group must be an IPv4 multicast address")
        if not isinstance(interface, ipaddress.IPv4Address):
            sock.close()
            raise ValueError("multicast interface must be an IPv4 address")
        membership = socket.inet_aton(str(group)) + socket.inet_aton(str(interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    return sock


def run(args) -> None:
    categories = parse_categories(args.categories)
    allowed_sources = parse_source_networks(args.allow_source)
    sock = open_udp_socket(
        args.bind,
        args.port,
        args.multicast_group,
        args.multicast_interface,
    )
    session = zenoh.open(make_config())
    publishers = {
        category: session.declare_publisher(
            "{}/raw/asterix/cat{}".format(TOPIC_ROOT, category)
        )
        for category in categories
    }
    print(
        "ASTERIX mixed UDP ingress on {}:{} categories={}".format(
            args.bind,
            args.port,
            ",".join(str(category) for category in sorted(categories)),
        ),
        flush=True,
    )
    if args.multicast_group:
        print(
            "ASTERIX multicast group={} interface={}".format(
                args.multicast_group,
                args.multicast_interface,
            ),
            flush=True,
        )
    if allowed_sources:
        print(
            "ASTERIX allowed sources={}".format(
                ",".join(str(network) for network in allowed_sources)
            ),
            flush=True,
        )
    try:
        while True:
            datagram, address = sock.recvfrom(MAX_DATAGRAM)
            source_ip = address[0]
            if not source_allowed(source_ip, allowed_sources):
                if args.verbose:
                    print("ASTERIX rejected source", source_ip, flush=True)
                continue
            try:
                frames = split_asterix_datagram(datagram)
            except ValueError as exc:
                print(
                    "ASTERIX rejected malformed datagram from {}: {}".format(
                        source_ip, exc
                    ),
                    flush=True,
                )
                continue
            for category, frame in frames:
                publisher = publishers.get(category)
                if publisher is None:
                    if args.verbose:
                        print(
                            "ASTERIX ignored CAT-{} from {}".format(category, source_ip),
                            flush=True,
                        )
                    continue
                publisher.put(frame, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                if args.verbose:
                    print(
                        "ASTERIX CAT-{} {} bytes from {} -> raw Zenoh".format(
                            category, len(frame), source_ip
                        ),
                        flush=True,
                    )
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        for publisher in publishers.values():
            publisher.undeclare()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mixed ASTERIX UDP -> category-specific raw Zenoh topics"
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("ASTERIX_BIND", "0.0.0.0"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ASTERIX_PORT", "50000")),
    )
    parser.add_argument(
        "--categories",
        default=os.environ.get("ASTERIX_CATEGORIES", "34,48"),
        help="comma-separated categories to publish",
    )
    parser.add_argument(
        "--multicast-group",
        default=os.environ.get("ASTERIX_MULTICAST_GROUP", ""),
    )
    parser.add_argument(
        "--multicast-interface",
        default=os.environ.get("ASTERIX_MULTICAST_INTERFACE", "0.0.0.0"),
    )
    parser.add_argument(
        "--allow-source",
        action="append",
        default=[os.environ.get("ASTERIX_ALLOW_SOURCE", "")],
        help="allowed IPv4 address/CIDR; repeat or comma-separate",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    try:
        parse_categories(args.categories)
        parse_source_networks(args.allow_source)
    except ValueError as exc:
        parser.error(str(exc))
    run(args)


if __name__ == "__main__":
    main()
