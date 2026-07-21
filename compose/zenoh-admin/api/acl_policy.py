"""Deterministic Zenoh 1.9 ACL compiler for direct managed peers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .trust_types import parse_key_scope


_ALL_MESSAGES = [
    "put", "delete", "declare_subscriber", "query", "declare_queryable", "reply",
    "liveliness_token", "liveliness_query", "declare_liveliness_subscriber",
]


@dataclass(frozen=True)
class PeerPolicy:
    grant_id: str
    identity_uri: str
    cert_common_name: str
    username: str
    relationship: str  # parent | child
    namespace: str
    publish: tuple[str, ...]
    subscribe: tuple[str, ...]
    quarantined: bool = False

    def __post_init__(self):
        if self.relationship not in {"parent", "child"}:
            raise ValueError("relationship must be parent or child")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", self.cert_common_name):
            raise ValueError("certificate common name is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.username):
            raise ValueError("link username is invalid")
        parse_key_scope(self.namespace)
        for expression in (*self.publish, *self.subscribe):
            parse_key_scope(expression)


def _stable_id(prefix: str, grant_id: str) -> str:
    digest = hashlib.sha256(grant_id.encode()).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _rule(identifier: str, messages: list[str], flows: list[str], key_exprs: list[str], permission="allow"):
    return {
        "id": identifier,
        "messages": messages,
        "flows": flows,
        "permission": permission,
        "key_exprs": sorted(set(key_exprs)),
    }


def compile_acl(
    *,
    local_data_scope: str,
    inbound_scope: str,
    federation_root: str,
    local_namespace: str,
    peers: list[PeerPolicy],
) -> dict:
    """Compile only direct, identity-bound peers plus the loopback subject."""
    parse_key_scope(local_data_scope)
    parse_key_scope(inbound_scope)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", federation_root):
        raise ValueError("federation root must be one literal segment")

    rules = [
        _rule("local-data", ["put", "delete", "declare_subscriber", "query", "declare_queryable", "reply"], ["ingress", "egress"], [local_data_scope]),
        _rule("local-inbound", ["put", "declare_subscriber", "query", "reply"], ["ingress", "egress"], [inbound_scope]),
        _rule("local-admin-read", ["query", "reply"], ["ingress", "egress"], ["@/**"]),
        _rule(
            "local-federation",
            ["put", "declare_subscriber"],
            ["ingress", "egress"],
            [
                f"{federation_root}/**/@config/v1",
                f"{federation_root}/**/@config/relay/v1",
                f"{federation_root}/**/@config/status/v1",
                f"{federation_root}/**/@topology/v1",
            ],
        ),
    ]
    subjects = [{"id": "local-tcp", "link_protocols": ["tcp"]}]
    policies = [{
        "id": "local-policy",
        "rules": [item["id"] for item in rules],
        "subjects": ["local-tcp"],
    }]

    seen_usernames = set()
    seen_cns = set()
    for peer in sorted(peers, key=lambda item: (item.relationship, item.identity_uri)):
        if peer.relationship == "child" and peer.username in seen_usernames:
            raise ValueError("link username is assigned to more than one child")
        if peer.cert_common_name in seen_cns:
            raise ValueError("certificate common name is assigned to more than one direct peer")
        if peer.relationship == "child":
            seen_usernames.add(peer.username)
        seen_cns.add(peer.cert_common_name)
        stem = _stable_id("peer", peer.grant_id)
        subject_id = stem + "-identity"
        subject = {
            "id": subject_id,
            "cert_common_names": [peer.cert_common_name],
        }
        # A connector presents its own username to the listener. The listener
        # therefore identifies a child by certificate AND username. On the
        # connector side the parent presents only its mTLS server certificate;
        # reusing the child's outgoing username here creates an impossible
        # subject that denies every parent message.
        if peer.relationship == "child":
            subject["usernames"] = [peer.username]
        subjects.append(subject)
        if peer.quarantined:
            deny_id = stem + "-quarantine"
            rules.append(_rule(deny_id, _ALL_MESSAGES, ["ingress", "egress"], ["**"], "deny"))
            policies.append({"id": stem + "-policy", "rules": [deny_id], "subjects": [subject_id]})
            continue

        peer_rule_ids = []
        if peer.relationship == "child":
            definitions = [
                ("publish", ["put", "delete"], ["ingress"], list(peer.publish)),
                ("publish-interest", ["declare_subscriber", "query"], ["egress"], list(peer.publish)),
                ("subscribe-declare", ["declare_subscriber", "query"], ["ingress"], list(peer.subscribe)),
                ("subscribe-deliver", ["put", "reply"], ["egress"], list(peer.subscribe)),
                ("control-in", ["put"], ["ingress"], [
                    f"{peer.namespace.removesuffix('/**')}/@config/status/v1",
                    f"{peer.namespace.removesuffix('/**')}/@topology/v1",
                ]),
                ("control-out", ["put", "declare_subscriber"], ["egress"], [
                    f"{peer.namespace.removesuffix('/**')}/@config/v1",
                    f"{peer.namespace.removesuffix('/**')}/@config/relay/v1",
                ]),
            ]
        else:
            definitions = [
                ("own-publish", ["put", "delete"], ["egress"], list(peer.publish)),
                ("own-publish-interest", ["declare_subscriber", "query"], ["ingress"], list(peer.publish)),
                ("approved-in", ["put", "reply"], ["ingress"], list(peer.subscribe)),
                ("approved-declare", ["declare_subscriber", "query"], ["egress"], list(peer.subscribe)),
                ("control-in", ["put", "declare_subscriber"], ["ingress"], [
                    f"{local_namespace}/@config/v1",
                    f"{local_namespace}/@config/relay/v1",
                ]),
                ("control-out", ["put"], ["egress"], [
                    f"{local_namespace}/@config/status/v1",
                    f"{local_namespace}/@topology/v1",
                ]),
            ]
        for suffix, messages, flows, scopes in definitions:
            if not scopes:
                continue
            rule_id = f"{stem}-{suffix}"
            rules.append(_rule(rule_id, messages, flows, scopes))
            peer_rule_ids.append(rule_id)
        policies.append({"id": stem + "-policy", "rules": peer_rule_ids, "subjects": [subject_id]})

    access_control = {
        "enabled": True,
        "default_permission": "deny",
        "rules": rules,
        "subjects": subjects,
        "policies": policies,
    }
    canonical = json.dumps(access_control, sort_keys=True, separators=(",", ":"))
    return {
        "access_control": access_control,
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "canonical_json": canonical,
    }
