"""GeoConfirmed bridge: world GeoJSON + icon names -> recent placemarks as incident markers, and how the layers draw them.
The fixtures are trimmed copies of the site's real /api/placemark/geojson and /api/placemark/icons documents."""

import calendar
import importlib.util
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols"):
    sys.path.insert(0, str(ROOT / sub))

from layers.vendors.tak import tak_layer  # noqa: E402
from layers.vendors.tak.systematic import sitaware_layer  # noqa: E402
from protocols.vendors.random.track_views import semantic_topic  # noqa: E402

_spec = importlib.util.spec_from_file_location("gc_bridge", ROOT / "compose/bridges/vendors/geoconfirmed/geoconfirmed_bridge.py")
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)

FIXTURES = ROOT / "tests" / "fixtures" / "geoconfirmed"
WORLD = json.loads((FIXTURES / "world.json").read_text())
ICONS = json.loads((FIXTURES / "icons.json").read_text())
NOW = calendar.timegm((2026, 10, 12, 12, 0, 0))          # 15:00 on 12 October in Kyiv


def _match(pattern: str, topic: str) -> bool:
    regex = pattern.replace("**", "\0").replace("*", "[^/]+").replace("\0", ".*")
    return re.match("^" + regex + "$", topic + "/x") is not None


def _cot(topic: str):
    rel = topic[len(tak_layer.TOPIC_ROOT) + 1:]
    return [v for p, (v, _s) in tak_layer._TOPIC_COT.items() if _match(p, rel)]


def _sidc(topic: str):
    rel = topic[len(tak_layer.TOPIC_ROOT) + 1:]
    return [v for p, v in sitaware_layer._TOPIC_SIDC.items() if _match(p, rel)]


def test_only_placemarks_inside_the_window_become_markers_newest_first():
    kinds = gc.kind_names(ICONS)
    three = gc.event_tracks(WORLD, kinds, NOW, days=3)
    four = gc.event_tracks(WORLD, kinds, NOW, days=4)
    assert len(three) == 4 and len(four) == 6                      # the 2023 placemark is in neither
    assert {t["event_date"] for _, t in three.values()} == {"2026-10-11"}
    assert list(gc.event_tracks(WORLD, kinds, NOW, days=4, limit=2).values())[0][1]["event_date"] == "2026-10-11"


def test_a_placemark_names_its_kind_and_date_and_says_who_struck():
    kinds = gc.kind_names(ICONS)
    out = gc.event_tracks(WORLD, kinds, NOW, days=4)
    by_kind = {kinds[gc._template(f["properties"]["icon"])][:30]: f for f in WORLD["geojson"]["features"]}
    assert out and all(re.fullmatch(r"GC-[0-9A-F]{12}", t["uid"]) for _, t in out.values())
    for prefix, track in out.values():
        assert track["callsign"].endswith(("12 Oct", "11 Oct", "10 Oct", "09 Oct"))
        assert "GeoConfirmed" in track["remarks"] and track["position_uncertainty_m"] == 100 and track["role"] in {"striker", "victim", "other"}
    assert {t["role"] for _, t in out.values()} >= {"other"}


def test_the_side_is_whoever_struck_read_from_what_the_colour_means_for_that_kind_of_event():
    # kind names are GeoConfirmed's own icon names
    shelling = "Shelling by artillery or aerial bombings - Location of the shelling, colour of who is shelling; Brown = UXO"
    drone = "Short Range (<50km) OWAD (One Way Attack Drone), mostly rotary wing, Flying OR Attacking a target"
    crashed = "Recently destroyed/Damaged [Intercepted/crashed] Longe Range (>50km) OWAD (One Way Attack Drone)"
    destroyed = "Recently Destroyed/Damaged APC OR IFV / TRACKED or WHEELED / ARMOURED"
    civilians = "Killed or wounded civilian. Potential Warcrime. In the colour of who is assessed as doing the attack/killing."
    assert gc.striker(shelling, "Russia (UKR)") == ("hostile", "striker")             # the colour is the shooter
    assert gc.striker(shelling, "Ukraine (UKR)") == ("friendly", "striker")
    assert gc.striker(drone, "Russia (UKR)") == ("hostile", "striker")
    assert gc.striker(crashed, "Ukraine (UKR)") == ("friendly", "striker")            # a drone lost in its own attack
    assert gc.striker(civilians, "Russia Civilian (UKR)") == ("hostile", "striker")   # the colour is who killed them
    assert gc.striker(destroyed, "Russia (UKR)") == ("friendly", "victim")            # a Russian APC destroyed: a Ukrainian strike
    assert gc.striker(destroyed, "Ukraine (UKR)") == ("hostile", "victim")
    assert gc.striker("WIA/KIA Soldier/Militant", "Russia (UKR)") == ("friendly", "victim")
    assert gc.striker("Recently destroyed/Damaged[Visible in footage] or still burning infrastucture", "Neutral (UKR)") == ("neutral", "victim")
    assert gc.striker("Tank (Driving or fighting)", "Russia (UKR)") == ("hostile", "other")          # not a strike
    assert gc.striker("POW - Prisinor of War - In the colour of the faction of the POW", "Ukraine (UKR)") == ("friendly", "other")
    assert gc.striker("Interception of a UAV - Colour of the force intercepting", "Ukraine (UKR)") == ("friendly", "other")
    assert gc.striker("Controlled Demolition OR explosion of unknown origin", "Russia (UKR)") == ("neutral", "other")


def test_the_remarks_say_who_struck_or_that_it_was_not_a_strike():
    world = {"factionMeta": [{"id": 7, "name": "Russia (UKR)", "conflictCode": "UKR"}],
             "geojson": {"features": [
                 {"geometry": {"type": "Point", "coordinates": [37.0, 48.0]},
                  "properties": {"id": "a" * 8 + "-1", "factionId": 7, "date": "2026-10-12T00:00:00", "icon": "x/172.png"}},
                 {"geometry": {"type": "Point", "coordinates": [37.1, 48.1]},
                  "properties": {"id": "b" * 8 + "-2", "factionId": 7, "date": "2026-10-12T00:00:00", "icon": "x/10.png"}},
                 {"geometry": {"type": "Point", "coordinates": [37.2, 48.2]},
                  "properties": {"id": "c" * 8 + "-3", "factionId": 7, "date": "2026-10-12T00:00:00", "icon": "x/41.png"}}]}}
    kinds = {"172.png": "Recently Destroyed/Damaged APC OR IFV / TRACKED", "10.png": "Shelling by artillery or aerial bombings - Location",
             "41.png": "Tank (Driving or fighting)"}
    out = {t["role"]: (p, t) for p, t in gc.event_tracks(world, kinds, NOW).values()}
    assert out["victim"][0].endswith("/friendly/incident") and "Ukrainian strike (the owner was struck, inferred)" in out["victim"][1]["remarks"]
    assert out["striker"][0].endswith("/hostile/incident") and "Russian strike" in out["striker"][1]["remarks"]
    assert out["other"][0].endswith("/hostile/incident") and "not a strike" in out["other"][1]["remarks"]


def test_civilians_are_neutral_and_unknown_factions_unknown():
    assert gc._side("Russia Civilian (UKR)") == "neutral" and gc._side("Ukraine Civilian (UKR)") == "neutral"
    assert gc._side("NATO (UKR)") == "friendly" and gc._side("Belarus (UKR)") == "hostile"
    assert gc._side("Special (UKR)") == "unknown" and gc._side("Something new") == "unknown"


def test_an_event_kind_is_cut_to_its_first_clause():
    assert gc._short("Shelling by artillery or aerial bombings - Location of the shelling, c") == "Shelling by artillery or aerial bombings"
    assert gc._short("WIA/KIA Soldier/Militant") == "WIA/KIA Soldier/Militant"
    assert gc._short("Recently Destroyed/Damaged[Visible in footage] Logistical Trucks (Soft Skin)") == "Recently Destroyed/Damaged Logistical Trucks"
    assert gc._short("Killed or wounded civilian. Potential Warcrime. In the colour of who is the victim") == "Killed or wounded civilian"
    assert len(gc._short("x" * 100)) <= 44


def test_both_c2s_draw_every_side_as_the_2525b_explosion():
    out = gc.event_tracks(WORLD, gc.kind_names(ICONS), NOW, days=4)
    expected = {"hostile": ("a-h-G-I-E", "SHGPIE----*****"), "friendly": ("a-f-G-I-E", "SFGPIE----*****"),
                "neutral": ("a-n-G-I-E", "SNGPIE----*****"), "unknown": ("a-u-G-I-E", "SUGPIE----*****")}
    for prefix, track in out.values():
        topic = semantic_topic(prefix, track)
        side = prefix.rsplit("/", 2)[-2]
        assert _cot(topic) == [expected[side][0]], topic
        assert _sidc(topic) == [expected[side][1]], topic
        xml = tak_layer.track_to_cot(track, _cot(topic)[0], stale_s=tak_layer._track_stale_s(track, 120))
        assert xml and track["callsign"] in xml


def test_a_placemark_without_a_position_or_in_another_conflict_is_skipped():
    world = {"factionMeta": WORLD["factionMeta"] + [{"id": 99, "name": "Iran (Iran)", "conflictCode": "Iran"}],
             "geojson": {"features": [
                 {"geometry": {"type": "Point", "coordinates": []}, "properties": {"id": "a", "factionId": 7, "date": "2026-10-12T00:00:00", "icon": "x/1.png"}},
                 {"geometry": {"type": "Point", "coordinates": [30.0, 50.0]}, "properties": {"id": "b", "factionId": 99, "date": "2026-10-12T00:00:00", "icon": "x/1.png"}},
                 {"geometry": {"type": "Point", "coordinates": [30.0, 50.0]}, "properties": {"id": "c", "factionId": 7, "icon": "x/1.png"}}]}}
    assert gc.event_tracks(world, {}, NOW) == {}



def test_a_strike_on_infrastructure_is_told_by_the_place_ukrainian_in_russia_russian_elsewhere():
    world = {"factionMeta": [{"id": 9, "name": "Neutral (UKR)", "conflictCode": "UKR"}],
             "geojson": {"features": [
                 {"geometry": {"type": "Point", "coordinates": [38.0, 51.0]},       # in the Russian square below
                  "properties": {"id": "a" * 8 + "-1", "factionId": 9, "date": "2026-10-12T00:00:00", "icon": "x/90.png"}},
                 {"geometry": {"type": "Point", "coordinates": [30.0, 49.0]},       # outside it
                  "properties": {"id": "b" * 8 + "-2", "factionId": 9, "date": "2026-10-12T00:00:00", "icon": "x/90.png"}}]}}
    kinds = {"90.png": "Recently destroyed/Damaged [Visible in footage] or still burning infrastucture"}
    square = [{"id": "RU", "name": "Russia", "rings": [[[35.0, 49.0], [45.0, 49.0], [45.0, 55.0], [35.0, 55.0], [35.0, 49.0]]]}]
    sides = {t["lon_deg"]: (p.split("/")[-2], t["remarks"]) for p, t in gc.event_tracks(world, kinds, NOW, russia=square).values()}
    assert sides[38.0][0] == "friendly" and "Ukrainian strike (the place says so" in sides[38.0][1]
    assert sides[30.0][0] == "hostile" and "Russian strike (the place says so" in sides[30.0][1]
    unknown = {p.split("/")[-2] for p, t in gc.event_tracks(world, kinds, NOW).values()}
    assert unknown == {"neutral"}                                                   # no outline of Russia: left neutral
