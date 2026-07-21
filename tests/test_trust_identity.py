import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))

from api.trust_identity import bounded_common_name, child_namespace, router_identity  # noqa: E402


def test_managed_child_is_always_a_strict_namespace_descendant():
    assert child_namespace("LTU/CISB/hq", "branch") == "LTU/CISB/hq/branch"
    assert child_namespace("LTU/CISB/hq", "LTU/CISB/hq/branch") == "LTU/CISB/hq/branch"
    with pytest.raises(ValueError):
        child_namespace("LTU/CISB/hq", "LTU/foreign")


def test_identity_and_common_names_are_stable_and_bounded():
    identity = router_identity("LTU/CISB/hq/branch")
    assert identity == "spiffe://efdi.global/router/LTU/CISB/hq/branch"
    assert bounded_common_name("router", identity).startswith("efdi-router-")
    assert len(bounded_common_name("policy", identity)) < 64
