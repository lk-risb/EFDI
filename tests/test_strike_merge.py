"""Fusion merges strike markers: one attack, one marker, whichever feed reports it."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))

from protocols.vendors.random.fusion import StrikeIndex  # noqa: E402

GC = "O/land/geoconfirmed/event/hostile/incident/event/{}/tracks/v1"
DSM = "O/land/dronestrikemap/strike/hostile/incident/x/{}/tracks/v1"


def _track(uid, lat, lon, uncertainty, day="2026-10-12", **extra):
    return dict({"uid": uid, "type": "event", "lat_deg": lat, "lon_deg": lon, "position_uncertainty_m": uncertainty,
                 "event_date": day}, **extra)


def test_two_reports_of_one_attack_leave_the_more_precise_marker():
    index = StrikeIndex()
    assert index.observe(_track("DSM-1", 48.0, 37.0, 5000), 100.0, DSM.format("DSM-1")) == []
    out = index.observe(_track("GC-A", 48.0005, 37.0, 100), 101.0, GC.format("GC-A"))     # ~55 m away, more precise
    assert out == [("DSM-1", DSM.format("DSM-1"), "event")] or [u for u, _, _ in out] == ["DSM-1"]
    # the town-centroid marker, seen again at the next poll, is retracted again
    assert [u for u, _, _ in index.observe(_track("DSM-1", 48.0, 37.0, 5000), 1000.0, DSM.format("DSM-1"))] == ["DSM-1"]


def test_nothing_is_merged_that_is_far_apart_on_another_day_on_another_side_or_no_strike():
    index = StrikeIndex()
    index.observe(_track("GC-A", 48.0, 37.0, 100), 1.0, GC.format("GC-A"))
    assert index.observe(_track("GC-B", 48.002, 37.0, 100), 2.0, GC.format("GC-B")) == []                    # 220 m
    assert index.observe(_track("GC-C", 48.0, 37.0, 100, day="2026-10-11"), 3.0, GC.format("GC-C")) == []   # another day
    assert index.observe(_track("GC-D", 48.0, 37.0, 100), 4.0, GC.format("GC-D").replace("hostile", "friendly")) == []
    assert index.observe(_track("GC-E", 48.0, 37.0, 100, role="other"), 5.0, GC.format("GC-E")) == []       # a tank, no strike
    assert index.observe(_track("X-1", 48.0, 37.0, 100), 6.0, "O/air/other/hostile/unit/x/tracks/v1") == []


def test_three_reports_of_one_attack_leave_one_and_a_deleted_marker_leaves_the_group():
    index = StrikeIndex()
    index.observe(_track("GC-A", 48.0, 37.0, 100), 1.0, GC.format("GC-A"))
    index.observe(_track("GC-B", 48.0003, 37.0, 100), 2.0, GC.format("GC-B"))
    out = index.observe(_track("GC-C", 48.0, 37.0004, 100), 3.0, GC.format("GC-C"))
    assert sorted(u for u, _, _ in out) == ["GC-B", "GC-C"]
    index.observe({"uid": "GC-A", "_delete": True}, 4.0, GC.format("GC-A"))
    assert [u for u, _, _ in index.observe(_track("GC-C", 48.0, 37.0004, 100), 5.0, GC.format("GC-C"))] == ["GC-C"]


def test_fusion_announces_a_military_aircraft_so_the_layers_hold_back_its_civil_copies():
    import json
    from unittest import mock
    from protocols.vendors.random import fusion
    fuser = fusion.TrackFuser.__new__(fusion.TrackFuser)        # only what on_any_track uses
    fuser._lock, fuser._session, fuser._verbose = fusion.threading.RLock(), mock.Mock(), False
    fuser._twins, fuser._strikes, fuser._twin_sent = fusion.TwinIndex(), fusion.StrikeIndex(), {}

    def sample(topic, track):
        return mock.Mock(key_expr=topic, payload=json.dumps(track).encode())
    root = fusion.TOPIC_ROOT
    fuser.on_any_track(sample(root + "/air/backbone/civ/aircraft/tracks/v1", {"uid": "AE07A7", "icao24": "ae07a7", "lat_deg": 46.0, "lon_deg": 22.0}))
    assert not [c for c in fuser._session.put.call_args_list if "suppress/civil" in c.args[0]]     # civil traffic announces nothing
    fuser.on_any_track(sample(root + "/air/dangausakis/adsb/friendly/aircraft/tracks/v1", {"icao24": "ae07a7", "lat_deg": 46.0, "lon_deg": 22.0}))
    (call,) = [c for c in fuser._session.put.call_args_list if "suppress/civil" in c.args[0]]
    assert call.args[0] == fusion.civil_topic(root, "AE07A7") and json.loads(call.args[1])["icao24"] == "ae07a7"


def test_a_hostile_strike_told_only_by_its_place_lands_friendly_in_occupied_territory():
    import json
    from unittest import mock
    from protocols.vendors.random import fusion
    fuser = fusion.TrackFuser.__new__(fusion.TrackFuser)
    fuser._lock, fuser._session, fuser._verbose = fusion.threading.RLock(), mock.Mock(), False
    fuser._twins, fuser._strikes, fuser._twin_sent = fusion.TwinIndex(), fusion.StrikeIndex(), {}
    fuser._occupied, fuser._occupied_seen = [], 0.0
    root = fusion.TOPIC_ROOT
    topic = root + "/land/geoconfirmed/event/hostile/incident/event/GC-AAAA/tracks/v1"

    def sample(key, track):
        return mock.Mock(key_expr=key, payload=json.dumps(track).encode())
    track = {"uid": "GC-AAAA", "type": "event", "lat_deg": 47.5, "lon_deg": 37.5, "side_basis": "place", "remarks": "x"}
    fuser.on_any_track(sample(topic, track))
    assert not fuser._session.put.called                                           # no occupied territory known yet: left hostile
    fuser.on_occupied(sample(fusion.OCCUPIED_TOPIC, {"rings": [[[36.0, 47.0], [39.0, 47.0], [39.0, 49.0], [36.0, 49.0], [36.0, 47.0]]]}))
    fuser.on_any_track(sample(topic, dict(track, lat_deg=51.0)))                  # outside the ring: stays hostile
    assert not fuser._session.put.called
    fuser.on_any_track(sample(topic, track))
    keys = {c.args[0]: json.loads(c.args[1]) for c in fuser._session.put.call_args_list}
    moved = next(v for k, v in keys.items() if "/friendly/incident/" in k)
    assert moved["uid"] == "GC-AAAA-UA" and moved["_fused"] and "Ukrainian strike" in moved["remarks"]
    assert keys[topic]["_delete"] and keys[topic]["uid"] == "GC-AAAA"              # the hostile copy is retracted
    fuser._session.put.reset_mock()
    fuser.on_any_track(sample(next(k for k in keys if "/friendly/" in k), moved))   # the moved copy is not moved again
    assert not fuser._session.put.called
