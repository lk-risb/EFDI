"""Shared TAK Server TCP/TLS transport — used by any layer that streams CoT
or GeoChat events to TAK (tak_layer.py, tak_alert_layer.py, ...).
"""

import queue
import socket
import ssl
import threading

RECONNECT_S    = 5
SEND_TIMEOUT_S = 10
TAK_QUEUE_MAX  = 10000   # bounded CoT backlog; drops oldest when a link stalls


def _enable_keepalive(sock: socket.socket) -> None:
    """Make the OS notice a silently-dropped peer instead of leaving a half-open
    socket.

    Over a flaky mesh (e.g. a NetBird tunnel that dies mid-stream) TCP does not
    fail a write immediately: sendall() keeps succeeding into an unacknowledged
    send buffer and every CoT is lost with no error to trigger a reconnect. This
    arms two Linux guards so a dead peer surfaces as an error within ~20s:
      * SO_KEEPALIVE probes an *idle* connection (10s idle, 5s interval, 3 fails)
      * TCP_USER_TIMEOUT bounds *in-flight unACKed* data — the case that bit us,
        where the link drops while we are actively sending.
    Best-effort: options absent on non-Linux platforms are skipped.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 20000)
    except OSError:
        pass


class TcpSender:
    """CoT/GeoChat writer with a single-writer reconnect loop. Plaintext or mutual TLS.

    Accepts multiple (host, port) candidates — e.g. a LAN IP, a NetBird mesh
    IP, and a Tailscale mesh IP for one TAK Server — and rotates through them
    when a *connect* fails, converging on whichever path is reachable.

    All socket I/O runs on ONE background thread that owns the socket. Callers
    only enqueue; send() never touches the socket. That is deliberate: it closes
    the native use-after-free that core-dumped the process whenever the TAK link
    flapped — a zenoh callback thread doing an OpenSSL write on a socket another
    thread had just replaced mid-reconnect. An unreachable host now degrades to
    "drop events until reconnected" instead of crashing. The queue is bounded and
    drops the oldest on overflow: CoT/GeoChat is state, so a newer update
    supersedes a dropped one, and a stalled link never blocks the callbacks or
    grows memory.
    """

    def __init__(self, hosts: list[tuple[str, int]], tls: bool = False,
                 certfile: str | None = None, keyfile: str | None = None,
                 cafile: str | None = None, server_name: str | None = None):
        self.hosts     = hosts
        self._tls      = tls
        self._certfile = certfile
        self._keyfile  = keyfile
        self._cafile   = cafile
        self._server_name = server_name
        self.drop_tak_ingress = True
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=TAK_QUEUE_MAX)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tak-writer", daemon=True)
        self._thread.start()

    def send(self, xml: str) -> None:
        # Hand off to the writer thread; never blocks the caller. On overflow,
        # drop the oldest queued event so the freshest still gets through.
        while not self._stop.is_set():
            try:
                self._q.put_nowait(xml)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def _open(self, host: str, port: int) -> socket.socket:
        raw = socket.create_connection((host, port), timeout=SEND_TIMEOUT_S)
        _enable_keepalive(raw)
        if not self._tls:
            raw.settimeout(SEND_TIMEOUT_S)
            return raw
        # Dial and identity names are deliberately separate: redundant IP/DNS
        # paths may all reach one TAK server certificate. The configured TLS
        # server name must still match that certificate's SAN.
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self._cafile)
        ctx.check_hostname = True
        if self._certfile and self._keyfile:
            ctx.load_cert_chain(self._certfile, self._keyfile)
        s = ctx.wrap_socket(raw, server_hostname=self._server_name or host)
        s.settimeout(SEND_TIMEOUT_S)
        return s

    def _run(self) -> None:
        sock: socket.socket | None = None
        idx = 0
        while not self._stop.is_set():
            if sock is None:
                host, port = self.hosts[idx % len(self.hosts)]
                try:
                    sock = self._open(host, port)
                    print("TAK {} connected → {}:{}".format(
                        "TLS" if self._tls else "TCP", host, port), flush=True)
                except OSError as exc:
                    # Connect failed → this path is down; rotate to the next
                    # candidate and back off. One thread, so no reconnect storm.
                    print("TAK connect failed ({}:{}) — {}, next candidate in {}s".format(
                        host, port, exc, RECONNECT_S), flush=True)
                    idx += 1
                    self._stop.wait(RECONNECT_S)
                    continue
            try:
                xml = self._q.get(timeout=1.0)
            except queue.Empty:
                continue  # idle: loop back to re-check the stop flag
            try:
                sock.sendall((xml + "\n").encode("utf-8"))
            except OSError:
                # A failed write means the server closed an established stream,
                # not that the path is down — reconnect to the SAME candidate.
                # The candidates are alternate addresses of one TAK Server, which
                # identifies clients by certificate, so returning on a different
                # address would drop the prior session and churn. If the path is
                # genuinely down, the next connect fails and rotation happens there.
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                self._stop.wait(RECONNECT_S)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
