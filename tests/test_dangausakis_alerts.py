"""dangausakis.lt region alerts: bridge filtering and tak_alert_layer GeoChat handler."""

import json
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "layers" / "vendors" / "tak"))
sys.path.insert(0, str(ROOT / "compose" / "bridges" / "vendors" / "dangausakis"))

import dangausakis_bridge as bridge  # noqa: E402
import tak_alert_layer  # noqa: E402

NOW = time.time()


def _iso(age_s):
    return (datetime.fromtimestamp(NOW - age_s, tz=timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh(**kw):
    return {"status": "ok", "stale": False, "updatedAt": _iso(30), **kw}


def _feed(*alerts, stale=False):
    return {"alerts": list(alerts), "sources": {"LT": _fresh(stale=stale), "UA": _fresh()}}


def _alert(alert_id="LT:vilnius", country="LT", age_s=600, level="red"):
    return {"id": alert_id, "country": country, "level": level, "since": _iso(age_s),
            "area": "Vilnius", "source": "LT72"}


def test_recent_alert_is_kept():
    out = bridge.active_alerts(_feed(_alert()), NOW, max_age_s=3600)
    assert out["LT:vilnius"]["alert_type"] == "region_alert"
    assert out["LT:vilnius"]["stale"] is False


def test_standing_old_alert_is_dropped():
    old = _alert("UA:crimea", "UA", age_s=4 * 365 * 86400)
    assert bridge.active_alerts(_feed(old), NOW, max_age_s=3600) == {}


def test_country_filter_and_stale_flag():
    feed = _feed(_alert(), _alert("UA:kyiv", "UA"), stale=True)
    assert set(bridge.active_alerts(feed, NOW, 3600, {"LT"})) == {"LT:vilnius"}
    assert bridge.active_alerts(feed, NOW, 3600)["LT:vilnius"]["stale"] is True


def test_bad_since_is_skipped():
    bad = _alert()
    bad["since"] = "not-a-date"
    assert bridge.active_alerts(_feed(bad), NOW, 3600) == {}


class _Sample:
    def __init__(self, payload):
        self.key_expr = "efdi/land/dangausakis/alert/neutral/zone/status"
        self.payload = json.dumps(payload).encode()


def _payload(**kw):
    p = {"alert_type": "region_alert", "alert_id": "LT:vilnius", "country": "LT",
         "level": "red", "area": "Vilnius", "since": _iso(60), "stale": False}
    p.update(kw)
    return p


def test_layer_fires_once_then_rearms_after_clear():
    tak_alert_layer._alerted.clear()
    sender = mock.Mock()
    handler = tak_alert_layer.make_region_alert_handler(sender, verbose=False)
    handler(_Sample(_payload()))
    handler(_Sample(_payload()))
    assert sender.send.call_count == 1
    assert "AIR ALERT LT" in sender.send.call_args[0][0]
    handler(_Sample(_payload(_delete=True)))
    handler(_Sample(_payload()))
    assert sender.send.call_count == 2


def test_layer_ignores_stale_and_foreign_payloads():
    tak_alert_layer._alerted.clear()
    sender = mock.Mock()
    handler = tak_alert_layer.make_region_alert_handler(sender, verbose=False)
    handler(_Sample(_payload(stale=True)))
    handler(_Sample({"alert_type": "something_else", "alert_id": "x"}))
    sender.send.assert_not_called()


# --- aircraft tracks -------------------------------------------------------

def _ac(**kw):
    a = {"hex": "33FD3D", "flight": "GRANY01 ", "registration": "MM82017", "aircraftType": "A139",
         "signalType": "adsb_icao", "lat": 54.7, "lon": 25.3, "altitudeFt": 1000, "groundSpeedKt": 100.0,
         "trackDeg": 90.0, "verticalRateFpm": 600, "squawk": "1234", "seenPositionSec": 3,
         "positionTimestamp": 1791191848500}
    a.update(kw)
    return a


def test_aircraft_track_shape_and_units():
    (prefix, track), = bridge.aircraft_tracks({"aircraft": [_ac()]})
    assert prefix.endswith("/air/dangausakis/adsb/civ/aircraft")
    assert track["icao24"] == "33fd3d" and track["callsign"] == "GRANY01"
    assert track["baro_alt_m"] == 304.8 and track["speed_ms"] == 51.44
    assert track["vertical_rate_ms"] == 3.05 and track["_ts"] == 1791191848.5


def test_aircraft_mlat_modality_and_stale_filters():
    assert bridge.aircraft_tracks({"aircraft": [_ac(signalType="mlat")]})[0][0].endswith("/mlat/civ/aircraft")
    assert bridge.aircraft_tracks({"stale": True, "aircraft": [_ac()]}) == []
    assert bridge.aircraft_tracks({"aircraft": [_ac(seenPositionSec=300)]}) == []
    assert bridge.aircraft_tracks({"aircraft": [_ac(hex=""), _ac(lat=None)]}) == []


def test_aircraft_uid_matches_other_sources_for_same_icao24():
    import tak_layer
    (_, track), = bridge.aircraft_tracks({"aircraft": [_ac()]})
    assert tak_layer._uid(track) == tak_layer._uid({"icao24": "33FD3D", "_src": "some-other-adsb"})


def test_frozen_or_errored_source_marks_alerts_stale_even_if_flag_says_fresh():
    frozen = {"alerts": [_alert()], "sources": {"LT": _fresh(updatedAt=_iso(34 * 3600))}}
    assert bridge.active_alerts(frozen, NOW, 3600)["LT:vilnius"]["stale"] is True
    errored = {"alerts": [_alert()], "sources": {"LT": _fresh(status="error")}}
    assert bridge.active_alerts(errored, NOW, 3600)["LT:vilnius"]["stale"] is True
    missing = {"alerts": [_alert()], "sources": {}}
    assert bridge.active_alerts(missing, NOW, 3600)["LT:vilnius"]["stale"] is True
