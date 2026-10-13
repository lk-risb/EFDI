#!/usr/bin/env python3
"""NFFI (STANAG 5527) and FFI (APP-11 Edition D) ingress -> Zenoh.

This bridge is the ingest side for protocols/vendors/nffi/nffi.py and ffi.py, which own no source socket by design and expect complete
XML documents to already be on the local Zenoh router under `.../raw/nffi/{source-id}` (NFFI) or `.../raw/ffi/{source-id}` (FFI). This
file's only job is to get those bytes there: it does not parse NFFI or FFI, does not decode a track, does not know what a SIDC is. The
two formats come in through the same connection code and the same framing; ffi_bridge.py is this bridge with the FFI_* settings, so the
two can run side by side against two servers. Which format a document is, is read from the frame, not from the bridge's name.

============================================================================
WHAT IT CONNECTS TO (checked against a live SitaWare Headquarters 6.21)
============================================================================
A SitaWare "NFFI and FFI Manager" IP1 Classic Server (Coalition Gateway, Servers) listens on the port its form asks for and writes its
source layer as NFFI or FFI (its Payload Message setting), but only to a host that has a CONTRACT on the server (the server's "No
contracts" link, Create New: the host name of this machine, a snapshot and a delta interval). A host without a contract is accepted and
closed again without data. So: create the server, create the contract for the host this bridge runs on, and set NFFI_HOST and NFFI_PORT
(or FFI_HOST and FFI_PORT) to the SitaWare machine and the server's port. The feed has no login or encryption.

Each document is written behind a 16-byte header (see docs/references/nffi/NFFI.md): the first byte is the message type (0x04 NFFI, 0x14
FFI), then 3 zero bytes, the time in epoch seconds (4 bytes, big-endian), 4 zero bytes, `01 00`, and the document length (2 bytes,
big-endian). A stream with no header (bare XML documents back to back) is still understood, by the closing tag of the root element, for
a partner server that writes none. Unconfirmed: TLS on any such server (the options below are kept), and other server profiles (IP1,
IP2, SIP3), which were not looked at.

A SitaWare server whose source layer holds the force tracks this pod sent to SitaWare (the EFDI-TAK layer of nffi_layer.py and
ffi_layer.py) sends them back: the decoders drop those (this pod's own source system EFDI-<namespace> with subsystem TAK, or a TAK id this pod published, see
fusion.is_own_force_track; a partner pod's tracks are kept), but a layer of
real force tracks is the better source layer.

============================================================================
CONFIGURATION (the FFI bridge reads the same names with FFI_ for NFFI_)
============================================================================
  NFFI_HOST              Required. Host/IP of the NFFI/FFI server to dial.
  NFFI_PORT              Required. Its port.
  NFFI_TLS               "1" to wrap the socket in TLS. Default: off.
  NFFI_CERT, NFFI_KEY    Client cert/key for mTLS (optional even with TLS on).
  NFFI_CA                CA bundle to verify the server cert against (required if NFFI_TLS=1 and the server cert isn't from a public CA).
  NFFI_TLS_SERVER_NAME   DNS SAN expected in the server certificate; defaults to NFFI_HOST.
  NFFI_SOURCE_ID         Segment identifying this feed under `.../raw/nffi/{source-id}` (or raw/ffi). Default: "sitaware".
  NFFI_RECONNECT_S       Seconds between reconnect attempts. Default: 10.
  NFFI_READ_CHUNK        Bytes per socket.recv() call. Default: 65536.
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import time

import zenoh
from namespace_prefix import topic_root
from protocols.vendors.random.gateway import open_session

TOPIC_ROOT = topic_root()

_READ_CHUNK = int(os.environ.get("NFFI_READ_CHUNK", "65536"))

# Roots to watch for as a frame boundary in a stream with no header: NFFI's NFFIMessage (or a bare track, which nffi.py also takes) and
# FFI's mtf:FriendlyForceInformation.
_FRAME_TAGS = ("NFFIMessage", "track", "mtf:FriendlyForceInformation")

HEADER_LEN = 16
_KIND_BY_TYPE = {0x04: "nffi", 0x14: "ffi"}


def _kind_of(xml: bytes) -> str:
    """"ffi" or "nffi", read from a document's own root element."""
    return "ffi" if b"FriendlyForceInformation" in xml[:400] else "nffi"


class Ip1Reader:
    """Cuts a TCP stream into (kind, xml) documents: behind SitaWare's 16-byte header (type, time, length), or bare XML back to back."""

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list:
        self._buf += data
        out = []
        while self._buf:
            if self._buf[:1] == b"<":                           # no header: bare documents, cut by the closing tag of the root
                text = [self._buf.decode("utf-8", errors="replace")]
                frames = _extract_frames(text)
                if not frames:
                    self._buf = text[0].encode("utf-8", errors="replace")
                    break
                out.extend((_kind_of(f.encode()), f.encode("utf-8", errors="replace")) for f in frames)
                self._buf = text[0].encode("utf-8", errors="replace")
                continue
            if len(self._buf) < HEADER_LEN:
                break
            if self._buf[1:4] != b"\x00\x00\x00" or self._buf[8:12] != b"\x00\x00\x00\x00":      # bytes 12-13 are the contract's header priority
                self._buf = self._buf[1:]                       # not a header: look for the next one
                continue
            length = int.from_bytes(self._buf[14:16], "big")
            if len(self._buf) < HEADER_LEN + length:
                break
            xml = self._buf[HEADER_LEN:HEADER_LEN + length]
            kind = _KIND_BY_TYPE.get(self._buf[0]) or _kind_of(xml)
            out.append((kind, xml))
            self._buf = self._buf[HEADER_LEN + length:]
        return out


def _connect(
    host: str,
    port: int,
    tls: bool,
    certfile: str | None,
    keyfile: str | None,
    cafile: str | None,
    server_name: str | None = None,
) -> socket.socket:
    raw = socket.create_connection((host, port), timeout=30)
    raw.settimeout(60.0)
    try:
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                raw.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 20000)
    except OSError:
        pass
    if not tls:
        return raw
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
    ctx.check_hostname = True
    if certfile and keyfile:
        ctx.load_cert_chain(certfile, keyfile)
    return ctx.wrap_socket(raw, server_hostname=server_name or host)


def _extract_frames(buffer: list[str]) -> list[str]:
    """Extract complete <NFFIMessage>...</NFFIMessage> (or bare <track>)
    documents from a rolling text buffer. See the module docstring's
    STATUS section — this framing assumption is unverified."""
    text = buffer[0]
    frames: list[str] = []
    while True:
        starts = [(text.find("<" + tag), tag) for tag in _FRAME_TAGS]
        starts = [(pos, tag) for pos, tag in starts if pos >= 0]
        if not starts:
            buffer[0] = text[-64:]
            return frames
        start, tag = min(starts, key=lambda item: item[0])
        close = "</{}>".format(tag)
        end = text.find(close, start)
        if end < 0:
            buffer[0] = text[start:]
            return frames
        frames.append(text[start:end + len(close)])
        text = text[end + len(close):]
        buffer[0] = text


def run(args, prefix: str = "NFFI") -> None:
    host = args.host or os.environ.get("{}_HOST".format(prefix), "").strip()
    if not host:
        raise SystemExit("Set {}_HOST or pass --host".format(prefix))
    port = args.port
    if not port:
        raise SystemExit("Set {}_PORT or pass --port".format(prefix))
    if args.tls and not args.ca:
        raise SystemExit("--ca / {}_CA is required when --tls is specified (unless the server cert is publicly trusted)".format(prefix))
    source_id = os.environ.get("{}_SOURCE_ID".format(prefix), "sitaware").strip() or "sitaware"
    reconnect_s = float(os.environ.get("{}_RECONNECT_S".format(prefix), "10"))

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("{} bridge Zenoh connect failed: {} - retry in {}s".format(prefix, exc, reconnect_s), flush=True)
            time.sleep(reconnect_s)
    publishers = {kind: session.declare_publisher("{}/raw/{}/{}".format(TOPIC_ROOT, kind, source_id)) for kind in ("nffi", "ffi")}

    print("{} ingress bridge started".format(prefix), flush=True)
    print("  Host   : {}:{}".format(host, port), flush=True)
    print("  TLS    : {}".format("on" if args.tls else "off"), flush=True)
    print("  Raw out: {}/raw/nffi|ffi/{}".format(TOPIC_ROOT, source_id), flush=True)

    while True:
        sock = None
        try:
            sock = _connect(host, port, args.tls, args.cert, args.key, args.ca, args.tls_server_name)
            print("{} {} connected -> {}:{}".format(prefix, "mTLS" if args.tls else "TCP", host, port), flush=True)
            reader = Ip1Reader()
            while True:
                chunk = sock.recv(_READ_CHUNK)
                if not chunk:
                    break
                for kind, xml in reader.feed(chunk):
                    publishers[kind].put(xml, encoding=zenoh.Encoding.APPLICATION_XML)
                    if args.verbose:
                        print("{} PUB {} ({} bytes)".format(prefix, kind, len(xml)), flush=True)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("{} ingest error on {}:{} - {} - retry in {}s".format(prefix, host, port, exc, reconnect_s), flush=True)
            time.sleep(reconnect_s)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    for publisher in publishers.values():
        publisher.undeclare()


def main(argv=None, prefix: str = "NFFI", description: str = "NFFI (STANAG 5527) / FFI (APP-11 Ed D) -> Zenoh ingress bridge") -> None:
    env = lambda name, default="": os.environ.get("{}_{}".format(prefix, name), default)
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--host", default=env("HOST").strip())
    parser.add_argument("--port", type=int, default=int(env("PORT", "0") or 0))
    parser.add_argument("--tls", action="store_true", default=env("TLS", "0") == "1")
    parser.add_argument("--cert", default=env("CERT") or None)
    parser.add_argument("--key", default=env("KEY") or None)
    parser.add_argument("--ca", default=env("CA") or None)
    parser.add_argument("--tls-server-name", default=env("TLS_SERVER_NAME") or None,
                        help="DNS SAN expected in the server certificate; defaults to the dial host")
    parser.add_argument("--verbose", "-v", action="store_true")
    run(parser.parse_args(argv), prefix)


if __name__ == "__main__":
    main()
