#!/usr/bin/env python3
"""NFFI (STANAG 5527) ingress -> Zenoh.

This bridge is the missing ingest side for protocols/random/nffi.py, which
owns no source socket of its own by design (see that file's docstring) and
expects complete NFFI XML documents to already be sitting on the local
Zenoh router under `.../raw/nffi/{source-id}`. This file's only job is to
get those bytes there — it does not parse NFFI, does not decode a track,
does not know what a SIDC is. nffi.py does all of that, unchanged, on
whatever this bridge publishes.

============================================================================
STATUS: untested shell, not a confirmed-working bridge yet
============================================================================
The TCP-dial-and-frame-extraction structure below is copied from
tak_bridge.py's own ingress path (a proven, working pattern in this
codebase for "connect out to a partner's server, pull a stream of XML
documents, republish the raw bytes"), NOT from any confirmed NFFI wire
documentation. Specifically UNVERIFIED against a real endpoint:

- That the far side is a plain (or TLS) TCP socket you dial OUT to at all.
  A NATO NFFI/FFI "IP1"/"IP1 Classic"/"IP2"/"SIP3" server (see a real
  SitaWare Headquarters "NFFI and FFI Manager" screen, Coalition Gateway
  section) may behave differently per profile — could be the other side
  dialing IN to you instead, could be UDP, could need a handshake this
  file sends nothing for.
- That documents arrive back-to-back on one stream, frame-delimited only
  by their own opening/closing tags (`_extract_frames` below assumes this,
  matching how tak_bridge.py's `_extract_events` handles CoT's `<event>`
  stream) — real NFFI could be one document per TCP connection, length-
  prefixed, or something else entirely.
- The default port: there isn't one. TAK_INGEST_PORT's default of 8087 in
  tak_bridge.py came from TAK Server's own documented CoreConfig default;
  no equivalent default exists here, so NFFI_HOST/NFFI_PORT are required,
  not defaulted, and the bridge refuses to start without them.

Fill in / correct once a real SitaWare (or other partner) NFFI/FFI server
is reachable and its connection docs (which profile, host, port, TLS
y/n, actual framing) are known. Until then this is scaffolding: it will
either work as written or fail loudly with a connect/timeout error, never
silently pretend to work.

============================================================================
CONFIGURATION
============================================================================
  NFFI_HOST              Required. Host/IP of the NFFI/FFI server to dial.
  NFFI_PORT              Required. Its port.
  NFFI_TLS               "1" to wrap the socket in TLS. Default: off.
  NFFI_CERT, NFFI_KEY    Client cert/key for mTLS (optional even with TLS on).
  NFFI_CA                CA bundle to verify the server cert against
                          (required if NFFI_TLS=1 and the server cert isn't
                          from a public CA).
  NFFI_TLS_SERVER_NAME   DNS SAN expected in the server certificate;
                          defaults to NFFI_HOST.
  NFFI_SOURCE_ID         Segment identifying this feed under
                          `.../raw/nffi/{source-id}` — lets nffi.py (and
                          anyone else) tell multiple NFFI sources apart.
                          Default: "sitaware".
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
from protocols.gateway import open_session

TOPIC_ROOT = topic_root()

_SOURCE_ID = os.environ.get("NFFI_SOURCE_ID", "sitaware").strip() or "sitaware"
_RECONNECT_S = float(os.environ.get("NFFI_RECONNECT_S", "10"))
_READ_CHUNK = int(os.environ.get("NFFI_READ_CHUNK", "65536"))
_RAW_TOPIC = "{}/raw/nffi/{}".format(TOPIC_ROOT, _SOURCE_ID)

# NFFI 1.4's own root is NFFIMessage (see nffi.py's parse_nffi()), which
# also tolerates a bare <track> with no wrapper. Watch for either as a
# frame boundary, since which one a real server actually sends is one of
# the unconfirmed points above.
_FRAME_TAGS = ("NFFIMessage", "track")


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


def run(args) -> None:
    host = args.host or os.environ.get("NFFI_HOST", "").strip()
    if not host:
        raise SystemExit("Set NFFI_HOST or pass --host")
    port = args.port
    if not port:
        raise SystemExit("Set NFFI_PORT or pass --port")
    if args.tls and not args.ca:
        raise SystemExit("--ca / NFFI_CA is required when --tls is specified (unless the server cert is publicly trusted)")

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("NFFI bridge Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)
    raw_pub = session.declare_publisher(_RAW_TOPIC)

    print("NFFI ingress bridge started", flush=True)
    print("  Host   : {}:{}".format(host, port), flush=True)
    print("  TLS    : {}".format("on" if args.tls else "off"), flush=True)
    print("  Raw out: {}".format(_RAW_TOPIC), flush=True)

    while True:
        sock = None
        try:
            sock = _connect(host, port, args.tls, args.cert, args.key, args.ca, args.tls_server_name)
            mode = "mTLS" if args.tls else "TCP"
            print("NFFI {} connected -> {}:{}".format(mode, host, port), flush=True)
            text_buffer = [""]
            while True:
                chunk = sock.recv(_READ_CHUNK)
                if not chunk:
                    break
                text_buffer[0] += chunk.decode("utf-8", errors="replace")
                for xml in _extract_frames(text_buffer):
                    raw_pub.put(xml.encode("utf-8", errors="replace"), encoding=zenoh.Encoding.APPLICATION_XML)
                    if args.verbose:
                        print("NFFI PUB {} ({} bytes)".format(_RAW_TOPIC, len(xml)), flush=True)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("NFFI ingest error on {}:{} — {} — retry in {}s".format(
                host, port, exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    raw_pub.undeclare()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="NFFI (STANAG 5527) -> Zenoh ingress bridge")
    parser.add_argument("--host", default=os.environ.get("NFFI_HOST", "").strip())
    parser.add_argument("--port", type=int, default=int(os.environ.get("NFFI_PORT", "0") or 0))
    parser.add_argument("--tls", action="store_true", default=os.environ.get("NFFI_TLS", "0") == "1")
    parser.add_argument("--cert", default=os.environ.get("NFFI_CERT"))
    parser.add_argument("--key", default=os.environ.get("NFFI_KEY"))
    parser.add_argument("--ca", default=os.environ.get("NFFI_CA"))
    parser.add_argument(
        "--tls-server-name",
        default=os.environ.get("NFFI_TLS_SERVER_NAME"),
        help="DNS SAN expected in the NFFI server certificate; defaults to the dial host",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
