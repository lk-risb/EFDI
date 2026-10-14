"""tak_bridge: only atoms (a-...) become tracks, and the connection defaults to the tak_layer one (mTLS 8089)."""

import importlib.util
import pathlib
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols"):
    sys.path.insert(0, str(ROOT / sub))

_spec = importlib.util.spec_from_file_location("tak_bridge", ROOT / "compose/bridges/vendors/tak/tak_bridge.py")
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def _event(cot_type, uid="ANDROID-1"):
    return ET.fromstring(
        '<event version="2.0" uid="{}" type="{}" how="m-g" time="2026-10-13T13:00:00Z" start="2026-10-13T13:00:00Z" '
        'stale="2026-10-13T13:05:00Z"><point lat="54.7" lon="25.3" hae="100" ce="10" le="10"/><detail/></event>'.format(uid, cot_type))


def test_a_client_position_becomes_a_friendly_land_unit_track():
    topic, record = bridge._normalize_event(_event("a-f-G-U-C"))
    assert topic.endswith("/land/tak/cot/friendly/unit") and record["lat_deg"] == 54.7 and record["_ingress"] == "tak_server"


def test_tasking_and_deletion_frames_are_not_tracks():
    for cot_type in ("t-x-d-d", "t-x-takp-v", "u-d-f", "b-t-f"):
        assert bridge._normalize_event(_event(cot_type)) is None, cot_type


def test_the_connection_defaults_to_the_tak_layer_one_and_tak_ingest_overrides_it(monkeypatch):
    for name in ("TAK_INGEST_PORT", "TAK_INGEST_TLS", "TAK_PORT", "TAK_TLS"):
        monkeypatch.delenv(name, raising=False)
    seen = {}
    monkeypatch.setattr(bridge, "run", lambda args: seen.update(port=args.port, tls=args.tls))
    bridge.main(["--host", "x"])
    assert seen == {"port": 8089, "tls": True}
    monkeypatch.setenv("TAK_PORT", "9000")
    monkeypatch.setenv("TAK_INGEST_TLS", "0")
    bridge.main(["--host", "x"])
    assert seen == {"port": 9000, "tls": False}


def test_a_tak_client_is_the_app6d_dismounted_individual_by_role_and_type():
    from layers.vendors.tak.systematic import sitaware_layer
    xml = ('<event version="2.0" uid="A1" type="{}" how="m-g" time="2026-10-13T13:00:00Z" start="2026-10-13T13:00:00Z" '
           'stale="2026-10-13T13:05:00Z"><point lat="54.7" lon="25.3" hae="1" ce="1" le="1"/>'
           '<detail><takv device="Pixel" platform="ATAK-CIV"/><contact callsign="Alpha"/><__group name="Cyan" role="{}"/></detail></event>')
    sitaware_layer._INDIVIDUAL_SCHEME = "app6d"
    sidc = sitaware_layer._friendly_unit_sidc
    topic, record = bridge._normalize_event(ET.fromstring(xml.format("a-f-G-U-C", "HQ")))
    assert record["tak_role"] == "HQ" and record["tak_team"] == "Cyan" and record["callsign"] == "Alpha"
    assert sidc(record) == "10032700001100004600"                                  # set 27, military, individual
    assert sidc(dict(record, tak_role="Medic")) == "10032700001101024600"
    assert sidc(dict(record, tak_role="RTO")) == "10032700001101044600"
    assert sidc(dict(record, tak_role="Sniper")) == "10032700001102094600"
    assert sidc(dict(record, tak_role="Forward Observer")) == "10032700001102024600"
    assert sidc(dict(record, tak_role="Team Lead")) == "10032700001100004600"
    assert sidc(dict(record, tak_role=None, cot_type="a-f-G-U-C-I")) == "10032700001101014600"
    assert sidc(dict(record, tak_role=None, cot_type="a-f-G-U-C-R")) == "10032700001101034600"
    assert len(sidc(record)) == 20 and sidc(record).isdigit()
    assert sitaware_layer._callsign(record, "U") == "Alpha (Cyan)" and sitaware_layer._callsign(dict(record, _ingress="x"), "U") == "Alpha"
    # the numeric SIDC goes out under the APP-6D scheme, and a TAK marker that is no client keeps the unit frame
    item = sitaware_layer.track_to_nvg_item(record, sidc(record), "2525b")
    assert 'symbol="app6d:1003270000110000' in item[1]
    marker = dict(record, tak_platform=None, tak_device=None, tak_role=None, tak_team=None)
    assert sidc(marker) == "SFGPU-----*A***" and sidc(dict(marker, cot_type="a-f-G-U-C-I-Z")) == "SFGPUCIZ--*A***"
    assert sidc({"uid": "nffi", "cot_type": "a-f-G-U-C-I"}) == "SFGPU-----*****"       # not from TAK: as before
    assert sitaware_layer._cot_type_for_sidc(sidc(record)) == "a-f-G"
    sitaware_layer._INDIVIDUAL_SCHEME = ""                                              # off (the default): the unit frame with team/crew
    assert sidc(dict(record, tak_role="Medic")) == "SFGPUSM---*A***"
    assert sidc(record) == "SFGPUH----*A***"


def test_a_client_with_no_gps_fix_reports_0_0_and_is_not_a_track():
    event = _event("a-f-G-U-C")
    event.find("point").set("lat", "0.0")
    event.find("point").set("lon", "0.0")
    assert bridge._normalize_event(event) is None


def test_a_client_with_no_gps_fix_reports_0_0_and_is_not_a_track():
    event = _event("a-f-G-U-C")
    event.find("point").set("lat", "0.0")
    event.find("point").set("lon", "0.0")
    assert bridge._normalize_event(event) is None


def test_a_clients_last_position_is_repeated_until_its_own_stale_time():
    now = 1000.0
    known = {"A": ("t/a", {"uid": "A", "stale_s": 120.0, "lat_deg": 1.0}, now),
             "B": ("t/b", {"uid": "B", "stale_s": 30.0}, now - 100.0),          # spent 70 s ago
             "C": ("t/c", {"uid": "C"}, now - 10.0)}                              # no stale time: the default of 5 minutes
    due = {topic: record for topic, record in bridge._due_republish(known, now)}
    assert set(due) == {"t/a", "t/c"} and "B" not in known
    assert due["t/a"]["_ts"] == now and due["t/a"]["stale_s"] == 120.0 and due["t/a"]["lat_deg"] == 1.0
    assert due["t/c"]["stale_s"] == 290.0
    assert bridge._due_republish(known, now + 400.0) == [] and known == {}


def _frame(cot_type, detail, uid="ANDROID-1-9-1-1"):
    return ET.fromstring('<event version="2.0" uid="{}" type="{}" how="h-e" time="2026-10-13T13:00:00Z" start="2026-10-13T13:00:00Z" '
                         'stale="2026-10-13T13:05:00Z"><point lat="54.7" lon="25.3" hae="0" ce="10" le="10"/><detail>{}</detail></event>'
                         .format(uid, cot_type, detail))


def test_a_tak_emergency_frame_names_its_client_and_a_cancel_clears_it():
    alert = _frame("b-a-o-tbl", '<link uid="ANDROID-1" relation="p-p" type="a-f-G-U-C"/><emergency type="911 Alert">Alpha</emergency>')
    assert bridge._emergency(alert) == ("ANDROID-1", "911 Alert")
    assert bridge._emergency(_frame("b-a-o-tbl", ""))[0] == "ANDROID-1"                      # the uid suffix names the client
    assert bridge._emergency(_frame("b-a-o-can", '<emergency cancel="true"/>')) == ("ANDROID-1", None)
    assert bridge._emergency(_event("a-f-G-U-C")) is None and bridge._emergency(_event("t-x-d-d")) is None
    assert bridge._normalize_event(alert) is None                                              # still not a track of its own
