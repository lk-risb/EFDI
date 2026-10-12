"""What a decoded backbone record looks like on the C2 side: the topic it is published on, the CoT type and SIDC
the layers choose for it, and the name the marker carries. Built from schema-valid messages."""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols"):
    sys.path.insert(0, str(ROOT / sub))

from layers.vendors.tak import tak_layer  # noqa: E402
from layers.vendors.tak.systematic import sitaware_layer  # noqa: E402
from protocols.vendors.fpv import fpv  # noqa: E402
from protocols.vendors.palantir import palantir  # noqa: E402
from protocols.vendors.socbx import socbx  # noqa: E402
from protocols.vendors.usareur import usareur  # noqa: E402
from schemas.vendors.fpv.proto.crsf_telemetry_pb2 import TelemetryMessage  # noqa: E402
from schemas.vendors.palantir.proto.palantir_adsb_pb2 import AdsbBatch  # noqa: E402
from schemas.vendors.socbx.proto.socbx_unified_pb2 import UnifiedSchema  # noqa: E402
from schemas.vendors.usareur.proto.adsb_aircraft_pb2 import AircraftBatch  # noqa: E402


def _match(pattern: str, topic: str) -> bool:
    regex = pattern.replace("**", "\0").replace("*", "[^/]+").replace("\0", ".*")
    return re.match("^" + regex + "$", topic + "/x") is not None


def _tak(topic: str, track: dict) -> list:
    rel = topic[len(tak_layer.TOPIC_ROOT) + 1:]
    return [v(track) if callable(v) else v for p, (v, _s) in tak_layer._TOPIC_COT.items() if _match(p, rel)]


def _sidc(topic: str, track: dict) -> list:
    rel = topic[len(tak_layer.TOPIC_ROOT) + 1:]
    return [v(track) if callable(v) else v for p, v in sitaware_layer._TOPIC_SIDC.items() if _match(p, rel)]


def _topic(domain: str, affiliation: str, entity: str = "unit") -> str:
    return "{}/{}/backbone/{}/{}/tracks/v1".format(tak_layer.TOPIC_ROOT, domain, affiliation, entity)


def test_fpv_drone_is_drawn_as_an_unmanned_aircraft_named_after_its_source():
    message = TelemetryMessage()
    message.source_id = "fpv-7"
    message.gps.latitude_deg, message.gps.longitude_deg, message.gps.altitude_m = 50.1, 30.2, 120
    record = fpv.gps_record(message.SerializeToString())
    topic = _topic("air", record.pop("affiliation"))
    assert _tak(topic, record) == ["a-u-A-M-F-Q"] and _sidc(topic, record) == ["SUAPMFQ---*****"]
    assert tak_layer._callsign(record, "UID-X") == "fpv-7"


def test_partner_adsb_aircraft_get_the_same_civil_type_dangausakis_gives_them():
    batch = AdsbBatch()
    track = batch.tracks.add()
    track.icao24, track.callsign, track.lat, track.lon = "4ca2b1", "RYR123", 52.2, 21.0
    (palantir_record,) = palantir.adsb_records(batch.SerializeToString())
    usa = AircraftBatch()
    aircraft = usa.aircraft.add()
    aircraft.icao_hex, aircraft.callsign, aircraft.lat, aircraft.lon = "3c6444", "DLH9AB", 51.1, 17.0
    (usareur_record,) = usareur.aircraft_records(usa.SerializeToString())
    dangausakis_topic = "{}/air/dangausakis/civ/aircraft/tracks/v1".format(tak_layer.TOPIC_ROOT)
    for record in (palantir_record, usareur_record):
        topic = _topic("air", record.pop("affiliation"))
        assert _tak(topic, record) == _tak(dangausakis_topic, record) == [tak_layer._CIV_AIR_TYPE]
        assert _sidc(topic, record) == _sidc(dangausakis_topic, record)
    assert tak_layer._callsign(palantir_record, "UID") == "RYR123"


def test_a_partner_drone_tagged_only_by_its_telemetry_source_is_an_aircraft_not_a_ground_unit():
    message = UnifiedSchema()
    message.node_id = "n1"
    features = message.objects["drone_telemetry:deltaquad-nvrec5"].features
    features.location.latitude, features.location.longitude = 49.2, 11.9
    features.identity.object_id = "drone_telemetry:deltaquad-nvrec5"
    (record,) = socbx.unified_records(message.SerializeToString())
    assert record["_dimension"] == "air" and record["target_type"] == "uav"
    topic = _topic(record.pop("_dimension"), record.pop("_slot"), record.pop("_entity", "unit"))
    assert _tak(topic, record) == ["a-u-A-M-F-Q"]


def _sample(key: str, track: dict):
    import json
    from unittest import mock
    return mock.Mock(key_expr=key, payload=json.dumps(track).encode())


def test_sitaware_holds_back_a_loose_copy_fusion_announced_and_keeps_the_keyed_aircraft():
    import json
    from unittest import mock
    cache = sitaware_layer.NVGFeedCache(stale_s=60, max_tracks=100)
    handler = sitaware_layer.make_handler("SUAPMF----*****", cache, False)
    root = tak_layer.TOPIC_ROOT
    key = root + "/air/backbone/unknown/unit/tracks/v1"
    keyed = {"uid": "48A5C6", "icao24": "48a5c6", "callsign": "SPKOG", "lat_deg": 52.5, "lon_deg": 21.3}
    loose = {"uid": "48A5C6", "callsign": "SPKOG", "lat_deg": 52.53, "lon_deg": 21.36}
    handler(_sample(key, keyed))
    handler(_sample(key, loose))
    assert len(cache._items) == 2                                   # before fusion has said anything: two markers
    sitaware_layer._twins.on_sample(mock.Mock(payload=json.dumps({"uid": "48A5C6"}).encode()))
    handler(_sample(key, loose))
    assert len(cache._items) == 1                                   # the loose copy is removed and not re-added
    handler(_sample(key, keyed))
    assert len(cache._items) == 1                                   # the keyed aircraft stays


def test_sitaware_draws_fused_tracks_from_track_fusion():
    cache = sitaware_layer.NVGFeedCache(stale_s=60, max_tracks=100)
    topic = "{}/air/trackfusion/fused/unknown/aircraft/x/tracks/v1".format(tak_layer.TOPIC_ROOT)
    assert _sidc(topic, {}) == ["SUAPMF----*****"]
    handler = sitaware_layer.make_handler("SUAPMF----*****", cache, False)
    handler(_sample(topic, {"uid": "FUSED-1", "icao24": "123456", "callsign": "TST1", "lat_deg": 50.0, "lon_deg": 20.0}))
    assert len(cache._items) == 1
