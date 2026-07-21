"""Bounded, localhost control-plane regression tests."""

import pathlib
import subprocess
from unittest.mock import patch
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))

from admin_control import (  # noqa: E402
    CONFIG_VALIDATE_MAX_BYTES,
    LOG_TAIL_BYTES,
    _classify_sitaware_hq_nvg_health,
    _validate_router_config,
    _sign_csr,
    _tail_lines,
)


def nvg_health(
    *,
    successful: int = 0,
    unauthorized: int = 0,
    age: float | None = None,
    last_success: str | None = None,
    last_unauthorized: str | None = None,
) -> dict:
    return {
        "status": "ok",
        "tracks": 603,
        "feed_requests": {
            "successful_requests": successful,
            "unauthorized_requests": unauthorized,
            "last_successful_request": last_success,
            "last_unauthorized_request": last_unauthorized,
            "seconds_since_last_success": age,
        },
    }


def test_sitaware_hq_nvg_health_waits_for_first_client_pull():
    status, details = _classify_sitaware_hq_nvg_health(nvg_health())

    assert status == "waiting-for-client"
    assert details["tracks"] == 603
    assert details["successful_requests"] == 0


def test_sitaware_hq_nvg_health_reports_latest_auth_failure():
    status, details = _classify_sitaware_hq_nvg_health(
        nvg_health(
            successful=2,
            unauthorized=1,
            age=75.0,
            last_success="2026-07-21T05:00:00Z",
            last_unauthorized="2026-07-21T05:01:00Z",
        )
    )

    assert status == "auth-failed"
    assert details["unauthorized_requests"] == 1


def test_sitaware_hq_nvg_health_reports_connected_and_stale_clients():
    connected, _ = _classify_sitaware_hq_nvg_health(
        nvg_health(successful=1, age=10.0, last_success="2026-07-21T05:00:00Z")
    )
    stale, _ = _classify_sitaware_hq_nvg_health(
        nvg_health(successful=1, age=61.0, last_success="2026-07-21T05:00:00Z")
    )

    assert connected == "client-connected"
    assert stale == "client-stale"


def test_sitaware_hq_nvg_health_rejects_malformed_details():
    status, details = _classify_sitaware_hq_nvg_health(
        {"status": "ok", "tracks": "many", "feed_requests": {}}
    )

    assert status == "health-unavailable"
    assert details == {}


def test_log_tail_does_not_return_the_whole_file(tmp_path):
    path = tmp_path / "large.log"
    path.write_bytes(b"".join(f"line-{i}\n".encode() for i in range(100_000)))

    lines = _tail_lines(path)

    assert len(lines) == 200
    assert lines[0] == "line-99800"
    assert lines[-1] == "line-99999"
    assert LOG_TAIL_BYTES < path.stat().st_size


def test_router_config_preflight_passes_candidate_only_on_stdin():
    completed = type("Completed", (), {"returncode": 0, "stdout": b"details", "stderr": b""})()
    with patch("admin_control.subprocess.run", return_value=completed) as run:
        result = _validate_router_config('{mode:"router"}')

    assert result == {
        "ok": True,
        "returncode": 0,
        "output": "Zenoh 1.9.0 accepted the candidate configuration",
    }
    args = run.call_args.args[0]
    assert args[:5] == ["docker", "exec", "-i", "efdi-pod-zenoh-router", "/bin/sh"]
    assert '{mode:"router"}' not in args
    assert run.call_args.kwargs["input"] == b'{mode:"router"}'


def test_router_config_preflight_rejects_oversized_candidate_without_exec():
    with patch("admin_control.subprocess.run") as run:
        result = _validate_router_config("x" * (CONFIG_VALIDATE_MAX_BYTES + 1))

    assert result["ok"] is False
    assert result["returncode"] == 400
    run.assert_not_called()


def test_router_ca_signs_verified_p256_transport_csr(tmp_path, monkeypatch):
    ca_key = tmp_path / "ca-key.pem"
    ca_cert = tmp_path / "ca-cert.pem"
    leaf_key = tmp_path / "leaf-key.pem"
    leaf_csr = tmp_path / "leaf.csr"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", ca_key], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-key", ca_key, "-sha256", "-days", "1",
        "-subj", "/CN=test-router-ca", "-out", ca_cert,
    ], check=True)
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", leaf_key], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-key", leaf_key, "-subj", "/CN=child-1", "-out", leaf_csr,
    ], check=True)
    monkeypatch.setattr("admin_control.ROUTER_CA_KEY_PATH", ca_key)
    monkeypatch.setattr("admin_control.ROUTER_CA_CERT_PATH", ca_cert)
    monkeypatch.setattr("admin_control.ROUTER_CA_CHAIN_PATH", ca_cert)

    result = _sign_csr(leaf_csr.read_text(), "child-1", "transport", 0, 1)

    assert result["ok"] is True
    issued = tmp_path / "issued.pem"
    issued.write_text(result["certificate"])
    verification = subprocess.run(
        ["openssl", "verify", "-CAfile", ca_cert, issued],
        capture_output=True,
        text=True,
    )
    assert verification.returncode == 0, verification.stderr


def test_router_ca_rejects_csr_for_a_different_identity(tmp_path, monkeypatch):
    ca_key = tmp_path / "ca-key.pem"
    ca_cert = tmp_path / "ca-cert.pem"
    leaf_key = tmp_path / "leaf-key.pem"
    leaf_csr = tmp_path / "leaf.csr"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", ca_key], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-key", ca_key, "-sha256", "-days", "1",
        "-subj", "/CN=test-router-ca", "-out", ca_cert,
    ], check=True)
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", leaf_key], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-key", leaf_key, "-subj", "/CN=sibling", "-out", leaf_csr,
    ], check=True)
    monkeypatch.setattr("admin_control.ROUTER_CA_KEY_PATH", ca_key)
    monkeypatch.setattr("admin_control.ROUTER_CA_CERT_PATH", ca_cert)
    monkeypatch.setattr("admin_control.ROUTER_CA_CHAIN_PATH", ca_cert)

    result = _sign_csr(leaf_csr.read_text(), "child-1", "transport", 0, 1)

    assert result == {"ok": False, "output": "CSR subject must contain only the invited common name"}


def test_router_ca_enforces_issuer_path_length(tmp_path, monkeypatch):
    ca_key = tmp_path / "ca-key.pem"
    ca_cert = tmp_path / "ca-cert.pem"
    child_key = tmp_path / "child-key.pem"
    child_csr = tmp_path / "child.csr"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", ca_key], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-key", ca_key, "-sha256", "-days", "1",
        "-subj", "/CN=bounded-router-ca", "-addext", "basicConstraints=critical,CA:TRUE,pathlen:1",
        "-out", ca_cert,
    ], check=True)
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", child_key], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-key", child_key, "-subj", "/CN=efdi-ca-child", "-out", child_csr,
    ], check=True)
    monkeypatch.setattr("admin_control.ROUTER_CA_KEY_PATH", ca_key)
    monkeypatch.setattr("admin_control.ROUTER_CA_CERT_PATH", ca_cert)
    monkeypatch.setattr("admin_control.ROUTER_CA_CHAIN_PATH", ca_cert)

    rejected = _sign_csr(child_csr.read_text(), "efdi-ca-child", "router-ca", 1, 1)
    accepted = _sign_csr(child_csr.read_text(), "efdi-ca-child", "router-ca", 0, 1)

    assert rejected == {
        "ok": False,
        "output": "child CA delegation depth must be lower than the issuer certificate path length",
    }
    assert accepted["ok"] is True
