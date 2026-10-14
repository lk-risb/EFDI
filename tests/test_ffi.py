"""The way back: what SitaWare writes (NFFI 1.3 or APP-11(D) FFI, each behind its 16-byte header) is cut by the bridge, decoded, and an own
echo is dropped. The documents here are the ones nffi_layer and ffi_layer write, which follow what a SitaWare 6.21 server writes."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("compose", "compose/control", "compose/generated", "compose/generated/protocols"):
    sys.path.insert(0, str(ROOT / sub))

from bridges.vendors.nffi import nffi_bridge  # noqa: E402
from layers.vendors.tak.systematic import ffi_layer, nffi_layer  # noqa: E402
from protocols.vendors.nffi import ffi, nffi  # noqa: E402
from protocols.vendors.random import fusion  # noqa: E402

USER = {"uid": "ANDROID-7e82", "callsign": "Alpha (1)", "lat_deg": 54.76, "lon_deg": -25.26, "tak_role": "Medic", "tak_team": "Cyan",
        "tak_platform": "ATAK-CIV", "cot_type": "a-f-G-U-C", "cot_time_epoch_s": 1791885326.0}


def _stream_in_pieces(data: bytes, size: int):
    reader, out = nffi_bridge.Ip1Reader(), []
    for i in range(0, len(data), size):
        out.extend(reader.feed(data[i:i + size]))
    return out


def test_the_bridge_cuts_a_stream_into_documents_by_the_header_and_tells_nffi_from_ffi():
    nffi_doc, ffi_doc = nffi_layer.track_message(USER), ffi_layer.track_message(USER)
    frames = _stream_in_pieces(nffi_doc + ffi_doc + nffi_doc, 7)           # split anywhere, even inside the header
    assert [kind for kind, _ in frames] == ["nffi", "ffi", "nffi"]
    assert frames[0][1] == nffi_doc[16:] and frames[1][1] == ffi_doc[16:]


def test_the_bridge_resynchronises_on_noise_and_still_reads_a_stream_with_no_header():
    ffi_doc = ffi_layer.track_message(USER)
    assert [k for k, _ in _stream_in_pieces(b"\xff\xfe" + ffi_doc, 1000)] == ["ffi"]
    bare = ffi_doc[16:] + nffi_layer.track_message(USER)[16:]
    assert [k for k, _ in _stream_in_pieces(bare, 50)] == ["ffi", "nffi"]


def test_nffi_13_and_14_both_decode_and_keep_the_source_that_the_echo_filter_reads(monkeypatch):
    for version in ("1.3", "1.4"):
        monkeypatch.setattr(nffi_layer, "NFFI_NS", "urn:nato:fft:protocols:nffi" + version.replace(".", ""))
        (track,) = nffi.parse_nffi(nffi_layer.track_message(USER)[16:])
        assert (track["lat_deg"], track["lon_deg"], track["source_system"], track["source_subsystem"]) == (54.76, -25.26, nffi_layer.SYSTEM, "TAK")
        assert track["unit_type"] == "SFGPUSM----A---" and track["callsign"] == "Alpha -1-"
        assert fusion.is_own_force_track(track)


def test_ffi_decodes_the_position_in_degrees_and_minutes_and_the_symbol_with_its_standard(monkeypatch):
    (track,) = ffi.parse_ffi(ffi_layer.track_message(USER)[16:])
    assert abs(track["lat_deg"] - 54.76) < 1e-5 and abs(track["lon_deg"] - -25.26) < 1e-5     # a western longitude keeps its sign
    assert track["unit_type"] == "SFGPUSM----A---" and track["symbol_standard"] == "APP-6(B)"
    assert track["ffi_time_epoch_s"] == 1791885326.0 and track["affiliation"] == "friendly" and track["uid"].startswith("FFI-" + nffi_layer.SYSTEM + "-TAK-")
    assert fusion.is_own_force_track(track)
    monkeypatch.setattr(ffi_layer, "SYMBOL_STANDARD", "APP-6(D)")
    (numeric,) = ffi.parse_ffi(ffi_layer.track_message(USER)[16:])
    assert numeric["symbol_standard"] == "APP-6(D)" and numeric["unit_type"].isdigit() and len(numeric["unit_type"]) == 20
    assert ffi._domain_topic(numeric["unit_type"]).endswith("/land/nato/c2/friendly/unit")


def test_the_domain_of_a_numeric_symbol_is_its_symbol_set():
    for symbol_set, domain in (("01", "air"), ("05", "space"), ("10", "land"), ("27", "land"), ("30", "sea"), ("35", "sea"), ("99", "land")):
        symbol = "1003" + symbol_set + "0000110000" + "4600"                       # version, context, identity, set, ... 20 digits
        assert ffi._domain_topic(symbol).endswith("/{}/nato/c2/friendly/unit".format(domain))


def test_the_source_system_names_this_pod_and_a_partner_pod_is_not_taken_for_ours(monkeypatch):
    monkeypatch.setenv("PARTNER_NAMESPACE", "LTU/CISB/zenoh_gateway")
    assert fusion.own_force_track_system() == "EFDI-LTU-CISB-ZENOH-GATEWAY"
    ours = {"source_system": "EFDI-LTU-CISB-ZENOH-GATEWAY", "source_subsystem": "TAK"}
    partner_pod = {"source_system": "EFDI-LVA-RIGA-POD", "source_subsystem": "TAK"}       # the same product, another pod: kept
    old_untagged = {"source_system": "EFDI", "source_subsystem": "TAK"}
    for kept in (partner_pod, old_untagged, {"source_system": "SITAWARE", "source_subsystem": "TAK"}, {}):
        assert not fusion.is_own_force_track(kept)
    assert fusion.is_own_force_track(ours)
    monkeypatch.setenv("PARTNER_NAMESPACE", "A" * 60 + "/B")
    assert len(fusion.own_force_track_system()) <= 30                                      # the NFFI field takes 30


def test_a_transponder_id_this_pod_published_is_ours_whatever_the_tag(monkeypatch):
    import json
    from unittest import mock
    echoes = fusion.EchoFilter()
    own = {"uid": "ANDROID-7e8219c6362df24e", "callsign": "Alpha", "lat_deg": 54.7, "lon_deg": 25.2}
    echoes.on_sample(mock.Mock(payload=json.dumps(own).encode()))
    untagged = {"source_system": "EFDI", "source_subsystem": "TAK", "transponder_id": "ANDROID-7e8219c6362df24e"}
    assert fusion.is_own_force_track(untagged, echoes)                  # an old, untagged copy of one of our users
    assert not fusion.is_own_force_track(dict(untagged, transponder_id="ANDROID-aaaaaaaaaaaaaaaa"), echoes)      # a partner's user
    assert not fusion.is_own_force_track(dict(untagged, transponder_id="short"), echoes)
    assert not fusion.is_own_force_track(untagged)                      # no filter, no id match


def test_the_version_profile_sets_defaults_and_a_setting_of_its_own_wins(monkeypatch):
    from layers.vendors.tak.systematic import sitaware_profile as profile
    monkeypatch.delenv("FFI_LAYER_SYMBOL_STANDARD", raising=False)
    monkeypatch.setattr(profile, "VERSION", "6.21")
    assert profile.setting("FFI_LAYER_SYMBOL_STANDARD") == "APP-6(B)" and profile.setting("NFFI_LAYER_VERSION") == "1.3"
    assert profile.setting("SITAWARE_TAK_INDIVIDUAL_SCHEME") == ""
    monkeypatch.setattr(profile, "VERSION", "6.22")
    assert profile.setting("FFI_LAYER_SYMBOL_STANDARD") == "APP-6(B)" and profile.setting("NFFI_LAYER_VERSION") == "1.3"
    assert profile.setting("SITAWARE_TAK_INDIVIDUAL_SCHEME") == "app06" and profile.setting("SITAWARE_TAK_USERS_NVG") == "true"
    monkeypatch.setenv("FFI_LAYER_SYMBOL_STANDARD", "APP-6(D)")                       # mix and match: 6.22 with the numeric symbol over FFI
    assert profile.setting("FFI_LAYER_SYMBOL_STANDARD") == "APP-6(D)"
    monkeypatch.setattr(profile, "VERSION", "9.9")
    assert profile.profile() is profile.PROFILES["6.21"]


def test_tak_remarks_say_a_ground_track_came_from_a_partner_c2_by_nffi_or_ffi_and_which_system():
    from layers.vendors.tak import tak_layer
    track = {"_src": "ffi", "uid": "FFI-SITAWARE-SW-1", "callsign": "Bravo", "lat_deg": 54.7, "lon_deg": 25.2,
             "source_system": "SITAWARE", "source_subsystem": "SW"}
    text = tak_layer._build_remarks(track, "a-f-G-U-C")
    assert "FEED: FFI (APP-11 Ed D)" in text and "SOURCE: SITAWARE / SW" in text
    assert "FEED: NFFI" in tak_layer._build_remarks(dict(track, _src="nffi"), "a-f-G-U-C")
    assert "FEED" not in tak_layer._build_remarks({"_src": "tak", "callsign": "Alpha", "lat_deg": 54.7, "lon_deg": 25.2}, "a-f-G-U-C")


def test_a_sitaware_track_gets_the_cot_type_of_its_symbol_in_tak():
    from layers.vendors.tak import tak_layer
    from protocols.vendors.random import ground_unit as gu
    cot = gu.friendly_unit_cot_type
    for symbol, expected in (("SFGPUCI----A---", "a-f-G-U-C-I"), ("SFGPUCIZ---A---", "a-f-G-U-C-I-Z"), ("SFGPUCA--------", "a-f-G-U-C-A"),
                             ("SFGPUSM----A---", "a-f-G-U-S-M"), ("SFGPUUS--------", "a-f-G-U-U-S"),
                             ("SFGPU----------", "a-f-G-U-C"), ("SFGPUCX--------", "a-f-G-U-C"),       # no function / one not checked
                             ("SFAPMF---------", "a-f-G-U-C"), ("", "a-f-G-U-C"), (None, "a-f-G-U-C"), ("SHGPUCI--------", "a-f-G-U-C-I")):
        assert cot(symbol) == expected, (symbol, cot(symbol))
    app6d = lambda symbol_set, entity: "1003" + symbol_set + "0000" + entity + "0000"
    for symbol_set, entity, expected in (("10", "121100", "a-f-G-U-C-I"), ("10", "120500", "a-f-G-U-C-A"), ("10", "130300", "a-f-G-U-C-F"),
                                         ("10", "161300", "a-f-G-U-S-M"), ("27", "110101", "a-f-G-U-C-I"), ("27", "110104", "a-f-G-U-U-S"),
                                         ("10", "999999", "a-f-G-U-C"), ("27", "110000", "a-f-G-U-C"), ("15", "121100", "a-f-G-U-C")):
        assert cot(app6d(symbol_set, entity)) == expected, (symbol_set, entity, cot(app6d(symbol_set, entity)))
    fn = tak_layer._TOPIC_COT["land/**/friendly/unit/**"][0]
    assert fn({"unit_type": "SFGPUCI----A---"}) == "a-f-G-U-C-I" and fn({}) == "a-f-G-U-C"          # a TAK user has no unit_type
