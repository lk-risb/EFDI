"""Radar <-> NEPTUN corroboration: fusion matches, the NEPTUN feed suppresses."""

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

from protocols.vendors.random import fusion  # noqa: E402
from bridges.vendors.dangausakis import neptun as bridge  # noqa: E402

NOW = time.time()


def _threat(uid="NEPTUN-a", lat=50.0, lon=30.0, heading=90, unc_m=4000, kind="uav"):
    return {uid: {"ts": NOW, "track": {"uid": uid, "type": kind, "lat_deg": lat, "lon_deg": lon,
                                        "heading_deg": heading, "position_uncertainty_m": unc_m}}}


def _radar(lat=50.0, lon=30.0, heading=95, **kw):
    r = {"radar_id": "R1", "lat_deg": lat, "lon_deg": lon, "heading_deg": heading}
    r.update(kw)
    return r


def _cache(**tracks):
    return {uid: {"track": t, "ts": NOW} for uid, t in tracks.items()}


def _m(radar, threats, cache=None, ident=None):
    kw = {"ident": ident} if ident else {}
    cache = _cache(R1=radar) if cache is None else cache
    return fusion.match_neptun(radar, "R1", cache, threats, NOW, **kw)


def test_nearby_heading_consistent_radar_track_matches():
    assert _m(_radar(lat=50.01), _threat()) == "NEPTUN-a"


def test_transponder_tracks_never_match():
    assert _m(_radar(icao24="33fd3d"), _threat()) is None


def test_too_far_or_wrong_heading_or_stale_do_not_match():
    assert _m(_radar(lat=50.5), _threat()) is None            # ~30 NM away
    assert _m(_radar(heading=270), _threat()) is None         # opposite course
    old = _threat()
    old["NEPTUN-a"]["ts"] = NOW - 3600
    assert _m(_radar(), old) is None


def test_missing_headings_and_speed_still_match_on_distance():
    assert _m(_radar(heading=None), _threat(heading=None)) == "NEPTUN-a"


def test_radius_is_clamped_between_min_and_max():
    assert _m(_radar(lat=50.01), _threat(unc_m=0)) == "NEPTUN-a"   # minimum radius still applies
    assert _m(_radar(lat=50.2), _threat(unc_m=500_000)) is None    # ~12 NM, beyond the cap


def test_speed_class_gate():
    uav = _threat()
    assert _m(_radar(speed_ms=50), uav) == "NEPTUN-a"
    assert _m(_radar(speed_ms=194), uav) == "NEPTUN-a"             # 700 km/h jet drone
    assert _m(_radar(speed_ms=250), uav) is None                   # airliner-speed contact is not a drone
    missile = {"NEPTUN-m": {"ts": NOW, "track": {**_threat("NEPTUN-m")["NEPTUN-m"]["track"], "type": "missile"}}}
    assert _m(_radar(speed_ms=250), missile) == "NEPTUN-m"
    assert _m(_radar(speed_ms=30), missile) is None                # too slow for a missile
    ballistic = {"NEPTUN-b": {"ts": NOW, "track": {**_threat("NEPTUN-b")["NEPTUN-b"]["track"], "type": "ballistic"}}}
    assert _m(_radar(speed_ms=1500), ballistic) == "NEPTUN-b"      # ballistic is far above the drone ceiling
    for kind in ("mig31k", "unknown"):                             # no speed class: gate does not apply
        other = {"NEPTUN-x": {"ts": NOW, "track": {**_threat("NEPTUN-x")["NEPTUN-x"]["track"], "type": kind}}}
        assert _m(_radar(speed_ms=500), other) == "NEPTUN-x"


def test_radar_track_fitting_two_threats_is_ambiguous():
    threats = {**_threat("NEPTUN-1", lat=50.002), **_threat("NEPTUN-2", lat=50.004)}
    assert _m(_radar(), threats) is None


def test_threat_fitting_two_radar_objects_is_ambiguous():
    r1, r2 = _radar(), _radar(lat=50.003, radar_id="R2")
    assert _m(r1, _threat(), cache=_cache(R1=r1, R2=r2)) is None


def test_handoff_duplicates_count_as_one_radar_object():
    r1, r2 = _radar(), _radar(lat=50.003, radar_id="R2")
    ident = lambda uid: "R1"   # both uids share one cross-radar primary
    assert _m(r1, _threat(), cache=_cache(R1=r1, R2=r2), ident=ident) == "NEPTUN-a"


def test_far_away_other_radar_track_does_not_block():
    r1, far = _radar(), _radar(lat=51.0, radar_id="R2")
    assert _m(r1, _threat(), cache=_cache(R1=r1, R2=far)) == "NEPTUN-a"


class FakeSession:
    def __init__(self):
        self.puts = []

    def put(self, key, payload, **_):
        self.puts.append((key, json.loads(payload)))


class Sample:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()


def _tracks():
    return [("efdi/air/neptun/hostile/uav", {"uid": "NEPTUN-a", "type": "uav", "lat_deg": 50.0, "lon_deg": 30.0})]


def test_bridge_retracts_once_then_stays_quiet_then_recovers(monkeypatch):
    published = []
    monkeypatch.setattr(bridge, "publish_dual", lambda s, p, t, c: published.append(t["uid"]))
    session, supp, retracted = FakeSession(), bridge.Suppression(ttl_s=30), set()

    bridge.publish_threats(session, _tracks(), supp, retracted)
    assert published == ["NEPTUN-a"] and session.puts == []

    supp.on_sample(Sample({"uid": "NEPTUN-a"}))
    bridge.publish_threats(session, _tracks(), supp, retracted)
    bridge.publish_threats(session, _tracks(), supp, retracted)
    assert published == ["NEPTUN-a"]                       # not published again
    assert len(session.puts) == 1                          # tombstone exactly once
    key, body = session.puts[0]
    assert key.endswith("/tracks/v1") and body["_delete"] and body["uid"] == "NEPTUN-a"

    supp._until["NEPTUN-a"] = 0                            # suppression lapses
    bridge.publish_threats(session, _tracks(), supp, retracted)
    assert published == ["NEPTUN-a", "NEPTUN-a"] and not retracted


class KeyedSample(Sample):
    def __init__(self, key, payload):
        super().__init__(payload)
        self.key_expr = key


def test_fuser_annotates_radar_track_and_announces_suppression_once():
    session = FakeSession()
    fuser = fusion.TrackFuser(session, verbose=False)
    threat = {"uid": "NEPTUN-a", "type": "uav", "locality": "Zgurivka", "lat_deg": 50.0,
              "lon_deg": 30.0, "heading_deg": 90, "position_uncertainty_m": 4000}
    fuser.on_neptun(KeyedSample("efdi/air/neptun/hostile/uav/uav/neptun-a/tracks/v1", threat))
    fuser.on_neptun(KeyedSample("efdi/air/neptun/hostile/uav/uav/neptun-a/sapient/tracks/v1", threat))  # other view ignored

    fused = _radar(lat=50.005)
    fuser._corroborate_with_neptun(fused, dict(fused))
    fuser._corroborate_with_neptun(dict(fused), dict(fused))   # throttled, no second announcement
    assert "Also reported by NEPTUN (uav Zgurivka)" in fused["remarks"]
    assert [k.rsplit("/", 1)[-1] for k, _ in session.puts] == ["NEPTUN-a"]
    assert session.puts[0][1]["radar_uid"] == "R1"

    fuser.on_neptun(KeyedSample("efdi/air/neptun/hostile/uav/uav/neptun-a/tracks/v1",
                                {"uid": "NEPTUN-a", "_delete": True}))
    clean = _radar(lat=50.005)
    fuser._corroborate_with_neptun(clean, dict(clean))
    assert "remarks" not in clean                               # tombstoned threat no longer matches
