import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))

from api.acl_policy import PeerPolicy, compile_acl  # noqa: E402


def peer(**updates):
    values = {
        "grant_id": "11111111-1111-1111-1111-111111111111",
        "identity_uri": "spiffe://efdi.global/router/LTU/CISB/child",
        "cert_common_name": "efdi-router-child",
        "username": "link-child",
        "relationship": "child",
        "namespace": "CISB/child/**",
        "publish": ("LTU/CISB/child/**",),
        "subscribe": ("LTU/CISB/approved/**",),
    }
    values.update(updates)
    return PeerPolicy(**values)


def compile_with(peers):
    return compile_acl(
        local_data_scope="LTU/CISB/hq/**",
        inbound_scope="LTU/CISB/inbound/**",
        federation_root="LTU",
        local_namespace="LTU/CISB/hq",
        peers=peers,
    )


def test_compiler_binds_cn_and_username_and_has_no_blanket_tls_subject():
    result = compile_with([peer()])
    subjects = result["access_control"]["subjects"]
    assert {"id": "local-tcp", "link_protocols": ["tcp"]} in subjects
    identity = next(item for item in subjects if item["id"] != "local-tcp")
    assert identity["cert_common_names"] == ["efdi-router-child"]
    assert identity["usernames"] == ["link-child"]
    assert all(item.get("link_protocols") != ["tls"] for item in subjects)
    assert len(result["sha256"]) == 64


def test_parent_connector_is_certificate_pinned_and_publish_interests_are_reciprocal():
    result = compile_with([peer(relationship="parent")])
    identity = next(
        item for item in result["access_control"]["subjects"]
        if item["id"] != "local-tcp"
    )
    assert identity["cert_common_names"] == ["efdi-router-child"]
    assert "usernames" not in identity
    rules = result["access_control"]["rules"]
    interest = next(item for item in rules if item["id"].endswith("own-publish-interest"))
    assert interest["messages"] == ["declare_subscriber", "query"]
    assert interest["flows"] == ["ingress"]


def test_quarantine_emits_only_explicit_deny_for_peer():
    result = compile_with([peer(quarantined=True)])
    peer_policy = next(item for item in result["access_control"]["policies"] if item["id"] != "local-policy")
    rule = next(item for item in result["access_control"]["rules"] if item["id"] == peer_policy["rules"][0])
    assert rule["permission"] == "deny"
    assert rule["key_exprs"] == ["**"]


def test_duplicate_direct_identity_or_username_is_rejected():
    with pytest.raises(ValueError, match="username"):
        compile_with([peer(), peer(
            grant_id="22222222-2222-2222-2222-222222222222",
            identity_uri="spiffe://efdi.global/router/LTU/CISB/other",
            cert_common_name="efdi-router-other",
        )])


def test_compiler_is_deterministic_independent_of_peer_order():
    second = peer(
        grant_id="22222222-2222-2222-2222-222222222222",
        identity_uri="spiffe://efdi.global/router/LTU/CISB/other",
        cert_common_name="efdi-router-other",
        username="link-other",
        namespace="CISB/other/**",
        publish=("LTU/CISB/other/**",),
    )
    assert compile_with([peer(), second])["sha256"] == compile_with([second, peer()])["sha256"]
