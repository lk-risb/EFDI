"""Direct alert sources hidden under the dangausakis bridge: NEPTUN districts, LT72 RSS, merging."""

import pathlib
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "layers" / "vendors" / "tak"))

from bridges.vendors.dangausakis import alert_sources as src  # noqa: E402
import tak_alert_layer  # noqa: E402

NOW = time.time()
MAX_AGE, SRC_AGE = 21600, 1800


def _iso(age_s):
    return datetime.fromtimestamp(NOW - age_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _neptun(raions=(), oblasts=(), updated_age=10):
    return {"updatedAt": _iso(updated_age), "raions": list(raions), "oblasts": list(oblasts)}


def _raion(key="бахмутський", level="red", age=600):
    return {"key": key, "name": "Бахмутський район", "oblast": "Донецька область",
            "since": _iso(age), "level": level, "reasons": ["Ракетна загроза (червоний рівень)"]}


def test_neptun_district_alert_shape_and_no_popup():
    out = src.neptun_alerts(_neptun([_raion()]), NOW, SRC_AGE)
    a = out["UA-R:бахмутський"]
    assert a["country"] == "UA" and a["level"] == "red" and a["source"] == "NEPTUN"
    assert a["notify"] is False and a["stale"] is False and "Missile threat" in a["reason"]
    assert a["_src"] == "neptun.in.ua"


def test_neptun_keeps_long_standing_alerts_drops_unknown_levels_and_flags_stale_source():
    old = _raion("крим", age=4 * 365 * 86400)                  # occupied regions stay red for years
    assert "UA-O:крим" in src.neptun_alerts(_neptun(oblasts=[old]), NOW, SRC_AGE)
    assert src.neptun_alerts(_neptun([_raion(level="purple")]), NOW, SRC_AGE) == {}
    stale = src.neptun_alerts(_neptun([_raion()], updated_age=7200), NOW, SRC_AGE)
    assert stale["UA-R:бахмутський"]["stale"] is True


def test_neptun_boundaries_use_the_alert_ids():
    sq = [[30, 50], [31, 50], [31, 51], [30, 51], [30, 50]]
    raions = {"features": [{"properties": {"key": "бахмутський", "rayon": "Бахмутський район"},
                            "geometry": {"type": "Polygon", "coordinates": [sq]}}]}
    oblasts = {"features": [{"properties": {"key": "донецька", "region": "Донецька область"},
                             "geometry": {"type": "MultiPolygon", "coordinates": [[sq], [sq]]}}]}
    b = src.neptun_boundaries(raions, oblasts)
    assert b["UA-R:бахмутський"]["name"] == "Bakhmutskyi District" and len(b["UA-O:донецька"]["rings"]) == 2


def _rss(*items):
    body = "".join(
        "<item><title>{}</title><pubDate>{}</pubDate><description><![CDATA[{}]]></description></item>".format(
            t, format_datetime(datetime.fromtimestamp(NOW - age, tz=timezone.utc)), d) for t, age, d in items)
    return "<rss><channel>" + body + "</channel></rss>"


YELLOW = ("Oro pavojus Geltona", 1200, "Lietuvos kariuomenė praneša: TIKĖTINAS ORO PAVOJUS (GELTONA). Nusimatykite saugią vietą.")
WHITE = ("Oro pavojaus atšaukimas Balta", 300, "Lietuvos kariuomenė praneša: ORO PAVOJAUS NĖRA (BALTA).")
TEST = ("Perspėjimo sistemos patikrinimas", 60, "PAGD praneša: Tikrinama gyventojų perspėjimo sistema, įjungiamos sirenos.")


def test_lt72_yellow_alert_is_nationwide_and_notifies():
    out = src.lt72_alert(_rss(YELLOW), NOW, MAX_AGE)
    a = out["LT:ALL"]
    assert a["level"] == "yellow" and a["country"] == "LT" and a["notify"] is True and a["source"] == "LT72"
    assert a["_src"] == "lt72.lt"


def test_lt72_newest_white_message_clears_and_system_tests_are_ignored():
    assert src.lt72_alert(_rss(WHITE, YELLOW), NOW, MAX_AGE) == {}          # newest first: cleared
    assert "LT:ALL" in src.lt72_alert(_rss(TEST, YELLOW), NOW, MAX_AGE)     # test message skipped
    assert src.lt72_alert(_rss(TEST), NOW, MAX_AGE) == {}
    old = ("Oro pavojus Geltona", MAX_AGE + 600, YELLOW[2])
    assert src.lt72_alert(_rss(old), NOW, MAX_AGE) == {}


def test_lt72_colour_words_and_unknown_colour_defaults_to_orange():
    for word, level in (("RAUDONA", "red"), ("ORANŽINĖ", "orange"), ("GELTONA", "yellow")):
        item = ("Oro pavojus", 60, "ORO PAVOJUS ({}).".format(word))
        assert src.lt72_alert(_rss(item), NOW, MAX_AGE)["LT:ALL"]["level"] == level
    assert src.lt72_alert(_rss(("Oro pavojus", 60, "ORO PAVOJUS.")), NOW, MAX_AGE)["LT:ALL"]["level"] == "orange"


def test_expand_nationwide_adds_silent_per_county_entries():
    boundaries = {"LT:LT-VL": {"name": "Vilniaus apskritis", "rings": []},
                  "LT:LT-KU": {"name": "Kauno apskritis", "rings": []},
                  "LV:0001000": {"name": "Riga", "rings": []}}
    out = src.expand_nationwide(src.lt72_alert(_rss(YELLOW), NOW, MAX_AGE), boundaries, "LT")
    assert set(out) == {"LT:ALL", "LT:LT-VL", "LT:LT-KU"}
    assert out["LT:ALL"]["notify"] is True and out["LT:LT-VL"]["notify"] is False
    assert out["LT:LT-VL"]["area"] == "Vilniaus apskritis" and out["LT:LT-VL"]["level"] == "yellow"


def test_merge_direct_source_replaces_site_alerts_for_its_country_only():
    site = {"UA:київська": {"country": "UA", "alert_id": "UA:київська"},
            "PL:slaskie": {"country": "PL", "alert_id": "PL:slaskie"},
            "LT:LT-VL": {"country": "LT", "alert_id": "LT:LT-VL"}}
    direct = {"UA": {"UA-R:x": {"country": "UA", "alert_id": "UA-R:x"}}, "LT": {}}   # LT healthy but quiet
    assert set(src.merge(site, direct)) == {"PL:slaskie", "UA-R:x"}
    assert set(src.merge(site, {"UA": None, "LT": None})) == set(site)               # sources down: site stays


def test_source_cache_keeps_last_good_value_for_a_while():
    cache = src.SourceCache(max_age_s=100)
    assert cache.update({"a": 1}, now=0) == {"a": 1}
    assert cache.update(None, now=50) == {"a": 1}       # failed poll, still fresh enough
    assert cache.update(None, now=200) is None          # too old
    assert cache.update({"b": 2}, now=300) == {"b": 2}


def test_alert_layer_skips_zone_only_alerts():
    sender = mock.Mock()
    handler = tak_alert_layer.make_region_alert_handler(sender, verbose=False)

    def sample(**kw):
        import json
        payload = {"alert_type": "region_alert", "alert_id": "UA-R:x", "country": "UA", "level": "red",
                   "area": "X", "since": "t", "stale": False}
        payload.update(kw)
        return mock.Mock(payload=json.dumps(payload).encode())

    tak_alert_layer._alerted.clear()
    handler(sample(notify=False))
    sender.send.assert_not_called()
    handler(sample(notify=True, alert_id="LT:ALL"))
    sender.send.assert_called_once()


# --- Poland (RSO, additive) and the per-cycle failure handling ---------------------------

def _rso_notice(title, shortcut="", created="2026-10-05 12:00:00", valid_to=None, provinces=None):
    return {"title": title, "shortcut": shortcut, "created_at": created, "valid_from": created, "valid_to": valid_to,
            "provinces": provinces or {"12": {"id": "12", "name": "śląskie", "slug_name": "slaskie"}}}


# 12:00 Warsaw local on 2026-10-05 is 10:00:00Z
RSO_NOW = datetime(2026, 10, 5, 10, 30, tzinfo=timezone.utc).timestamp()


def test_rso_air_raid_notice_becomes_a_red_province_alert():
    data = {"newses": [_rso_notice("ALARM LOTNICZY dla woj. śląskiego")]}
    a = src.rso_alerts(data, RSO_NOW, MAX_AGE)["PL:slaskie"]
    assert a["level"] == "red" and a["country"] == "PL" and a["source"] == "RSO" and a["notify"] is True
    assert a["_src"] == "komunikaty.tvp.pl" and a["since"] == "2026-10-05T10:00:00Z"


def test_rso_threat_wording_is_orange_and_each_province_gets_an_alert():
    two = {"1": {"slug_name": "malopolskie", "name": "małopolskie"}, "2": {"slug_name": "slaskie", "name": "śląskie"}}
    data = {"newses": [_rso_notice("Zagrożenie atakiem z powietrza", provinces=two)]}
    out = src.rso_alerts(data, RSO_NOW, MAX_AGE)
    assert set(out) == {"PL:malopolskie", "PL:slaskie"} and out["PL:slaskie"]["level"] == "orange"


def test_rso_tests_unrelated_notices_expired_and_old_are_ignored():
    for notice in (
        _rso_notice("TESTY SYREN ALARMOWYCH", "Test syren w Bielsku-Białej. Alarm testowy."),
        _rso_notice("Alarm lotniczy - ćwiczenia"),
        _rso_notice("Przekroczenie PD35 dni PM10"),
        _rso_notice("Alarm lotniczy", valid_to="2026-10-05 12:10:00"),        # ended 10:10Z, before RSO_NOW
        _rso_notice("Alarm lotniczy", created="2026-10-04 12:00:00"),          # 22 h old
    ):
        assert src.rso_alerts({"newses": [notice]}, RSO_NOW, MAX_AGE) == {}, notice["title"]
    assert src.rso_alerts({}, RSO_NOW, MAX_AGE) == {}


def test_additive_country_keeps_the_sites_alerts_and_adds_direct_ones():
    site = {"PL:pomorskie": {"country": "PL", "alert_id": "PL:pomorskie"},
            "UA:x": {"country": "UA", "alert_id": "UA:x"}}
    direct = {"PL": {"PL:slaskie": {"country": "PL", "alert_id": "PL:slaskie"}},
              "UA": {"UA-R:y": {"country": "UA", "alert_id": "UA-R:y"}}}
    out = src.merge(site, direct, additive=frozenset({"PL"}))
    assert set(out) == {"PL:pomorskie", "PL:slaskie", "UA-R:y"}       # PL added to, UA replaced


from bridges.vendors.dangausakis import dangausakis_bridge as bridge  # noqa: E402

_SITE = {"updatedAt": _iso(10), "alerts": [
    {"id": "LV:0001000", "country": "LV", "level": "yellow", "since": _iso(600), "area": "Riga", "source": "112"},
    {"id": "UA:київська", "country": "UA", "level": "orange", "since": _iso(600), "area": "Kyiv", "source": "NEPTUN"}],
    "sources": {c: {"status": "ok", "stale": False, "updatedAt": _iso(30)} for c in ("LV", "UA", "LT", "PL")}}
_NEP = _neptun([_raion()])
_LT = _rss(YELLOW)
_RSO = {"newses": []}


def _compose(site=_SITE, nep=_NEP, lt=_LT, rso=_RSO, boundaries=None):
    return bridge.compose_alerts(NOW, site, nep, lt, rso, boundaries or {})


def test_compose_all_sources_healthy_direct_replaces_site_for_ua_and_lt():
    out = _compose()
    assert "UA-R:бахмутський" in out and "UA:київська" not in out       # NEPTUN replaces the site's UA
    assert "LT:ALL" in out and "LV:0001000" in out                      # LT72 direct, LV from the site


def test_compose_direct_source_down_falls_back_to_the_sites_data_for_that_country():
    out = _compose(nep=None)
    assert "UA:київська" in out and not any(k.startswith("UA-R:") for k in out)
    assert "LT:ALL" in out                                              # other direct sources unaffected


def test_compose_site_down_keeps_direct_sources_alerts():
    out = _compose(site=None)
    assert set(out) == {"UA-R:бахмутський", "LT:ALL"}


def test_compose_everything_down_returns_none_so_the_picture_is_kept():
    assert _compose(site=None, nep=None, lt=None, rso=None) is None


def test_compose_healthy_but_quiet_direct_source_still_replaces_the_sites_alerts():
    quiet_ua = _neptun([])
    assert not any(k.startswith("UA") for k in _compose(nep=quiet_ua))   # NEPTUN says nothing active -> none
    clear_lt = _rss(WHITE, YELLOW)
    assert "LT:ALL" not in _compose(lt=clear_lt)


def test_compose_respects_country_filter(monkeypatch):
    monkeypatch.setattr(bridge, "COUNTRIES", {"LV"})
    assert set(_compose()) == {"LV:0001000"}


def test_english_romanises_ukrainian_names_and_translates_common_words():
    assert src.english("Кропивницький район") == "Kropyvnytskyi District"
    assert src.english("Київська область") == "Kyivska Oblast"
    assert src.english("м. Київ") == "City of Kyiv"
    assert src.english("Автономна Республіка Крим") == "Autonomous Republic of Crimea"
    assert src.english("Ракетна загроза (червоний рівень)") == "Missile threat (red level)"
    assert src.english("already English 12") == "already English 12"
    out = src.neptun_alerts(_neptun([_raion()]), NOW, SRC_AGE)
    assert out["UA-R:бахмутський"]["area"] == "Bakhmutskyi District"


def test_lv_alert_only_for_air_raid_items_and_empty_feed_is_healthy():
    assert src.lv_alerts({"alerts": []}, NOW) == {}
    assert src.lv_alerts({"alerts": [{"title": "Plūdu brīdinājums"}]}, NOW) == {}
    got = src.lv_alerts({"alerts": [{"title": "Gaisa trauksme visā valstī"}]}, NOW)
    assert got["LV:ALL"]["level"] == "red" and got["LV:ALL"]["country"] == "LV"
