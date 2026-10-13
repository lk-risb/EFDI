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
