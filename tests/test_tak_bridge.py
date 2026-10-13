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


def test_a_clients_role_and_type_pick_the_sitaware_symbol_only_when_known():
    from layers.vendors.tak.systematic import sitaware_layer
    topic, record = bridge._normalize_event(ET.fromstring(
        '<event version="2.0" uid="A1" type="a-f-G-U-C" how="m-g" time="2026-10-13T13:00:00Z" start="2026-10-13T13:00:00Z" '
        'stale="2026-10-13T13:05:00Z"><point lat="54.7" lon="25.3" hae="1" ce="1" le="1"/>'
        '<detail><contact callsign="Alpha"/><__group name="Cyan" role="HQ"/></detail></event>'))
    assert record["tak_role"] == "HQ" and record["cot_detail"]["tak_team"] == "Cyan"
    sidc = sitaware_layer._friendly_unit_sidc
    assert sidc(record) == "SFGPUH----*****"                                          # headquarters
    assert sidc(dict(record, tak_role="Medic")) == "SFGPUSM---*****"
    assert sidc(dict(record, tak_role="RTO")) == "SFGPUUS---*****"
    assert sidc(dict(record, tak_role="Team Lead")) == "SFGPU-----*****"              # no checked entry: the plain unit
    assert sidc(dict(record, tak_role=None, cot_type="a-f-G-U-C-I-Z")) == "SFGPUCIZ--*****"
    assert sidc(dict(record, tak_role=None, cot_type="a-f-G-U-C")) == "SFGPU-----*****"
    assert sidc({"uid": "nffi", "cot_type": "a-f-G-U-C-I"}) == "SFGPU-----*****"       # not from TAK: as before
