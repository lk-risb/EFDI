"""Covers the antenna-site identity stabilization fix: RTSA-Suite PRO
reassigns a fresh antennaID to the same physical IsoLOG antenna across
samples (confirmed live — a single stationary antenna showed as ~10 stacked,
identically-named CoT markers in WinTAK), the same churn problem already
documented and fixed for drone trackIDs via _class_identity. _stable_site_uid
applies the same fix by proximity instead of by _entity_kind, since a site
has no entity kind."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "compose"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "compose" / "control"))

from protocols.vendors.aartos.aartos_json import (  # noqa: E402
    _site_identity,
    _stable_site_uid,
)


def setup_function(_):
    _site_identity.clear()


def test_same_antenna_churning_antenna_id_keeps_same_uid():
    uid1 = _stable_site_uid("host-a", "antenna-id-1", 54.6872, 25.2797)
    uid2 = _stable_site_uid("host-a", "antenna-id-2", 54.68721, 25.27971)
    uid3 = _stable_site_uid("host-a", "antenna-id-3", 54.6872, 25.2797)
    assert uid1 == uid2 == uid3


def test_first_call_mints_a_host_scoped_uid_from_the_given_antenna_id():
    uid = _stable_site_uid("host-a", "isolog-42", 54.6872, 25.2797)
    assert uid == "aartos-host-a-isolog-42"


def test_genuinely_different_antennas_get_different_uids():
    uid1 = _stable_site_uid("host-a", "antenna-1", 54.6872, 25.2797)
    uid2 = _stable_site_uid("host-a", "antenna-2", 54.7500, 25.4000)
    assert uid1 != uid2


def test_identity_is_scoped_per_host():
    uid1 = _stable_site_uid("host-a", "antenna-1", 54.6872, 25.2797)
    uid2 = _stable_site_uid("host-b", "antenna-1", 54.6872, 25.2797)
    assert uid1 != uid2


def test_position_just_outside_the_tolerance_radius_is_a_new_identity():
    uid1 = _stable_site_uid("host-a", "antenna-1", 54.6872, 25.2797)
    # ~0.001 deg longitude at this latitude is roughly 65m — well outside
    # the 15m stabilization radius, so this must NOT be folded into uid1.
    uid2 = _stable_site_uid("host-a", "antenna-2", 54.6872, 25.2807)
    assert uid1 != uid2
