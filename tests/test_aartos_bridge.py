import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
sys.path.insert(0, str(COMPOSE))
sys.path.insert(0, str(COMPOSE / "control"))

spec = importlib.util.spec_from_file_location(
    "aartos_bridge",
    COMPOSE / "bridges" / "aartos_bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bridge)


class _FakeResponse:
    def __init__(self, status=200, body=b"null"):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    """Stands in for http.client.HTTPConnection — records how many times
    it was constructed (one per real TCP connect) versus how many requests
    were made on it, so a test can tell a reused connection from a fresh
    one per poll."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.requests = 0
        self.closed = False
        self.fail_on_request = None
        _FakeConn.instances.append(self)

    def request(self, method, path):
        self.requests += 1
        if self.fail_on_request == self.requests:
            raise ConnectionError("simulated request failure")

    def getresponse(self):
        return _FakeResponse()

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self):
        self.puts = []

    def put(self, key, payload, encoding=None):
        self.puts.append((key, payload, encoding))

    def close(self):
        pass


def _poll_args(**overrides):
    base = dict(host="1.2.3.4", port=54663, mode="poll", poll_interval=0.1,
                timeout=5.0, verbose=False, limit=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def _run_for_n_sleeps(monkeypatch, args, n):
    """time.sleep() is the loop's only yield point per iteration in poll
    mode — raising KeyboardInterrupt from it after n calls terminates
    run()'s otherwise-infinite loop the same way Ctrl-C would, and run()
    already handles that exception as a clean shutdown."""
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= n:
            raise KeyboardInterrupt

    monkeypatch.setattr(bridge.time, "sleep", fake_sleep)
    monkeypatch.setattr(bridge, "open_session", lambda: _FakeSession())
    monkeypatch.setattr(bridge.http.client, "HTTPConnection", _FakeConn)
    _FakeConn.instances = []
    bridge.run(args)


def test_poll_mode_reuses_one_connection_across_successful_polls(monkeypatch):
    _run_for_n_sleeps(monkeypatch, _poll_args(), n=4)

    assert len(_FakeConn.instances) == 1
    conn = _FakeConn.instances[0]
    assert conn.requests == 4
    assert conn.closed is True  # closed once, cleanly, in run()'s finally


def test_poll_mode_reconnects_only_after_a_request_error(monkeypatch):
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= 4:
            raise KeyboardInterrupt

    def failing_conn(host, port, timeout=None):
        conn = _FakeConn(host, port, timeout)
        if len(_FakeConn.instances) == 1:
            conn.fail_on_request = 2  # first connection fails on its 2nd request
        return conn

    monkeypatch.setattr(bridge.time, "sleep", fake_sleep)
    monkeypatch.setattr(bridge, "open_session", lambda: _FakeSession())
    monkeypatch.setattr(bridge.http.client, "HTTPConnection", failing_conn)
    _FakeConn.instances = []

    bridge.run(_poll_args())

    # One connection failed and was replaced — never more than one live
    # connection is opened purely because of the passage of time/successful
    # polls, only because of an actual error.
    assert len(_FakeConn.instances) == 2
    assert _FakeConn.instances[0].closed is True
    assert _FakeConn.instances[1].closed is True


def test_poll_mode_publishes_non_null_samples_on_the_reused_connection(monkeypatch):
    class OneRealSampleConn(_FakeConn):
        def getresponse(self):
            if self.requests == 1:
                return _FakeResponse(body=b'{"data": "x"}')
            return _FakeResponse(body=b"null")

    def stop_after_first_iteration(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge.time, "sleep", stop_after_first_iteration)
    session = _FakeSession()
    monkeypatch.setattr(bridge, "open_session", lambda: session)
    monkeypatch.setattr(bridge.http.client, "HTTPConnection", OneRealSampleConn)
    _FakeConn.instances = []

    bridge.run(_poll_args())

    assert len(session.puts) == 1
    assert session.puts[0][1] == b'{"data": "x"}'
