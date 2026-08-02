#!/usr/bin/env python3
"""Reusable raw socket -> Zenoh ingress for profile-specific decoders.

This module deliberately does not decode a protocol.  It owns only the
transport and publishes octets to a raw topic; the matching protocol process
does validation and translation.
"""

from __future__ import annotations

import argparse
import os
import socket
import time

import zenoh

from namespace_prefix import topic_root
from protocols.gateway import open_session


def run_raw(protocol: str, default_port: int, args, frame_mode: str = "chunk") -> None:
    root = topic_root()
    topic = args.topic or "{}/raw/{}/{}".format(root, protocol, args.source)
    session = open_session()
    publisher = session.declare_publisher(topic)
    sock = socket.socket(socket.AF_INET6 if ":" in args.bind else socket.AF_INET,
                         socket.SOCK_STREAM if args.tcp else socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if args.tcp:
        sock.bind((args.bind, args.port))
        sock.listen(8)
        print("{} raw TCP listening on {}:{} -> {}".format(protocol, args.bind, args.port, topic), flush=True)
    else:
        sock.bind((args.bind, args.port))
        print("{} raw UDP listening on {}:{} -> {}".format(protocol, args.bind, args.port, topic), flush=True)
    try:
        while True:
            if args.tcp:
                client, address = sock.accept()
                try:
                    buffer = bytearray()
                    while True:
                        data = client.recv(args.max_bytes)
                        if not data:
                            break
                        buffer.extend(data)
                        if frame_mode == "vmf16be":
                            while len(buffer) >= 2:
                                length = int.from_bytes(buffer[:2], "big")
                                if length <= 0 or length > args.max_bytes:
                                    del buffer[0]
                                    continue
                                if len(buffer) < length + 2:
                                    break
                                publisher.put(bytes(buffer[2:2 + length]),
                                              encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                                del buffer[:2 + length]
                        elif frame_mode == "sapient32le":
                            while len(buffer) >= 4:
                                length = int.from_bytes(buffer[:4], "little")
                                if length <= 0 or length > args.max_bytes:
                                    del buffer[0]
                                    continue
                                if len(buffer) < length + 4:
                                    break
                                publisher.put(bytes(buffer[:length + 4]),
                                              encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                                del buffer[:length + 4]
                        elif frame_mode == "stanag16le":
                            while len(buffer) >= 6:
                                length = int.from_bytes(buffer[2:4], "little")
                                if length < 6 or length > args.max_bytes:
                                    del buffer[0]
                                    continue
                                if len(buffer) < length:
                                    break
                                publisher.put(bytes(buffer[:length]),
                                              encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                                del buffer[:length]
                        else:
                            publisher.put(data, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                finally:
                    client.close()
            else:
                data, _address = sock.recvfrom(args.max_bytes)
                if data:
                    publisher.put(data, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        publisher.undeclare()
        session.close()


def add_args(parser: argparse.ArgumentParser, protocol: str, default_port: int) -> None:
    parser.add_argument("--bind", default=os.environ.get(protocol.upper() + "_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get(protocol.upper() + "_RAW_PORT", default_port)))
    parser.add_argument("--tcp", action="store_true", default=False)
    parser.add_argument("--source", default=os.environ.get(protocol.upper() + "_SOURCE", socket.gethostname()))
    parser.add_argument("--topic", default="")
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
