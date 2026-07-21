"""Managed-router config activation and hierarchy path regression tests."""

import os
import pathlib
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")
os.environ.setdefault("ZENOH_ADMIN_SECRET_KEY", "test-secret")
os.environ.setdefault("PARTNER_NAMESPACE", "hq")

from api import config, federation_apply, federation_paths, topology  # noqa: E402


def fields(namespace: str = "hq") -> config.ConfigFields:
    return config.ConfigFields(
        mtls_port=7447,
        local_tcp_port=7448,
        fabric_endpoint="",
        fabric_endpoints=[],
        fabric_tls_profile="efdi",
        partner_namespace=namespace,
        inbound_namespace=namespace,
        namespace_prefix="LTU/CISB",
        publish_prefix="LTU/CISB",
        verify_name_on_connect=False,
        plugins_loading_enabled=True,
    )


def configure_state_paths(monkeypatch, tmp_path):
    current = tmp_path / "config.json5"
    current.write_text("old-config")
    prefix = tmp_path / "namespace-prefix"
    prefix.write_text("LTU/CISB\n")
    data_prefix = tmp_path / "data-prefix"
    data_prefix.write_text("LTU/CISB\n")
    monkeypatch.setattr(config, "CONFIG_PATH", str(current))
    monkeypatch.setattr(config, "_LAST_KNOWN_GOOD_PATH", str(tmp_path / "config.last-known-good"))
    monkeypatch.setattr(config, "_PREFIX_FILE", str(prefix))
    monkeypatch.setattr(config, "_DATA_PREFIX_FILE", str(data_prefix))
    monkeypatch.setattr(config, "validate_rendered_config", lambda _: (True, "accepted"))
    monkeypatch.setattr(config, "restart_native_processes", lambda: [])
    return current


def test_safe_apply_activates_only_after_preflight_and_health(monkeypatch, tmp_path):
    current = configure_state_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "restart_router_container", lambda: (True, None))
    monkeypatch.setattr(config, "wait_for_router_health", lambda: True)

    result = config.apply_rendered_config("new-config", fields(), restart_native=True)

    assert result["status"] == "applied"
    assert current.read_text() == "new-config"
    assert (tmp_path / "config.last-known-good").read_text() == "old-config"


def test_safe_apply_restores_last_known_good_when_candidate_is_unhealthy(monkeypatch, tmp_path):
    current = configure_state_paths(monkeypatch, tmp_path)
    health_results = iter([False, True])
    monkeypatch.setattr(config, "restart_router_container", lambda: (True, None))
    monkeypatch.setattr(config, "wait_for_router_health", lambda: next(health_results))

    result = config.apply_rendered_config("bad-config", fields(), restart_native=True)

    assert result["status"] == "rolled_back"
    assert result["rolled_back"] is True
    assert current.read_text() == "old-config"


def test_federated_apply_rolls_back_when_management_link_does_not_recover(monkeypatch, tmp_path):
    current = configure_state_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "restart_router_container", lambda: (True, None))
    monkeypatch.setattr(config, "wait_for_router_health", lambda: True)
    monkeypatch.setattr(config, "wait_for_remote_router_link", lambda _: False)

    result = config.apply_rendered_config(
        "isolated-config",
        fields(),
        restart_native=True,
        preserve_management=True,
    )

    assert result["status"] == "rolled_back"
    assert "remote management link" in result["error"]
    assert current.read_text() == "old-config"


def test_federated_candidate_cannot_replace_identity_or_every_uplink(monkeypatch, tmp_path):
    current_path = tmp_path / "current.json5"
    current_path.write_text("current")
    active = fields("hq")
    active.fabric_endpoint = "tls/parent-a:7447"
    active.fabric_endpoints = ["tls/parent-a:7447"]
    monkeypatch.setattr(federation_apply, "CONFIG_PATH", str(current_path))
    monkeypatch.setattr(federation_apply, "_extract_fields", lambda _: active)

    wrong_identity = active.model_copy(update={"partner_namespace": "sibling"})
    disconnected = active.model_copy(update={
        "fabric_endpoint": "tls/parent-b:7447",
        "fabric_endpoints": ["tls/parent-b:7447"],
    })
    staged = active.model_copy(update={
        "fabric_endpoints": ["tls/parent-a:7447", "tls/parent-b:7447"],
    })

    assert "partner namespace" in federation_apply._federated_candidate_error(wrong_identity)
    assert "must be staged" in federation_apply._federated_candidate_error(disconnected)
    assert federation_apply._federated_candidate_error(staged) is None


def test_path_lookup_returns_only_a_bounded_descendant_chain():
    now = time.monotonic()
    with topology._TOPOLOGY_LOCK:
        topology._TOPOLOGY.clear()
        topology._TOPOLOGY.update({
            "child": {"fact": {"parent_namespace": "hq"}, "last_seen": now},
            "grandchild": {"fact": {"parent_namespace": "child"}, "last_seen": now},
            "foreign": {"fact": {"parent_namespace": None}, "last_seen": now},
        })

    assert federation_paths.path_to("child") == ["child"]
    assert federation_paths.path_to("grandchild") == ["child", "grandchild"]
    assert federation_paths.path_to("foreign") is None


def test_path_lookup_rejects_cycles():
    now = time.monotonic()
    with topology._TOPOLOGY_LOCK:
        topology._TOPOLOGY.clear()
        topology._TOPOLOGY.update({
            "a": {"fact": {"parent_namespace": "b"}, "last_seen": now},
            "b": {"fact": {"parent_namespace": "a"}, "last_seen": now},
        })

    assert federation_paths.path_to("a") is None


def test_zenoh_19_router_record_reports_only_router_links():
    neighbors = topology._neighbors_from_router_record({
        "sessions": [
            {
                "peer": "aabbcc",
                "whatami": "router",
                "links": [{"src": "tls/10.0.0.1:7447", "dst": "tls/10.0.0.2:7447"}],
            },
            {
                "peer": "ddeeff",
                "whatami": "client",
                "links": [{"src": "tcp/127.0.0.1:7448", "dst": "tcp/127.0.0.1:40000"}],
            },
        ],
    })

    assert neighbors == [{
        "router_zid": "aabbcc",
        "whatami": "router",
        "link_count": 1,
        "protocols": ["tls"],
    }]
