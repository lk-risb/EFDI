"""socbx: ICAO-keyed aircraft and echoes of our own sensors coming back from the backbone."""

import json
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

from protocols.vendors.random.echo_filter import EchoFilter  # noqa: E402
from protocols.vendors.socbx import socbx  # noqa: E402
from schemas.vendors.socbx.proto.socbx_unified_pb2 import UnifiedSchema  # noqa: E402


def _payload(*objects):
    msg = UnifiedSchema()
    msg.node_id = "n1"
    for object_id, callsign, domain, *rest in objects:
        features = msg.objects[object_id].features
        features.location.latitude, features.location.longitude = 52.36, 13.5
        features.identity.object_id = object_id
        features.identity.callsign = callsign
        features.identity.domain = domain
        if rest:
            features.identity.object_type = features.identity.platform_type = rest[0]
    return msg.SerializeToString()


def test_bare_hex_object_without_type_is_an_icao_keyed_aircraft():
    (rec,) = socbx.unified_records(_payload(("0101B7", "MSR731", "")))
    assert rec["icao24"] == "0101b7" and rec["_dimension"] == "air" and rec["target_type"] == "aircraft"


def test_generic_sensor_tag_does_not_stop_an_aircraft_being_recognised():
    (rec,) = socbx.unified_records(_payload(("48c595", "SPSMZ   ", "", "sensor")))
    assert rec["icao24"] == "48c595" and rec["callsign"] == "SPSMZ" and rec["_dimension"] == "air"


def test_hex_lookalike_with_a_declared_type_and_other_ids_are_left_alone():
    typed, other = socbx.unified_records(_payload(("0101B7", "X", "land"), ("POI_97948BD76D41", "P", "")))
    assert "icao24" not in typed and typed["_dimension"] == "land"
    assert "icao24" not in other


def _sample(payload):
    return mock.Mock(payload=json.dumps(payload).encode())


def test_echo_filter_drops_copies_of_our_own_sensors_only():
    echoes = EchoFilter()
    echoes.on_sample(_sample({"sensor_id": "MAINLINE-DRONU-0478D5C6", "_src": "dronuradaras.lt"}))
    echoes.on_sample(_sample({"sensor_id": "SOMEONE-ELSES-SENSOR-1", "_src": "backbone:socbx:n1"}))   # partner's own
    echoes.on_sample(_sample({"lat_deg": 1}))
    echoes.on_sample(mock.Mock(payload=b"not json"))
    for uid in ("SENSOR:MAINLINE-DRONU-0478D5C6", "SENSOR:SENSOR:MAINLINE-DRONU-0478D5C6", "MAINLINE-DRONU-0478D5C6"):
        assert echoes.is_echo({"uid": uid})
    assert not echoes.is_echo({"uid": "SENSOR:MAINLINE-DRONU-FFFFFFFF"})
    assert not echoes.is_echo({"uid": "SOMEONE-ELSES-SENSOR-1"})
    assert not echoes.is_echo({"uid": "0101B7"}) and not echoes.is_echo({})


def test_echo_filter_forgets_a_sensor_after_the_ttl():
    echoes = EchoFilter(ttl_s=10)
    echoes.on_sample(_sample({"sensor_id": "MAINLINE-DRONU-0478D5C6", "_src": "dronuradaras.lt"}))
    now = max(echoes._ids.values())
    assert echoes.is_echo({"uid": "SENSOR:MAINLINE-DRONU-0478D5C6"}, now=now + 5)
    assert not echoes.is_echo({"uid": "SENSOR:MAINLINE-DRONU-0478D5C6"}, now=now + 60)


def test_echo_filter_also_matches_our_sensor_display_name_for_uuid_keyed_copies():
    echoes = EchoFilter()
    echoes.on_sample(_sample({"sensor_id": "MAINLINE-DRONU-755A1B2C", "sensor_name": "radar-30684",
                              "_src": "dronuradaras.lt"}))
    echoes.on_sample(_sample({"sensor_id": "MAINLINE-DRONU-00000001", "sensor_name": "dronu-sensor",
                              "_src": "dronuradaras.lt"}))                     # the bridge's fallback name
    uuid_copy = {"uid": "BACKBONE-68A6EFA3-0F1E-5D2B-9C3D-123456789ABC", "callsign": "radar-30684"}
    assert echoes.is_echo(uuid_copy)
    assert echoes.is_echo({"uid": "BACKBONE-X", "label": "RADAR-30684"})
    assert not echoes.is_echo({"uid": "BACKBONE-X", "callsign": "radar-99999"})
    assert not echoes.is_echo({"uid": "BACKBONE-X", "callsign": "dronu-sensor"})


def test_echo_filter_also_recognises_copies_of_our_alert_zones_and_markers():
    echoes = EchoFilter()
    echoes.on_sample(_sample({"uid": "DA-ZONE-UA-R:вишгородський", "callsign": "UA Vyshhorodskyi District YELLOW",
                              "_src": "dangausakis.lt", "geometry": {"type": "Polygon", "coordinates": []}}))
    echoes.on_sample(_sample({"uid": "DS-UNIT-12", "callsign": "488th Motor Rifle Regiment", "_src": "deepstatemap.live"}))
    echoes.on_sample(_sample({"uid": "DA-ZONE-GONE-1234567", "callsign": "Withdrawn zone", "_delete": True}))
    echoes.on_sample(_sample({"uid": "BACKBONE-SOMEONE-ELSES-OBJECT", "callsign": "Partner thing", "_src": "backbone:json:x"}))
    assert echoes.is_echo({"uid": "BACKBONE-DA-ZONE-UA-R:вишгородський", "callsign": "x"})      # our uid inside theirs
    assert echoes.is_echo({"uid": "BACKBONE-u1", "callsign": "UA Vyshhorodskyi District YELLOW"})  # our callsign
    assert echoes.is_echo({"uid": "BACKBONE-u2", "label": "488th Motor Rifle Regiment"})
    assert not echoes.is_echo({"uid": "BACKBONE-u3", "callsign": "Withdrawn zone"})            # tombstones teach nothing
    assert not echoes.is_echo({"uid": "BACKBONE-u4", "callsign": "Partner thing"})              # theirs, not ours
    assert not echoes.is_echo({"uid": "BACKBONE-u5", "callsign": "UA Other District RED"})


def test_socbx_icao_id_may_carry_a_word_prefix():
    match = socbx._ICAO_HEX.match("STATES:502D5A")
    assert match and match.group(1) == "502D5A" and socbx._ICAO_HEX.match("502D5A")
    assert not socbx._ICAO_HEX.match("SENSOR:MAINLINE-DRONU-0478D5C6") and not socbx._ICAO_HEX.match("STATES:502D5")


def test_socbx_skips_objects_that_are_only_a_flattened_field_path():
    msg = UnifiedSchema()
    for key in ("ROOT_ATTRIBUTES", "LOCATION", "AUTO:/LOCATION"):
        features = msg.objects[key].features
        features.location.latitude, features.location.longitude = 54.6, 25.1
    real = msg.objects["abc-1"].features
    real.location.latitude, real.location.longitude = 50.0, 8.0
    real.identity.callsign = "DLH12"
    bare = msg.objects["502D5A"].features
    bare.location.latitude, bare.location.longitude = 53.0, 14.0
    uids = sorted(r["uid"] for r in socbx.unified_records(msg.SerializeToString()))
    assert uids == ["502D5A", "abc-1"]


def test_echo_filter_matches_our_uid_used_as_the_partners_callsign():
    echoes = EchoFilter()
    echoes.on_sample(_sample({"uid": "DA-ZONE-UA-R:бердянський", "callsign": "UA Berdianskyi District RED", "_src": "dangausakis.lt"}))
    assert echoes.is_echo({"uid": "BACKBONE-08E5E910-20C7-56F0", "callsign": "DA-ZONE-UA-R:бердянський"})
    assert echoes.is_echo({"uid": "BACKBONE-1", "label": "da-zone-ua-r:бердянський"})
    assert not echoes.is_echo({"uid": "BACKBONE-2", "callsign": "DA-ZONE-UA-R:other-place"})


def test_socbx_adsb_aircraft_go_to_civ_aircraft_but_other_objects_stay_units():
    msg = UnifiedSchema()
    plane = msg.objects["502D5A"].features
    plane.location.latitude, plane.location.longitude = 53.0, 14.0
    plane.identity.callsign = "BTI7HB"
    unit = msg.objects["abc-1"].features
    unit.location.latitude, unit.location.longitude = 50.0, 8.0
    unit.identity.callsign = "SOMEUNIT"
    records = {r["uid"]: r for r in socbx.unified_records(msg.SerializeToString())}
    assert (records["502D5A"]["_slot"], records["502D5A"]["_entity"]) == ("civ", "aircraft")
    assert records["abc-1"]["_slot"] == "unknown" and "_entity" not in records["abc-1"]


def test_echo_filter_recognises_an_anonymous_copy_of_a_fixed_sensor_by_position():
    sensor = {"uid": "SENS-MAINLINE-DRONU-595EFFB1", "sensor_id": "MAINLINE-DRONU-595EFFB1", "sensor_name": "radar-56138",
              "lat_deg": 54.65255, "lon_deg": 25.36503}
    echoes = EchoFilter()
    echoes.on_sample(mock.Mock(payload=json.dumps(sensor).encode()))
    copy = {"uid": "31CEFC90-DB5F-5C96-A29A-E3D373A8E22C", "callsign": "E3D373A8E22C", "lat_deg": 54.65255, "lon_deg": 25.36503}
    assert echoes.is_echo(copy)
    assert echoes.is_echo(dict(copy, lat_deg=54.65260))                         # a few metres off, still the same place
    assert not echoes.is_echo(dict(copy, lat_deg=54.7))                          # a different place
    assert not echoes.is_echo(dict(copy, icao24="e3d373"))                       # an aircraft passing over it is not a sensor copy
    moving = dict(sensor, uid="TRK-1", sensor_id=None)
    other = EchoFilter()
    other.on_sample(mock.Mock(payload=json.dumps(moving).encode()))
    assert not other.is_echo(copy)                                               # only fixed sensors count
