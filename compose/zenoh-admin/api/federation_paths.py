"""Bounded path lookup over the live, read-only topology store."""

import os

from .topology import _TOPOLOGY, _TOPOLOGY_LOCK


_OWN_NAMESPACE = os.environ.get("PARTNER_NAMESPACE", "")


def own_namespace() -> str:
    return _OWN_NAMESPACE


def path_to(target_namespace: str) -> list[str] | None:
    """Return descendants from the first child through the requested target.

    Parent pointers are walked upward and bounded by the current store size so
    malformed or spoofed topology can never loop. Topology only suggests a
    path; the caller and every relay hop must additionally prove that its next
    hop is a directly registered child before signing anything.
    """
    if target_namespace == _OWN_NAMESPACE:
        return []
    with _TOPOLOGY_LOCK:
        parents = {
            namespace: entry["fact"].get("parent_namespace")
            for namespace, entry in _TOPOLOGY.items()
            if entry["fact"].get("reported", True) is not False
        }
    chain: list[str] = []
    current: str | None = target_namespace
    for _ in range(len(parents) + 1):
        if current is None:
            return None
        chain.append(current)
        parent = parents.get(current)
        if parent == _OWN_NAMESPACE:
            chain.reverse()
            return chain
        current = parent if isinstance(parent, str) else None
    return None
