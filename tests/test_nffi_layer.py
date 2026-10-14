"""nffi_layer: TAK clients become NFFI 1.3 tracks that follow the NC3A schema's patterns, and a client gets the table on connect."""

import importlib.util
import pathlib
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols"):
    sys.path.insert(0, str(ROOT / sub))

_spec = importlib.util.spec_from_file_location("nffi_layer", ROOT / "compose/layers/vendors/tak/systematic/nffi_layer.py")
nffi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nffi)
NS = {"n": nffi.NFFI_NS}
CLIENT = {"uid": "ANDROID-7e82", "callsign": "Alpha (1)", "lat_deg": 54.76, "lon_deg": 25.26, "alt_m": 100.0, "heading_deg": 370.0,
          "speed_ms": 2.0, "cot_type": "a-f-G-U-C", "tak_role": "Medic", "tak_team": "Cyan", "tak_platform": "ATAK-CIV",
          "cot_time_epoch_s": 1791885326.0}


def _doc(track):
    return ET.fromstring(nffi.track_message(track)[16:])      # behind the 16-byte header


def test_a_tak_client_is_one_nffi_track_that_fits_the_schema_patterns():
    root = _doc(CLIENT)
    assert root.tag == "{%s}NFFIMessage" % nffi.NFFI_NS
    text = lambda path: root.find(path, NS).text
    assert re.fullmatch(r"[A-Z]{3}", text("n:track/n:positionalData/n:trackSource/n:sourceSystem/n:country"))
    assert text("n:track/n:positionalData/n:trackSource/n:sourceSystem/n:system") == nffi.SYSTEM
    assert re.fullmatch(r"[A-Za-z0-9.,:*\-]{1,60}", text("n:track/n:positionalData/n:trackSource/n:transponderId"))
    assert re.fullmatch(r"\d{14}", text("n:track/n:positionalData/n:dateTime"))
    assert float(text("n:track/n:positionalData/n:coordinates/n:latitude")) == 54.76
    assert 0 <= float(text("n:track/n:positionalData/n:bearing")) < 360                       # 370 wraps to 10
    assert float(text("n:track/n:positionalData/n:speed")) == 7.2                             # 2 m/s in km/h
    assert re.fullmatch(r"[A-Z0-9\-]{15}", text("n:track/n:identificationData/n:unitSymbol"))
    assert text("n:track/n:identificationData/n:unitSymbol") == "SFGPUSM----A---"            # friendly ground, medical, team/crew
    assert re.fullmatch(r"[A-Za-z0-9.,:*\- ]{1,21}", text("n:track/n:identificationData/n:unitShortName"))
    assert text("n:track/n:operStatusData/n:remarks") == "Cyan Medic"


def test_a_tak_client_without_a_role_is_infantry_the_letter_code_counterpart_of_the_individual():
    assert nffi.unit_symbol({"cot_type": "a-f-G-U-C"}) == "SFGPUCI----A---"


def test_what_is_no_tak_client_or_has_no_position_is_not_a_track():
    assert nffi.track_message({k: v for k, v in CLIENT.items() if not k.startswith("tak_")}) is None      # a marker a user placed
    assert nffi.track_message({k: v for k, v in CLIENT.items() if k in ("uid", "lat_deg", "lon_deg", "tak_team")}) is not None   # no device, but a team: a user (iTAK, WinTAK)
    assert nffi.track_message(dict(CLIENT, lat_deg=0.0, lon_deg=0.0)) is None
    assert nffi.track_message(dict(CLIENT, lat_deg=95.0)) is None
    assert nffi.track_message({"uid": "x"}) is None


def test_a_connecting_client_gets_the_table_and_then_the_updates_and_stale_tracks_drop():
    feed = nffi.NffiFeed()
    feed.update(CLIENT)
    stop = threading.Event()
    server = feed.serve("127.0.0.1", 0, stop)
    try:
        client = socket.create_connection(server.getsockname(), timeout=3)
        first = client.recv(65536)
        assert b"<NFFIMessage" in first and b"ANDROID-7e82" in first
        feed.update(dict(CLIENT, lat_deg=54.77))
        time.sleep(0.2)
        assert b"54.77" in client.recv(65536)
        feed.update({"uid": "ANDROID-7e82", "_delete": True})
        assert feed.messages() == []
    finally:
        stop.set()
        client.close()
    feed.update(CLIENT, now=1000.0)
    assert feed.messages(now=1000.0 + nffi.STALE_S + 1) == []


def test_only_the_allowed_source_addresses_get_the_feed():
    feed = nffi.NffiFeed()
    feed.update(CLIENT)
    stop = threading.Event()
    refused = feed.serve("127.0.0.1", 0, stop, allow={"10.9.9.9"})
    try:
        client = socket.create_connection(refused.getsockname(), timeout=3)
        client.settimeout(1.0)
        assert client.recv(65536) == b""                              # closed without a byte
        client.close()
        assert feed._clients == []
    finally:
        stop.set()


def test_the_layer_connects_out_to_a_listening_client_and_reconnects():
    feed = nffi.NffiFeed()
    feed.update(CLIENT)
    peer = socket.socket()
    peer.bind(("127.0.0.1", 0))
    peer.listen(1)
    stop = threading.Event()
    feed.connect_out("127.0.0.1", peer.getsockname()[1], stop, retry_s=0.2)
    try:
        conn, _ = peer.accept()
        conn.settimeout(3)
        assert b"ANDROID-7e82" in conn.recv(65536)                        # the table on connect
        feed.update(dict(CLIENT, lat_deg=54.78))
        assert b"54.78" in conn.recv(65536)                               # then the updates
        conn.close()                                                      # the peer goes away: the layer dials again
        peer.settimeout(5)
        again, _ = peer.accept()
        again.settimeout(3)
        assert b"NFFIMessage" in again.recv(65536)
    finally:
        stop.set()
        peer.close()


def test_the_default_is_what_sitaware_621_writes_itself_a_16_byte_header_and_nffi13():
    framed = nffi.track_message(CLIENT)
    assert nffi.NFFI_NS == "urn:nato:fft:protocols:nffi13"
    assert framed[:4] == b"\x04\x00\x00\x00" and int.from_bytes(framed[4:8], "big") == 1791885326     # the position time, epoch seconds
    assert framed[8:14] == b"\x00\x00\x00\x00\x01\x00" and int.from_bytes(framed[14:16], "big") == len(framed) - 16
    assert framed[16:].startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<NFFIMessage') and framed.endswith(b"</NFFIMessage>\n")
    assert b'secClassification="UNCLASSIFIED"' in framed


def test_an_emergency_is_the_alert_flag_before_the_remark_in_operstatusdata():
    framed = nffi.track_message(dict(CLIENT, emergency_str="medic"))
    assert b"<alert>true</alert><remarks>EMERGENCY MEDIC" in framed and b"deviceSpecificData" not in framed
    assert b"<alert>" not in nffi.track_message(CLIENT)


def test_the_header_system_subsystem_and_country_are_optional_and_range_checked(monkeypatch):
    monkeypatch.setattr(nffi, "HEADER_SYSTEM", 7)
    monkeypatch.setattr(nffi, "HEADER_SUBSYSTEM", 9)
    monkeypatch.setattr(nffi, "HEADER_COUNTRY", 440)
    header = nffi.ip1_header(0x04, 5, 100)
    assert header[8:12] == bytes([7, 9]) + (440).to_bytes(2, "big") and header[12:14] == b"\x01\x00"
    monkeypatch.setenv("NFFI_LAYER_HEADER_COUNTRY", "5000")
    assert nffi._header_number("NFFI_LAYER_HEADER_COUNTRY", 1024) == 0
    monkeypatch.setenv("NFFI_LAYER_HEADER_COUNTRY", "440")
    assert nffi._header_number("NFFI_LAYER_HEADER_COUNTRY", 1024) == 440


def test_the_framing_can_be_changed_by_the_environment_variable(monkeypatch):
    monkeypatch.setattr(nffi, "FRAMING", "nodecl")
    plain = nffi.track_message(CLIENT)
    assert plain.startswith(b"<") and not plain.startswith(b"<?xml") and plain.endswith(b"\n")
    monkeypatch.setattr(nffi, "FRAMING", "length")
    framed = nffi.track_message(CLIENT)
    assert int.from_bytes(framed[:4], "big") == len(framed) - 4 and framed[4:].startswith(b"<?xml")
    monkeypatch.setattr(nffi, "FRAMING", "nul")
    assert nffi.track_message(CLIENT).endswith(b"\x00")
    monkeypatch.setattr(nffi, "FRAMING", "xml")
    assert nffi.track_message(CLIENT).startswith(b"<?xml") and nffi.track_message(CLIENT).endswith(b"\n")

