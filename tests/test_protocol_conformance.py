"""Regression vectors for edition-sensitive vendor protocol mappings."""

from __future__ import annotations

from importlib import import_module
import os
from pathlib import Path
from io import BytesIO
import struct
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT / "compose"))
sys.path.insert(0, os.fspath(ROOT / "compose" / "control"))

from protocols.vendors.asterix.cat import (  # noqa: E402
    decode_cat020_record,
    decode_cat021_record,
    decode_cat034,
    decode_cat048_record,
    decode_cat62_record,
)
from protocols.vendors.sapient.flex335 import (  # noqa: E402
    _decode_location,
    _decode_range_bearing,
)


def _fixed64(number: int, value: float) -> bytes:
    return bytes(((number << 3) | 1,)) + struct.pack("<d", value)


def _varint(number: int, value: int) -> bytes:
    return bytes((number << 3, value))


def test_cat020_ed111_uap_track_status_and_velocity_alignment():
    # FRN 6 track number, FRN 7 status, FRN 9 Cartesian velocity.
    data = b"\x07\x40" + struct.pack(">H", 0x0123) + b"\x00" + struct.pack(">hh", 4, -8)
    track, pos = decode_cat020_record(data, 0)
    assert pos == len(data)
    assert track["track_num"] == 0x123
    assert track["confirmed"] is True
    assert track["velocity_east_ms"] == 1.0
    assert track["velocity_north_ms"] == -2.0


def test_cat020_ed111_preprogrammed_message_uses_seven_bit_msg_field():
    # FRN 18 I020/310: TRB bit 8 and MSG bits 7-1.
    track, pos = decode_cat020_record(b"\x01\x01\x10\x85", 0)
    assert pos == 4
    assert track["in_trouble"] is True
    assert track["preprog_msg"] == "work_in_progress"


def test_cat020_ed111_position_accuracy_dop_is_decoded_and_bounded():
    # FRN 19 I020/500, DOP subfield only.
    data = b"\x01\x01\x08\x80" + struct.pack(">HHh", 4, 8, -2)
    track, pos = decode_cat020_record(data, 0)
    assert pos == len(data)
    assert track["dop_x"] == 1.0
    assert track["dop_y"] == 2.0
    assert track["dop_xy"] == -0.5

    truncated, pos = decode_cat020_record(b"\x01\x01\x08\x80\x00", 0)
    assert pos == 5
    assert "dop_x" not in truncated


def test_cat021_ed27_uap_position_time_and_wgs84_alignment():
    # FRN 3 track number, FRN 5 applicability time, FRN 6 WGS-84 position.
    lat_raw = 1 << 22                 # 90 degrees at 180 / 2^23
    lon_raw = -(1 << 22)              # -90 degrees
    data = b"\x2c" + struct.pack(">H", 77) + (1280).to_bytes(3, "big")
    data += lat_raw.to_bytes(3, "big", signed=True) + lon_raw.to_bytes(3, "big", signed=True)
    track, pos = decode_cat021_record(data, 0)
    assert pos == len(data)
    assert track["track_num"] == 77
    assert track["position_time_s"] == 10.0
    assert track["lat_deg"] == 90.0
    assert track["lon_deg"] == -90.0


def test_cat021_ed27_high_resolution_position_uses_2_pow_30():
    data = b"\x02" + struct.pack(">ii", 1 << 29, -(1 << 29))
    track, pos = decode_cat021_record(data, 0)
    assert pos == len(data)
    assert track["lat_deg"] == 90.0
    assert track["lon_deg"] == -90.0


def test_cat021_ed27_selected_altitude_and_operational_status_flags():
    # FRNs 32/33 I021/146 and /148.
    altitude_data = b"\x01\x01\x01\x01\x18\xc0\x04\xe0\x04"
    track, pos = decode_cat021_record(altitude_data, 0)
    assert pos == len(altitude_data)
    assert track["selected_alt_ft"] == 100
    assert track["selected_alt_source_available"] is True
    assert track["selected_alt_source"] == "mcp_fcu"
    assert track["final_alt_ft"] == 100
    assert track["managed_vertical_mode"] is True
    assert track["altitude_hold_mode"] is True
    assert track["approach_mode"] is True

    # FRN 36 I021/008.
    status_data = b"\x01\x01\x01\x01\x01\x80\xff"
    status, pos = decode_cat021_record(status_data, 0)
    assert pos == len(status_data)
    assert status["acas_ra_active"] is True
    assert status["trajectory_change_capability"] == "reserved"
    assert status["target_state_report_capable"] is True
    assert status["air_ref_velocity_capable"] is True
    assert status["cdti_airborne_operational"] is True
    assert status["tcas_operational"] is False
    assert status["single_antenna"] is True


def test_cat034_ed129_fixed_site_position_and_collimation_alignment():
    # FRN 11 I034/120 and FRN 12 I034/090.
    site = struct.pack(">h", 120) + (1 << 22).to_bytes(3, "big", signed=True)
    site += (-(1 << 22)).to_bytes(3, "big", signed=True)
    msg = decode_cat034(b"\x01\x18" + site + struct.pack("bb", -64, 64))
    assert msg["site_alt_m"] == 120
    assert msg["site_lat"] == 90.0
    assert msg["site_lon"] == -90.0
    assert msg["collimation_rng_nm"] == -0.5
    assert msg["collimation_az_deg"] == pytest.approx(64 * 360 / 16384, abs=0.0001)


def test_cat034_ed129_status_and_processing_bit_layout():
    # FRN 6 I034/050: COM + PSR + MDS. FRN 7 I034/060: all subfields.
    status = bytes((0x94, 0xE6, 0xD0, 0xBE, 0x80))
    processing = bytes((0x9C, 0x56, 0xB4, 0xC0, 0xB0))
    msg = decode_cat034(b"\x06" + status + processing)
    assert msg["sys_nogo"] is True
    assert msg["rdp_chain_2"] is True
    assert msg["rdp_restart"] is True
    assert msg["psr_antenna"] == 2
    assert msg["psr_channel"] == "b"
    assert msg["psr_status"] == "overload"
    assert msg["psr_overload"] is True
    assert msg["psr_msc_connected"] is True
    assert msg["mds_channel"] == "a"
    assert msg["mds_status"] == "overload"
    assert msg["mds_msc_connected"] is False
    assert msg["mds_scf_channel"] == 2
    assert msg["mds_dlf_channel"] == 2
    assert msg["mds_scf_overload"] is False
    assert msg["mds_dlf_overload"] is True
    assert msg["rdp_reduction_level"] == 5
    assert msg["xmt_reduction_level"] == 3
    assert msg["psr_polarization"] == "circular"
    assert msg["psr_reduction_level"] == 3
    assert msg["psr_stc_map"] == 1
    assert msg["ssr_reduction_level"] == 6
    assert msg["mds_reduction_level"] == 5
    assert msg["mds_cluster_state"] is True


def test_cat048_ed132_warning_and_doppler_are_not_legacy_fields():
    # FRN 16 warning code 6 (terrestrial vehicle), FRN 20 calculated Doppler.
    data = b"\x01\x01\x44" + bytes((6 << 1, 0x80)) + struct.pack(">H", 0x03F6)
    track, pos = decode_cat048_record(data, 0, None, None)
    assert pos == len(data)
    assert track["warning_error_names"] == ["terrestrial_vehicle"]
    assert track["on_ground"] is True
    assert track["doppler_ms"] == -10


def test_cat048_ed132_mode3a_and_flight_level_flags_are_not_payload():
    squawk, pos = decode_cat048_record(b"\x08\xe1\x23", 0, None, None)
    assert pos == 3
    assert squawk["squawk"] == "0443"
    assert squawk["squawk_invalid"] is True
    assert squawk["squawk_garbled"] is True
    assert squawk["squawk_not_extracted"] is True

    altitude, pos = decode_cat048_record(b"\x04\xc0\x04", 0, None, None)
    assert pos == 3
    assert altitude["alt_baro_ft"] == 100
    assert altitude["alt_baro_invalid"] is True
    assert altitude["alt_baro_garbled"] is True


def test_cat048_ed132_confidence_uses_only_low_twelve_quality_bits():
    track, pos = decode_cat048_record(b"\x01\x01\x20\xf0\x01", 0, None, None)
    assert pos == 5
    assert track["squawk_quality_mask"] == 1
    assert "squawk_not_transponder" not in track
    assert "squawk_garbled" not in track
    assert "squawk_smoothed" not in track


def test_cat048_ed132_compound_presence_extensions_precede_payload():
    plot, pos = decode_cat048_record(b"\x02\x81\x00\x20", 0, None, None)
    assert pos == 4
    assert plot["ssr_runlength_deg"] == pytest.approx(1.406, abs=0.001)

    doppler, pos = decode_cat048_record(
        b"\x01\x01\x04\x81\x00\x03\xf6", 0, None, None
    )
    assert pos == 7
    assert doppler["doppler_ms"] == -10


def test_cat062_ed121_reserved_frn2_consumes_no_payload():
    # Reserved FRN 2, service ID FRN 3, Cartesian position FRN 6, acceleration FRN 8.
    data = b"\x65\x80" + b"\x07"
    data += (20).to_bytes(3, "big", signed=True) + (-40).to_bytes(3, "big", signed=True)
    data += struct.pack("bb", 4, -8)
    track, pos = decode_cat62_record(data, 0)
    assert pos == len(data)
    assert track["service_id"] == 7
    assert track["x_m"] == 10.0
    assert track["y_m"] == -20.0
    assert track["ax_ms2"] == 1.0
    assert track["ay_ms2"] == -2.0


def test_cat062_ed121_confirmation_uses_cnf_bit_two():
    # FRN 13 I062/080. SRC bit 3 must not make a track tentative; CNF bit 2 does.
    confirmed, pos = decode_cat62_record(b"\x01\x04\x04", 0)
    assert pos == 3
    assert confirmed["confirmed"] is True

    tentative, pos = decode_cat62_record(b"\x01\x04\x02", 0)
    assert pos == 3
    assert tentative["confirmed"] is False


def test_cat062_ed121_aircraft_ground_speed_is_signed_and_mach_lsb_is_point_008():
    # FRN 11 I062/380, subfields 18 (GSP) and 27 (MAC).
    data = b"\x01\x10\x01\x01\x11\x04"
    data += struct.pack(">hH", -8192, 250)
    track, pos = decode_cat62_record(data, 0)
    assert pos == len(data)
    assert track["speed_ms"] == pytest.approx(-926.0, abs=0.001)
    assert track["mach"] == 2.0


def test_sapient_rejects_ambiguous_datums_instead_of_misprojecting():
    location = _fixed64(1, 25.0) + _fixed64(2, 54.0) + _varint(7, 1) + _varint(8, 0)
    with pytest.raises(ValueError, match="datum"):
        _decode_location(location)

    magnetic = (_fixed64(1, 0.0) + _fixed64(2, 90.0) + _fixed64(3, 1000.0)
                + _varint(7, 1) + _varint(8, 2))
    with pytest.raises(ValueError, match="heading transform"):
        _decode_range_bearing(magnetic)


def test_sapient_additional_information_is_not_a_content_message():
    from protocols.vendors.sapient.flex335 import SapientDecoder

    # Timestamp + node ID + status content + envelope additional_information.
    timestamp = b"\x08\x01"
    status = b"\x0a\x01s"
    frame = b"\x0a" + bytes((len(timestamp),)) + timestamp
    frame += b"\x12\x01n\x32" + bytes((len(status),)) + status
    frame += b"\x6a\x04note"
    event = SapientDecoder().decode(frame)
    assert event.kind == "status_report"


def test_misb_st0601_ber_oid_and_exact_packet_round_trip():
    module = import_module("protocols.vendors.stanag.stanag")
    value = b"\x81\x02\x01\xaa"      # BER-OID tag 130, length 1, value aa
    assert module.parse_local_set(value) == {130: b"\xaa"}

    packet = module.ST0601_LOCAL_SET_KEY + module._encode_ber(len(value)) + value
    assert module.split_klv_packet(packet) == (module.ST0601_LOCAL_SET_KEY, value)
    assert module.split_klv_packet(packet + b"trailing") is None


def test_misb_st0601_current_field_widths_and_reserved_signed_value():
    module = import_module("protocols.vendors.stanag.stanag")
    tags = {
        15: b"\x00\x00",
        18: b"\x80\x00\x00\x00",
        19: b"\x20\x00\x00\x00",
        25: b"\xff\xff",
    }
    decoded = module.decode_st0601(tags)
    assert decoded["sensor_relative_azimuth_deg"] == pytest.approx(180.0, abs=0.001)
    assert decoded["sensor_relative_elevation_deg"] == pytest.approx(45.0, abs=0.001)
    assert decoded["sensor_alt_m"] == -900.0
    assert decoded["frame_center_alt_m"] == 19000.0
    with pytest.raises(ValueError, match="reserved"):
        module.decode_signed(b"\x80\x00\x00\x00", -90.0, 90.0)
    assert "sensor_lat_deg" not in module.decode_st0601({13: b"\x80\x00\x00\x00"})


def test_misb_klv_stream_parser_handles_split_prefix_and_ber_length():
    module = import_module("protocols.vendors.stanag.stanag")

    class ChunkedStream(BytesIO):
        def read(self, _size=-1):
            return super().read(3)

    value = b"\x02\x08" + b"\x00" * 128
    packet = module.ST0601_LOCAL_SET_KEY + module._encode_ber(len(value)) + value
    stream = ChunkedStream(b"noise" + packet)
    assert list(module.parse_klv_packets(stream)) == [(module.ST0601_LOCAL_SET_KEY, value)]
