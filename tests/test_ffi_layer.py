"""ffi_layer: the TAK users as APP-11(D) FriendlyForceInformation messages, in the structure a SitaWare 6.21 IP1 server writes as FFI."""

import pathlib
import re
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols"):
    sys.path.insert(0, str(ROOT / sub))

from layers.vendors.tak.systematic import ffi_layer as ffi  # noqa: E402
from layers.vendors.tak.systematic import nffi_layer as nffi  # noqa: E402

CLIENT = {"uid": "ANDROID-7e82", "callsign": "Alpha (1)", "lat_deg": 54.76, "lon_deg": 25.26, "cot_type": "a-f-G-U-C",
          "tak_role": "Medic", "tak_platform": "ATAK-CIV", "cot_time_epoch_s": 1791885326.0}


def test_a_tak_client_is_one_ffi_message_behind_the_type_0x14_header():
    framed = ffi.track_message(CLIENT)
    assert framed[0] == 0x14 and framed[1:4] == b"\x00\x00\x00" and int.from_bytes(framed[4:8], "big") == 1791885326
    assert framed[8:14] == b"\x00\x00\x00\x00\x01\x00" and int.from_bytes(framed[14:16], "big") == len(framed) - 16
    root = ET.fromstring(framed[16:])
    assert root.tag == "{urn:nato:mtf:app-11(d):1:ffi}FriendlyForceInformation"
    assert root.find("MessageIdentifier/Standard").text == "APP-11(D)" and root.find("MessageIdentifier/Originator").text == "LTU"
    track = root.find("TrackInformation")
    assert track.find("TrackSource/System").text == "EFDI" and track.find("TrackSource/TransponderId").text == "ANDROID-7e82"
    assert track.find("TrackPositionalData/Location/LatitudeDegrees").text == "54"
    assert track.find("TrackPositionalData/Location/LatitudeMinutes04DecimalPlaces").text == "45.600"          # 54.76 degrees
    assert track.find("TrackPositionalData/Location/LongitudeDegrees").text == "025"
    assert track.find("TrackIdentificationData/UnitSymbol").text == "SFGPUSM----A---"
    assert track.find("TrackIdentificationData/SymbolStandard").text == "APP-6(B)"


def test_the_numeric_individual_is_sent_when_the_symbol_standard_is_app6d(monkeypatch):
    monkeypatch.setattr(ffi, "SYMBOL_STANDARD", "APP-6(D)")
    ident = ET.fromstring(ffi.track_message(CLIENT)[16:]).find("TrackInformation/TrackIdentificationData")
    assert re.fullmatch(r"\d{20}", ident.find("UnitSymbol").text) and ident.find("SymbolStandard").text == "APP-6(D)"


def test_the_ffi_header_does_not_follow_the_nffi_framing_setting(monkeypatch):
    monkeypatch.setattr(nffi, "FRAMING", "xml")
    assert ffi.track_message(CLIENT)[0] == 0x14


def test_what_is_no_tak_client_or_has_no_position_is_not_a_track():
    assert ffi.track_message({k: v for k, v in CLIENT.items() if k != "tak_platform"}) is None
    assert ffi.track_message(dict(CLIENT, lat_deg=0.0, lon_deg=0.0)) is None
    assert ffi.track_message({"uid": "x"}) is None


def test_the_feed_takes_the_ffi_builder():
    feed = nffi.NffiFeed(ffi.track_message)
    feed.update(CLIENT)
    assert feed.messages() and feed.messages()[0][0] == 0x14
