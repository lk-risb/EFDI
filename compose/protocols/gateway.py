"""The Zenoh backend: the only module that imports zenoh directly.

Every protocol translator (ASTERIX, SAPIENT, STANAG, Sparkplug, MQTT-JSON,
the `random/` bridges) and every fabric-facing bridge produces and consumes
plain dicts/bytes; opening a Zenoh session, publishing the JSON/SAPIENT/
protobuf views, and subscribing to a topic all go through here instead. That
keeps the transport swappable in one place — replacing Zenoh later means
editing this module, not every protocol file that uses it — and it doubles
as the shared onboarding layer bridges sit behind, since bridges are
per-user/per-integration code while getting data into and out of the fabric
is not.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time

import zenoh
from zenoh_auth import apply_zenoh_auth

from namespace_prefix import topic_root
from protocols.data_stats import record_in

# Re-exported so a category that needs to catch a connect-failure can do
# `from protocols.gateway import ZError` instead of importing zenoh itself.
ZError = zenoh.ZError

ORG = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")


def make_config(endpoint: str | None = None, *, local: bool = True) -> "zenoh.Config":
    """Build a connect config for `endpoint` (default: this process's own
    standard endpoint). `local=False` skips EFDI's own mTLS auth/cert
    material entirely — for a relay bridge's upstream leg, which connects to
    a different (possibly partner-operated, possibly unauthenticated) router
    that EFDI's own certs don't apply to. Never skip auth for EFDI's own
    endpoint; this flag exists for that one relay case, not as a shortcut."""
    config = zenoh.Config()
    config.insert_json5("mode", '"client"')
    ep = endpoint or ENDPOINT
    config.insert_json5("connect/endpoints", json.dumps([ep]))
    if local:
        apply_zenoh_auth(config)
        if ep.startswith("tls"):
            config.insert_json5("transport/link/tls", json.dumps({
                "root_ca_certificate": os.path.join(CERT_DIR, "efdi-ca-root.pem"),
                "connect_certificate": os.path.join(CERT_DIR, ORG + "-cert.pem"),
                "connect_private_key": os.path.join(CERT_DIR, ORG + "-key.pem"),
                "enable_mtls": True,
                "verify_name_on_connect": True,
            }))
    return config


def open_session(endpoint: str | None = None, *, local: bool = True):
    """Open a Zenoh session. See make_config() for `endpoint`/`local`."""
    return zenoh.open(make_config(endpoint, local=local))


def subscribe(session, topic: str, callback):
    """Declare a subscriber. The only place session.declare_subscriber() is called directly."""
    return session.declare_subscriber(topic, callback)


def payload_bytes(sample) -> bytes:
    payload = getattr(sample, "payload", sample)
    if hasattr(payload, "to_bytes"):
        return payload.to_bytes()
    return bytes(payload)


def payload_json(sample) -> object:
    return json.loads(payload_bytes(sample).decode("utf-8"))


def put_json(publisher, record: dict) -> None:
    publisher.put(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode(),
                  encoding=zenoh.Encoding.APPLICATION_JSON)


def base_record(source: str, uid: str, **fields) -> dict:
    record = {"_ts": time.time(), "_src": source, "uid": uid}
    record.update(fields)
    return record


def publish_dual(session, topic: str, track: dict, message_class, wrapper_field: str = "track") -> None:
    """Publish one track on its object key, in every view (JSON/SAPIENT/protobuf).

    Thin wrapper over protocols.track_views.publish_dual that injects the
    zenoh module so callers never need their own `import zenoh`."""
    from protocols.track_views import publish_dual as _publish_dual
    _publish_dual(session, topic, track, message_class, zenoh, wrapper_field=wrapper_field)


def publish_collection(session, topic: str, track: dict, message_class, wrapper_field: str = "track") -> None:
    """Publish one track onto a stable, registry-friendly collection key.

    Thin wrapper over protocols.track_views.publish_collection — see its
    docstring for when to use this instead of publish_dual()."""
    from protocols.track_views import publish_collection as _publish_collection
    _publish_collection(session, topic, track, message_class, zenoh, wrapper_field=wrapper_field)


def publish_native(session, topic: str, payload: bytes, protocol: str,
                    profile: str = "", content_type: str = "application/octet-stream",
                    received_timestamp: float = 0.0) -> None:
    """Publish source bytes verbatim inside a RawEnvelope protobuf.

    Thin wrapper over protocols.track_views.publish_native — see its
    docstring for why this view exists alongside publish_dual()."""
    from protocols.track_views import publish_native as _publish_native
    _publish_native(session, topic, payload, protocol, zenoh, profile=profile,
                     content_type=content_type, received_timestamp=received_timestamp)


# --------------------------------------------------------------------------
# ASTERIX inbound framing: the 3-byte CAT+LEN header is an ASTERIX wire
# convention, shared by every category in cat.py rather than per-category.
# The session.declare_subscriber() call inside run_zenoh_raw() is the only
# place this side touches zenoh, and it goes through subscribe() above.
# --------------------------------------------------------------------------

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return buf


def iter_frames_tcp(sock: socket.socket, stat_label: str):
    """Yield (category, payload_bytes) for each ASTERIX frame on a TCP stream."""
    while True:
        header = recv_exact(sock, 3)
        cat    = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        if length < 3:
            # There is no sync marker in an ASTERIX byte stream. Continuing
            # after an impossible length would interpret payload bytes as a
            # new header and silently corrupt every subsequent record.
            raise ValueError("invalid ASTERIX frame length: {}".format(length))
        data = recv_exact(sock, length - 3)
        record_in(stat_label, len(data))
        yield cat, data


def iter_frames_udp(sock: socket.socket, stat_label: str):
    """Yield (category, payload_bytes) for each ASTERIX frame in UDP datagrams."""
    while True:
        pkt, _ = sock.recvfrom(65535)
        offset = 0
        while offset + 3 <= len(pkt):
            cat    = pkt[offset]
            length = struct.unpack(">H", pkt[offset + 1:offset + 3])[0]
            if length < 3 or offset + length > len(pkt):
                break
            record = pkt[offset + 3:offset + length]
            record_in(stat_label, len(record))
            yield cat, record
            offset += length


def raw_frame_payload(frame: bytes, expected_category: int) -> bytes:
    if len(frame) < 3:
        raise ValueError("raw ASTERIX frame is shorter than its header")
    category = frame[0]
    length = struct.unpack(">H", frame[1:3])[0]
    if category != expected_category:
        raise ValueError("expected CAT-{}, received CAT-{}".format(expected_category, category))
    if length != len(frame):
        raise ValueError("ASTERIX header length does not match raw publication")
    return frame[3:]


def process_stream(frame_iter, handlers: dict, verbose: bool):
    for cat, data in frame_iter:
        if cat in handlers:
            handlers[cat](data, verbose)
        elif verbose:
            print("UNKNOWN cat=0x{:02x} len={}  hex={}".format(
                cat, len(data), data[:16].hex()), flush=True)


def run_zenoh_raw(session, input_topic: str, category: int, handler, verbose: bool):
    lock = threading.Lock()

    def _on_sample(sample):
        try:
            payload = raw_frame_payload(bytes(sample.payload), category)
        except ValueError as exc:
            print("CAT-{} ignored invalid raw Zenoh frame: {}".format(category, exc), flush=True)
            return
        with lock:
            handler(payload, verbose)

    subscriber = subscribe(session, input_topic, _on_sample)
    print("CAT-{} raw Zenoh input: {}".format(category, input_topic), flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        subscriber.undeclare()


def netbird_ip() -> str:
    for iface in ("wt0", "netbird0"):
        try:
            import subprocess
            out = subprocess.check_output(
                ["ip", "-4", "addr", "show", iface],
                stderr=subprocess.DEVNULL, text=True)
            for tok in out.split():
                if "/" in tok and tok[0].isdigit():
                    return tok.split("/")[0]
        except Exception:
            pass
    return socket.gethostname()


def run_inbound(port: int, use_tcp: bool, label: str, stat_label: str, handlers: dict, verbose: bool):
    ip = netbird_ip()
    if use_tcp:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        print("{} TCP server on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        while True:
            conn, addr = srv.accept()
            print("{} TCP connected: {}".format(label, addr), flush=True)
            threading.Thread(
                target=process_tcp_conn,
                args=(conn, addr, label, stat_label, handlers, verbose),
                daemon=True).start()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        print("{} UDP on 0.0.0.0:{}  (send to {}:{})".format(
            label, port, ip, port), flush=True)
        process_stream(iter_frames_udp(sock, stat_label), handlers, verbose)


def process_tcp_conn(conn, addr, label, stat_label, handlers, verbose):
    try:
        process_stream(iter_frames_tcp(conn, stat_label), handlers, verbose)
    except EOFError:
        pass
    except ValueError as exc:
        print("{} TCP protocol error from {}: {}".format(label, addr, exc), flush=True)
    finally:
        conn.close()
        print("{} TCP disconnected: {}".format(label, addr), flush=True)
