"""Fusion-level suppression of loose copies of ICAO-keyed aircraft."""

import json
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

from protocols.vendors.random.twin_suppress import TwinIndex, TwinSuppressions, twin_topic  # noqa: E402


def _keyed(**kw):
    return dict({"uid": "48A5C6", "icao24": "48a5c6", "callsign": "SPKOG", "lat_deg": 52.50, "lon_deg": 21.34}, **kw)


def _loose(**kw):
    return dict({"uid": "48A5C6", "callsign": "SPKOG", "lat_deg": 52.53, "lon_deg": 21.36}, **kw)


def test_loose_copy_is_announced_whichever_arrives_first():
    index = TwinIndex()
    assert index.observe(_keyed(), 100.0) == []
    assert index.observe(_loose(), 101.0) == ["48A5C6"]          # the keyed copy was seen first
    cold = TwinIndex()
    assert cold.observe(_loose(), 100.0) == []                     # a restarted decoder sends the loose copy first
    assert cold.observe(_keyed(), 101.0) == ["48A5C6"]


def test_address_may_come_from_a_word_prefix_or_the_callsign():
    index = TwinIndex()
    index.observe(_keyed(), 100.0)
    assert index.observe(_loose(uid="STATES:48A5C6", callsign="x"), 101.0) == ["STATES:48A5C6"]
    assert index.observe(_loose(uid="UUID-1", callsign="48A5C6"), 101.0) == ["UUID-1"]


def test_same_callsign_nearby_is_a_twin_but_far_away_or_other_callsign_is_not():
    index = TwinIndex()
    index.observe(_keyed(), 100.0)
    assert index.observe(_loose(uid="A-UUID", lat_deg=52.52), 101.0) == ["A-UUID"]
    assert index.observe(_loose(uid="B-UUID", lat_deg=54.0), 101.0) == []
    assert index.observe(_loose(uid="C-UUID", callsign="OTHER1"), 101.0) == []


def test_stale_keyed_track_and_unrelated_tracks_do_not_match():
    index = TwinIndex()
    index.observe(_keyed(), 100.0)
    assert index.observe(_loose(), 100.0 + 500) == []              # the keyed copy is long gone
    assert index.observe(_keyed(icao24="111111", uid="111111", callsign="ZZZ9"), 100.0 + 500) == []
    assert index.observe({"uid": "T-TRAIL", "lat_deg": 1, "lon_deg": 1}, 1.0) == []
    assert index.observe({"uid": "48A5C6", "_delete": True}, 1.0) == []
    assert index.observe(_loose(geometry={"type": "Polygon"}), 1.0) == []


def test_layer_holds_back_only_the_announced_loose_copy():
    held = TwinSuppressions(ttl_s=20.0)
    held.on_sample(mock.Mock(payload=json.dumps({"uid": "48A5C6"}).encode()))
    assert held.is_suppressed({"uid": "48A5C6", "callsign": "SPKOG"})
    assert not held.is_suppressed({"uid": "48A5C6", "icao24": "48a5c6"})   # the keyed copy is never held back
    assert not held.is_suppressed({"uid": "OTHER"})
    assert not held.is_suppressed({"uid": "48A5C6"}, now=10 ** 12)         # and it lapses
    held.on_sample(mock.Mock(payload=b"not json"))


def test_topic_is_a_safe_key_expression():
    assert twin_topic("ORG", "STATES:48 A5/C6") == "ORG/air/trackfusion/suppress/twin/STATES_48_A5_C6"
