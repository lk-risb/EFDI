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
