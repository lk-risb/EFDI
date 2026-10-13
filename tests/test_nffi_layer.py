"""nffi_layer: TAK clients become NFFI 1.4 tracks that follow the NC3A schema's patterns, and a client gets the table on connect."""

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
    return ET.fromstring(nffi.track_message(track))


def test_a_tak_client_is_one_nffi_track_that_fits_the_schema_patterns():
    root = _doc(CLIENT)
    assert root.tag == "{%s}NFFIMessage" % nffi.NFFI_NS
    text = lambda path: root.find(path, NS).text
    assert re.fullmatch(r"[A-Z]{3}", text("n:track/n:positionalData/n:trackSource/n:sourceSystem/n:country"))
    assert text("n:track/n:positionalData/n:trackSource/n:sourceSystem/n:system") == "EFDI"
    assert re.fullmatch(r"[A-Za-z0-9.,:*\-]{1,60}", text("n:track/n:positionalData/n:trackSource/n:transponderId"))
    assert re.fullmatch(r"\d{14}", text("n:track/n:positionalData/n:dateTime"))
    assert float(text("n:track/n:positionalData/n:coordinates/n:latitude")) == 54.76
    assert 0 <= float(text("n:track/n:positionalData/n:bearing")) < 360                       # 370 wraps to 10
    assert float(text("n:track/n:positionalData/n:speed")) == 7.2                             # 2 m/s in km/h
    assert re.fullmatch(r"[A-Z0-9\-]{15}", text("n:track/n:identificationData/n:unitSymbol"))
    assert text("n:track/n:identificationData/n:unitSymbol") == "SFGPUSM----A---"            # friendly ground, medical, team/crew
    assert re.fullmatch(r"[A-Za-z0-9.,:*\- ]{1,21}", text("n:track/n:identificationData/n:unitShortName"))
    assert text("n:track/n:operStatusData/n:remarks") == "Cyan Medic"


def test_what_is_no_tak_client_or_has_no_position_is_not_a_track():
    assert nffi.track_message({k: v for k, v in CLIENT.items() if k != "tak_platform"}) is None      # a marker a user placed
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
