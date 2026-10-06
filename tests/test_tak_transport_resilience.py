"""TcpSender: the writer thread survives unexpected errors and is revived if it ever stops."""

import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

from protocols.vendors.random import tak_transport  # noqa: E402


class _Sock:
    def __init__(self, fail_first=0):
        self.sent, self.fail, self.closed = [], fail_first, False

    def sendall(self, data):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("not an OSError")
        self.sent.append(data)

    def close(self):
        self.closed = True


def _wait(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_unexpected_connect_and_write_errors_do_not_kill_the_writer(monkeypatch):
    monkeypatch.setattr(tak_transport, "RECONNECT_S", 0.05)
    socks = [None, _Sock(fail_first=1), _Sock()]            # 1st connect raises ValueError, 2nd write raises RuntimeError
    opened = []

    def fake_open(self, host, port):
        item = socks[len(opened)]
        opened.append(item)
        if item is None:
            raise ValueError("bad certificate path")
        return item

    monkeypatch.setattr(tak_transport.TcpSender, "_open", fake_open)
    sender = tak_transport.TcpSender([("h", 1)])
    try:
        sender.send("<a/>")
        sender.send("<b/>")
        assert _wait(lambda: socks[2].sent), "writer never reconnected after the errors"
        assert socks[1].closed and sender._thread.is_alive()
    finally:
        sender.close()


def test_send_restarts_a_dead_writer_thread(monkeypatch):
    sock = _Sock()
    monkeypatch.setattr(tak_transport.TcpSender, "_open", lambda self, h, p: sock)
    sender = tak_transport.TcpSender([("h", 1)])
    try:
        sender._stop.set()
        sender._thread.join(2)
        sender._stop.clear()
        assert not sender._thread.is_alive()
        sender.send("<c/>")
        assert _wait(lambda: sock.sent), "a stopped writer was not revived by send()"
    finally:
        sender.close()
