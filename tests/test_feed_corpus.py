"""Regression corpus: payload shapes captured from real partner feeds, run through decoder and twin detection.

Each shape of the same drone (consilium sa-event, goat geo.track, goat telemetry, and three soc-bx objects) must end
up as exactly one drawn marker, a friendly unmanned aircraft, whichever order they arrive in. A new problem feed adds
its payload to tests/fixtures/feeds and a case here."""

import importlib.util
import itertools
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols",
            "compose/protocols/vendors/random"):
    sys.path.insert(0, str(ROOT / sub))

from protocols.vendors.random.fusion import TwinIndex  # noqa: E402
from protocols.vendors.socbx import socbx  # noqa: E402
from schemas.vendors.socbx.proto.socbx_unified_pb2 import UnifiedSchema  # noqa: E402

_spec = importlib.util.spec_from_file_location("generic_json_corpus", ROOT / "compose/protocols/vendors/random/generic_json.py")
generic_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generic_json)

FIXTURES = ROOT / "tests" / "fixtures" / "feeds"
ROOT_TOPIC = "LTU/CISB/zenoh_gateway"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _json_source(name: str, origin: str):
    record, dimension, slot = generic_json.normalize(_fixture(name), origin)
    return record, "{}/{}/backbone/{}/unit/tracks/v1".format(ROOT_TOPIC, dimension, slot)


def _socbx_source(key: str, lat: float, lon: float, **identity):
    message = UnifiedSchema()
    features = message.objects[key].features
    features.location.latitude, features.location.longitude = lat, lon
    for field, value in identity.items():
        setattr(features.identity, field, value)
    (record,) = socbx.unified_records(message.SerializeToString())
    dimension, slot = record.pop("_dimension"), record.pop("_slot")
    record.pop("_entity", None)
    return record, "{}/{}/backbone/{}/unit/tracks/v1".format(ROOT_TOPIC, dimension, slot)


def _drone_sources() -> list:
    return [
        _json_source("consilium_sa_event_drone.json", "consilium/sa-event/out/6f364c30/v2"),
        _json_source("goat_geo_track_drone.json", "df7b0baa/geo/track/v1/deltaquad-nvrec5"),
        _json_source("goat_telemetry_drone.json", "df7b0baa/telemetry/v1/deltaquad-nvrec5"),
        _socbx_source("track:minerva:teil-s-10:deltaquad-nvrec5", 49.2023, 11.8832,
                      object_id="minerva:teil-s-10:deltaquad-nvrec5", object_type="track", object_subtype="a-f-A-M-F-Q",
                      affiliation="friend", label="deltaquad-nvrec5", domain="air"),
        _socbx_source("drone_telemetry:deltaquad-nvrec5", 49.2022, 11.8831, object_id="deltaquad-nvrec5"),
        _socbx_source("8202ee7e-d2aa-5eac-8e69-de29ad4ad8d7", 49.2021, 11.8830,
                      label="deltaquad-nvrec5", affiliation="friend"),
    ]


def test_every_shape_of_the_same_drone_decodes_as_an_unmanned_aircraft():
    for record, topic in _drone_sources():
        assert "/air/" in topic and record["target_type"] == "uav", (record["uid"], topic)


def test_one_drone_seen_through_six_feeds_is_one_marker_whatever_the_arrival_order():
    sources = _drone_sources()
    for order in itertools.permutations(range(len(sources))):
        index, held = TwinIndex(), set()
        for now, i in enumerate(order, start=100):
            record, topic = sources[i]
            held.update(index.observe(record, float(now), topic))
        kept = [(r["uid"], t) for r, t in sources if r["uid"] not in held]
        assert len(kept) == 1, (order, kept)
        assert "/friendly/" in kept[0][1], (order, kept)


def test_two_different_drones_with_distinct_names_are_not_merged():
    first, second = _drone_sources()[1][0], dict(_drone_sources()[1][0], uid="BACKBONE-other", callsign="other-drone-7")
    index = TwinIndex()
    topic = ROOT_TOPIC + "/air/backbone/friendly/unit/tracks/v1"
    assert index.observe(first, 100.0, topic) == [] and index.observe(second, 101.0, topic) == []


def test_a_named_copy_far_away_is_a_different_object():
    near, far = _drone_sources()[1], _drone_sources()[2]
    far[0]["lat_deg"] = 52.0
    index = TwinIndex()
    index.observe(near[0], 100.0, near[1])
    assert index.observe(far[0], 101.0, far[1]) == []


def test_a_marker_that_declared_its_affiliation_does_not_flip_back_to_unknown():
    from protocols.vendors.random.fusion import MarkerMemory
    memory = MarkerMemory(hold_s=300)
    assert memory.settle("U1", "a-u-A-M-F-Q", 100.0) == "a-u-A-M-F-Q"       # nothing declared yet
    assert memory.settle("U1", "a-f-A-M-F-Q", 110.0) == "a-f-A-M-F-Q"
    assert memory.settle("U1", "a-u-A-M-F-Q", 120.0) == "a-f-A-M-F-Q"       # an undeclared copy arrives: held
    assert memory.settle("U1", "a-h-A-M-F-Q", 130.0) == "a-h-A-M-F-Q"       # a new declaration wins at once
    assert memory.settle("U1", "a-u-A-M-F-Q", 500.0) == "a-u-A-M-F-Q"       # the declaration went stale
    assert memory.settle("U2", "SUAPMFQ---*****", 100.0) == "SUAPMFQ---*****"
    assert memory.settle("U2", "SFAPMFQ---*****", 101.0) == "SFAPMFQ---*****"
    assert memory.settle("U2", "SUAPMFQ---*****", 102.0) == "SFAPMFQ---*****"
    assert memory.settle("U3", "not-a-code", 100.0) == "not-a-code"


def test_adsb_aircraft_a_partner_sends_back_are_keyed_by_icao_address_like_dangausakis_does():
    # three shapes seen live from the soc-bx feed (our own adsb.lol data via dangausakis, returned under a partner id)
    shapes = [
        ("aircraft:ae057d", dict(object_id="ae057d", callsign="RCH2048", object_type="aircraft", model="C5M"), "ae057d"),
        ("aircraft_observation:503fd9", dict(external_id="503fd9", callsign="LYF02", object_type="aircraft"), "503fd9"),
        ("aircraft_track:3f6cb8", dict(external_id="3f6cb8", callsign="LIMIT13", platform_type="aircraft"), "3f6cb8"),
    ]
    for key, identity, address in shapes:
        record, topic = _socbx_source(key, 49.2, 11.9, **identity)
        assert record["icao24"] == address and record["target_type"] == "aircraft", key
        assert "/air/backbone/civ/" in topic, topic


def test_a_loose_adsb_copy_behind_several_words_is_still_a_twin_of_the_keyed_aircraft():
    index = TwinIndex()
    index.observe({"icao24": "4a34e9", "callsign": "ROF224", "lat_deg": 46.5, "lon_deg": 22.7}, 100.0)
    assert index.observe({"uid": "Entity:aircraft:4a34e9", "callsign": "ROF224", "lat_deg": 46.6, "lon_deg": 22.8}, 101.0) \
        == ["Entity:aircraft:4a34e9"]
