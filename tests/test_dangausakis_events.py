"""dangausakis community data (drone reports, incident markers) and the events the bridge
sends to tak_alert_layer: status changes, data-source warnings, new reports."""

import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "layers" / "vendors" / "tak"))
sys.path.insert(0, str(ROOT / "compose" / "bridges" / "vendors" / "dangausakis"))

from bridges.vendors.dangausakis import alert_sources as src  # noqa: E402
import dangausakis_bridge as bridge  # noqa: E402
import tak_alert_layer  # noqa: E402
import tak_layer  # noqa: E402

NOW = time.time()
RID = "a" * 32


def _iso(offset_s):
    return datetime.fromtimestamp(NOW + offset_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reports(*reports):
    return {"serverTime": _iso(0), "lifetimeSeconds": 1200, "reports": list(reports)}


def _report(rid=RID, expires=600, **extra):
    return {"id": rid, "latitude": 54.7, "longitude": 25.3, "expiresAt": _iso(expires),
            "reportType": "heard", **extra}


def _incident(iid="inc-1234", expires=3600, **extra):
    return {"id": iid, "title": "Drone debris found", "latitude": 54.9, "longitude": 24.0,
            "url": "https://dangausakis.lt/naujiena/x/", "is_approximate": True,
            "published_at": _iso(expires - 86400), "expires_at": _iso(expires), **extra}


def test_drone_report_becomes_an_uncertain_unverified_uav_track():
    (uid, track), = src.drone_report_tracks(_reports(_report()), NOW).items()
    assert uid == "DA-REPORT-" + RID[:16] == track["uid"]
    assert (track["lat_deg"], track["lon_deg"], track["target_type"]) == (54.7, 25.3, "uav")
    assert track["position_uncertainty_m"] == 1000 and "unverified" in track["remarks"]
    assert tak_layer._unknown_air_type(track) == "a-u-A-M-F-Q"       # a drone symbol, not a civil aircraft


def test_drone_reports_skip_expired_malformed_and_garbage():
    data = _reports(_report(expires=-5), _report("short"), _report("b" * 32, latitude="x"),
                    _report("c" * 32, latitude=95), "junk")
    assert src.drone_report_tracks(data, NOW) == {}
    assert src.drone_report_tracks(None, NOW) == src.drone_report_tracks({"reports": 0}, NOW) == {}


def test_incident_marker_track_and_validation():
    data = {"server_time": _iso(0), "lifetime_seconds": 86400, "incidents": [
        _incident(), _incident("inc-9999", url="https://evil.example/x"), _incident("x", expires=60),
        _incident("inc-old", expires=-5), _incident("inc-notitle", title="")]}
    (uid, track), = src.incident_marker_tracks(data, NOW).items()
    assert uid == "DA-INCIDENT-inc-1234" and track["callsign"] == "Drone debris found"
    assert track["position_uncertainty_m"] == 2000 and track["url"] in track["remarks"]


def _alert(country="UA", level="red", area="A"):
    return {"country": country, "level": level, "area": area}


def test_status_changes_groups_raised_changed_cleared_per_country():
    prev = {"UA-R:a": _alert(area="Alpha"), "UA-R:b": _alert(level="yellow", area="Beta"),
            "LT:ALL": _alert("LT", "orange", "Lietuva")}
    cur = {"UA-R:a": _alert(level="yellow", area="Alpha"), "UA-R:b": _alert(level="yellow", area="Beta"),
           "UA-R:c": _alert(area="Gamma")}
    got = src.status_changes(prev, cur)
    assert got["UA"] == {"raised": [{"area": "Gamma", "level": "red"}],
                         "changed": [{"area": "Alpha", "from": "red", "to": "yellow"}], "cleared": []}
    assert got["LT"]["cleared"] == [{"area": "Lietuva", "level": "orange"}]
    assert src.status_changes(cur, cur) == {}


def test_source_health_warns_once_after_repeated_failures_and_once_on_recovery():
    health = src.SourceHealth(fail_cycles=3)
    seen = [health.observe("NEPTUN", False, "unreachable", NOW) for _ in range(5)]
    assert [e["state"] if e else None for e in seen] == [None, None, "down", None, None]
    assert seen[2]["alert_type"] == "source_warning" and seen[2]["detail"] == "unreachable"
    assert health.observe("NEPTUN", True, "", NOW)["state"] == "recovered"
    assert health.observe("NEPTUN", True, "", NOW) is None
    assert health.observe("RSO", False, "x", NOW) is None and health.observe("RSO", True, "", NOW) is None


def test_source_observations_flag_unreachable_and_frozen_feeds():
    fresh = {"updatedAt": _iso(-60)}
    frozen = {"updatedAt": _iso(-38 * 3600)}
    obs = src.source_observations(NOW, {"dangausakis.lt": frozen, "NEPTUN": fresh, "LT72": None, "RSO": "x",
                                        "112.lv": {"alerts": []}},
                                  {"dangausakis.lt", "NEPTUN", "LT72", "112.lv"}, 1800)
    assert obs["dangausakis.lt"] == (False, "not updated for 38 h")
    assert obs["NEPTUN"][0] is True and obs["LT72"] == (False, "unreachable") and obs["112.lv"][0] is True
    assert "RSO" not in obs                                        # disabled sources are not judged


def test_geochat_text_for_each_event_and_nothing_for_others():
    fmt = tak_alert_layer.format_event
    text = fmt({"alert_type": "region_status_change", "country": "UA",
                "raised": [{"area": "Konotop District", "level": "red"}],
                "changed": [{"area": "X", "from": "yellow", "to": "red"}], "cleared": [{"area": "Y"}]})
    assert text == "[AIR STATUS UA] raised: Konotop District (red) | changed: X yellow->red | cleared: Y"
    many = fmt({"alert_type": "region_status_change", "country": "UA",
                "raised": [{"area": "A{}".format(i), "level": "red"} for i in range(9)]})
    assert "A5 (red) +3 more" in many and "A6" not in many
    assert fmt({"alert_type": "source_warning", "source": "NEPTUN", "state": "down",
                "detail": "not updated for 38 h"}).startswith("[AIR ALERT DATA] NEPTUN not updated for 38 h")
    assert fmt({"alert_type": "source_warning", "source": "NEPTUN", "state": "recovered"}).endswith("recovered")
    assert "[DRONE REPORT LT] heard (unverified" in fmt(
        {"alert_type": "drone_report", "report_type": "heard", "lat": 54.7, "lon": 25.3})
    assert fmt({"alert_type": "incident", "title": "T", "url": "u"}) == "[INCIDENT] T - u"
    assert fmt({"alert_type": "region_alert"}) is None and fmt({"alert_type": "region_status_change"}) is None


def test_event_handler_sends_one_chat_per_event_and_ignores_region_alerts():
    sender = mock.Mock()
    handler = tak_alert_layer.make_event_handler(sender, verbose=False)

    def sample(payload):
        return mock.Mock(payload=json.dumps(payload).encode())

    handler(sample({"alert_type": "region_alert", "alert_id": "UA-R:a", "level": "red"}))
    handler(sample({"alert_type": "incident", "title": "T", "url": "u", "lat": 1, "lon": 2}))
    handler(mock.Mock(payload=b"not json"))
    assert sender.send.call_count == 1 and "[INCIDENT] T - u" in sender.send.call_args[0][0]


def test_publish_community_tracks_tombstones_and_announces_only_new_after_first_poll():
    session, events = mock.Mock(), []
    state = {"tracks": {}, "seen": None}
    feeds = {"drone-reports": _reports(_report()), "incident-markers": {"server_time": _iso(0), "incidents": []}}
    with mock.patch.object(bridge, "_fetch", side_effect=lambda path: feeds[path]), \
            mock.patch.object(bridge, "publish_dual") as publish:
        bridge.publish_community(session, events.append, NOW, state, False)
        assert publish.call_count == 1 and events == []            # first poll: shown, not announced
        feeds["drone-reports"] = _reports(_report(), _report("b" * 32))
        bridge.publish_community(session, events.append, NOW, state, False)
        assert [e["alert_type"] for e in events] == ["drone_report"] and events[0]["uid"].endswith("b" * 16)
        feeds["drone-reports"] = None                               # fetch failure keeps what is shown
        bridge.publish_community(session, events.append, NOW, state, False)
        assert len(state["tracks"]) == 2 and session.put.call_count == 0
        feeds["drone-reports"] = _reports()                         # reports left the feed
        bridge.publish_community(session, events.append, NOW, state, False)
    tombstones = [json.loads(c.args[1]) for c in session.put.call_args_list]
    assert sorted(t["uid"][-16:] for t in tombstones) == ["a" * 16, "b" * 16] and all(t["_delete"] for t in tombstones)


def test_drone_crossing_event_text_and_schema_value():
    import json as _json
    schema = _json.load(open(ROOT / "compose/schemas/vendors/random/json/dangausakis_alert_event.schema.json"))
    assert "drone_crossing" in schema["properties"]["alert_type"]["enum"] and {"kind", "count", "from", "to"} <= set(schema["properties"])
    from layers.vendors.tak import tak_alert_layer
    text = tak_alert_layer.format_event({"alert_type": "drone_crossing", "kind": "uav", "count": 3, "from": "Sumy Oblast", "to": "Poltava Oblast"})
    assert text == "[DRONE CROSSING] UAV x3 moved Sumy Oblast -> Poltava Oblast"
