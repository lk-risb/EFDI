"""Regression: cot_layer's TAK sender must survive connection churn.

The old TcpSender did native OpenSSL writes from the zenoh callback threads on a
socket that a concurrent reconnect could replace and free — a use-after-free
that core-dumped the whole process whenever the TAK link flapped (which the
NetBird lazy-connection mesh did constantly). This test reproduces the exact
path: a TAK peer that accepts then drops the connection, hammered by concurrent
senders while reconnect backoff is cranked down. A segfault would take the test
process down; the assertion just confirms the writer kept running.
"""
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "protocols"))
sys.path.insert(0, str(ROOT / "compose" / "layers"))

import cot_layer  # noqa: E402


def test_tcpsender_survives_reconnect_churn():
    cot_layer.RECONNECT_S = 0.02  # many reconnect cycles inside the window

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def server():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.recv(64)
            except OSError:
                pass
            conn.close()  # drop → client write fails → reconnect (the crash path)

    server_thread = threading.Thread(target=server, daemon=True)
    server_thread.start()

    sender = cot_layer.TcpSender([("127.0.0.1", port)], tls=False)
    deadline = time.time() + 1.5

    def hammer():
        while time.time() < deadline:
            sender.send("<event uid='x'/>")

    hammers = [threading.Thread(target=hammer) for _ in range(8)]
    for t in hammers:
        t.start()
    for t in hammers:
        t.join()

    writer_alive = sender._thread.is_alive()
    sender.close()
    stop.set()
    srv.close()

    assert writer_alive, "TAK writer thread died under reconnect churn"
