"""dup_report: finds backbone leaks, partner echoes, callsign twins and junk ids; EchoFilter drop regex."""

import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

import dup_report  # noqa: E402
from protocols.vendors.random import echo_filter  # noqa: E402


def _rec(uid, src, call=None, lat=50.0, lon=20.0):
    return {"uid": uid, "_src": src, "callsign": call, "lat_deg": lat, "lon_deg": lon}


def test_analyse_reports_each_kind_of_duplication():
    records = [
        ("ORG/land/dangausakis/airzone/neutral/zone/tracks/v1", _rec("DA-ZONE-UA-R:berdiansk", "dangausakis.lt", "UA Berdianskyi District RED")),
        ("ORG/land/backbone/unknown/unit/tracks/v1", _rec("BACKBONE-1", "backbone:json:x", "DA-ZONE-UA-R:berdiansk")),       # echo
        ("ORG/air/neptun/hostile/uav/tracks/v1", _rec("BACKBONE-2", "backbone:json:x", "Shahed")),                            # leak
        ("ORG/land/backbone/unknown/unit/tracks/v1", _rec("ROOT_ATTRIBUTES", "backbone:socbx:u")),                           # junk
        ("ORG/air/a/x/tracks/v1", _rec("EFDI-1", "adsb", "OMCBS", 49.2, 19.6)),
        ("ORG/air/b/x/tracks/v1", _rec("UUID-1", "adsb2", "omcbs", 49.4, 19.6)),                                              # twin
        ("ORG/air/c/x/tracks/v1", {"uid": "gone", "_delete": True}),
    ]
    found = dup_report.analyse(records)
    assert [x[1] for x in found["leaks"]] == ["BACKBONE-2"]
    assert [x[1] for x in found["echoes"]] == ["BACKBONE-1"]
    assert [x[1] for x in found["junk"]] == ["ROOT_ATTRIBUTES"]
    assert found["twins"] == [("OMCBS", ["EFDI-1", "UUID-1"])]
    assert found["sources"]["backbone:json:x"] == 2


def test_clean_fabric_reports_nothing():
    found = dup_report.analyse([("ORG/air/a/x/tracks/v1", _rec("EFDI-1", "adsb", "OMCBS"))])
    assert not (found["leaks"] or found["echoes"] or found["twins"] or found["junk"])


def test_echo_filter_drop_regex_and_own_topics(monkeypatch):
    monkeypatch.setenv("BACKBONE_DROP_REGEX", r"^synth:|demo-")
    reloaded = importlib.reload(echo_filter)
    try:
        echoes = reloaded.EchoFilter()
        assert echoes.is_echo({"uid": "SYNTH:900"}) and echoes.is_echo({"uid": "x", "callsign": "demo-81c7edd2"})
        assert not echoes.is_echo({"uid": "BACKBONE-1", "callsign": "OMCBS"})
        calls = []
        reloaded.subscribe_own(None, "ORG", echoes, lambda s, topic, cb: calls.append(topic) or topic)
        assert calls == ["ORG/land/**", "ORG/air/*/hostile/**", "ORG/air/*/trail/**"]
    finally:
        monkeypatch.delenv("BACKBONE_DROP_REGEX")
        importlib.reload(echo_filter)


def test_echo_filter_still_recognises_a_copy_of_a_track_of_ours_that_expired_an_hour_ago():
    import json
    from unittest import mock
    echoes = echo_filter.EchoFilter()
    ours = {"_src": "neptun.in.ua", "uid": "NEPTUN-trk_00258879", "callsign": "NEPTUN MISSILE Dnipro", "lat_deg": 48.46, "lon_deg": 35.04}
    echoes.on_sample(mock.Mock(payload=json.dumps(ours).encode()))
    copy = {"uid": "BACKBONE-DODE42DD", "callsign": "NEPTUN-trk_00258879"}
    assert echoes.is_echo(copy, now=__import__("time").time() + 3600)


def test_a_partner_copy_of_a_mapa_track_is_recognised_by_its_eleven_character_id():
    import json
    from unittest import mock
    echoes = echo_filter.EchoFilter()
    ours = {"_src": "mapa.ua", "uid": "MAPA-323725", "callsign": "MAPA MISSILE x2 Lutsk", "lat_deg": 51.0, "lon_deg": 25.0}
    echoes.on_sample(mock.Mock(payload=json.dumps(ours).encode()))
    # seen live: the soc-bx feed returns it as track:MAPA-323725 with a "(STALE)" callsign, drawn as an unknown ground unit
    copy = {"uid": "MAPA-323725", "label": "MAPA MISSILE x2 Lutsk (STALE)", "callsign": "MAPA MISSILE x2 Lutsk (STALE)"}
    assert echoes.is_echo(copy)
    assert not echoes.is_echo({"uid": "ABC-323725", "callsign": "UNRELATED"})
