"""Fabric endpoint reachability status regression tests."""

import os
import pathlib
import socket
import sys
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")
os.environ.setdefault("ZENOH_ADMIN_SECRET_KEY", "test-secret")

from api import status  # noqa: E402


_ADDRESS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.10", 7447))]


def test_endpoint_is_green_only_for_established_zenoh_transport(monkeypatch):
    monkeypatch.setattr(status.socket, "getaddrinfo", lambda *_args, **_kwargs: _ADDRESS)

    result = status._endpoint_status(
        "tls/router.example:7447",
        {("100.64.0.10", 7447)},
    )

    assert result["state"] == "connected"
    assert "Zenoh transport" in result["detail"]


def test_reachable_port_without_zenoh_transport_is_yellow(monkeypatch):
    probe = mock.MagicMock()
    probe.__enter__.return_value = probe
    monkeypatch.setattr(status.socket, "getaddrinfo", lambda *_args, **_kwargs: _ADDRESS)
    monkeypatch.setattr(status.socket, "socket", lambda *_args, **_kwargs: probe)

    result = status._endpoint_status("tls/router.example:7447", set())

    assert result["state"] == "degraded"
    probe.connect.assert_called_once_with(("100.64.0.10", 7447))


def test_unreachable_endpoint_is_red(monkeypatch):
    probe = mock.MagicMock()
    probe.__enter__.return_value = probe
    probe.connect.side_effect = ConnectionRefusedError("refused")
    monkeypatch.setattr(status.socket, "getaddrinfo", lambda *_args, **_kwargs: _ADDRESS)
    monkeypatch.setattr(status.socket, "socket", lambda *_args, **_kwargs: probe)

    result = status._endpoint_status("tls/router.example:7447", set())

    assert result["state"] == "disconnected"
