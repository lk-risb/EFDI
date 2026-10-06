"""Partner ADS-B feeds: bare-hex objects are aircraft, and bit-flipped duplicate addresses are dropped."""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))
sys.path.insert(0, str(ROOT / "compose" / "layers" / "vendors" / "tak"))

from protocols.vendors.random.icao_ghosts import GhostFilter  # noqa: E402
import tak_layer  # noqa: E402

_spec = importlib.util.spec_from_file_location("generic_json_under_test", ROOT / "compose/protocols/vendors/random/generic_json.py")
sys.path.insert(0, str(ROOT / "compose" / "protocols" / "vendors" / "random"))
generic_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generic_json)


def test_generic_json_object_named_by_a_bare_icao_hex_is_an_aircraft():
    record, dimension, _ = generic_json.normalize(
        {"ci_uuid": "acfc7b06-07b8-5a66", "ci_name": "4007F2", "lat": 51.5, "lon": 0.1}, "origin1")
    assert dimension == "air" and record["icao24"] == "4007f2" and record["target_type"] == "aircraft"
    assert record["uid"] == "BACKBONE-acfc7b06-07b8-5a66"          # a track identity of its own, merged by icao24
    xml_uid = tak_layer._uid(record)
    assert xml_uid == "EFDI-ICAO-4007F2"                              # the same marker as the ICAO-keyed feeds


def test_generic_json_leaves_other_objects_alone():
    for payload in ({"ci_uuid": "u1", "ci_name": "radar-30684", "lat": 50, "lon": 30},
                    {"ci_uuid": "u2", "ci_name": "4007F2", "lat": 50, "lon": 30, "type": "vehicle"},
                    {"ci_uuid": "u3", "ci_name": "4007F2", "lat": 50, "lon": 30, "dimension": "sea"}):
        record, dimension, _ = generic_json.normalize(payload, "o")
        assert "icao24" not in record and dimension in ("land", "sea")


def test_ghost_filter_drops_bit_flipped_addresses_with_the_same_callsign_only():
    ghosts = GhostFilter()
    assert not ghosts.is_ghost("4c11ec", "ASL124", now=0)
    assert ghosts.is_ghost("4c01ec", "ASL124", now=1)               # one bit away: a decoding error
    assert not ghosts.is_ghost("4c11ec", "ASL124", now=2)           # the real address keeps passing
    assert not ghosts.is_ghost("4c0000", "ASL124", now=3)           # far away: a different aircraft
    assert not ghosts.is_ghost("4c01ec", "OTHER1", now=3)           # another callsign
    assert not ghosts.is_ghost("123456", "", now=3) and not ghosts.is_ghost("", "ASL124", now=3)


def test_ghost_filter_forgets_after_the_window():
    ghosts = GhostFilter(window_s=100)
    assert not ghosts.is_ghost("c2c36d", "CFC3455", now=0)
    assert ghosts.is_ghost("82c36d", "CFC3455", now=50)
    assert not ghosts.is_ghost("82c36d", "CFC3455", now=500)       # the first address is long gone


def test_ghost_filter_also_matches_on_registration_when_the_callsign_differs():
    ghosts = GhostFilter()
    assert not ghosts.is_ghost("4C11EC", "ASL124", registration="LN-ABC")
    assert ghosts.is_ghost("4C01EC", "ASL999", registration="LN-ABC")       # new flight number, same airframe
    assert not ghosts.is_ghost("4C01EC", "ASL999", registration="LN-XYZ")   # a different airframe is left alone


def test_generic_json_object_id_with_a_word_prefix_is_still_the_icao_aircraft():
    record, dimension, _ = generic_json.normalize({"ci_uuid": "STATES:502D5A", "ci_name": "BTI7HB", "lat": 53.3, "lon": 14.5}, "o")
    assert dimension == "air" and record["icao24"] == "502d5a" and record["callsign"] == "BTI7HB"
    assert tak_layer._uid(record) == "EFDI-ICAO-502D5A"           # one marker with the ICAO-keyed feeds, not a ground unit


def test_twin_filter_drops_a_non_icao_copy_of_an_icao_keyed_aircraft_only_when_near_and_recent():
    from protocols.vendors.random.icao_ghosts import TwinFilter
    twins = TwinFilter()
    icao = {"icao24": "505ce9", "callsign": "OMCBS", "lat_deg": 49.22, "lon_deg": 19.62}
    assert not twins.is_twin(icao, now=1000.0)
    assert twins.is_twin({"callsign": "omcbs", "lat_deg": 49.40, "lon_deg": 19.62}, now=1030.0)      # about 20 km away
    assert not twins.is_twin({"callsign": "OMCBS", "lat_deg": 52.0, "lon_deg": 19.62}, now=1030.0)   # a different place
    assert not twins.is_twin({"callsign": "OMCBS", "lat_deg": 49.22, "lon_deg": 19.62}, now=1500.0)  # too long ago
    assert not twins.is_twin({"callsign": "OTHER1", "lat_deg": 49.22, "lon_deg": 19.62}, now=1030.0)
    assert not twins.is_twin({"lat_deg": 49.22, "lon_deg": 19.62}, now=1030.0)                       # no callsign
