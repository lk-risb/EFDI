"""tacvox ingest batch: names and affiliation reach the C2 layers."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

from protocols.vendors.tacvox import tacvox  # noqa: E402


def test_tak_items_get_a_callsign_and_the_affiliation_slot_of_their_cot_type():
    batch = {"tak": [
        {"id": 1, "callsign": "ALPHA-1", "type": "a-f-G-U-C", "state": "ok", "lat": 54.6, "lon": 25.3},
        {"id": 2, "summary": "contact east", "type": "a-h-G", "lat": 54.7, "lon": 25.4},
        {"id": 3, "callsign": "FARM", "type": "a-n-G", "lat": 54.8, "lon": 25.5},
        {"id": 4, "callsign": "UNK", "type": "a-u-G", "lat": 54.9, "lon": 25.6},
        {"id": 5, "callsign": "NOTYPE", "lat": 55.0, "lon": 25.7},
        {"id": 6, "callsign": "NOPOS", "type": "a-f-G"},
    ]}
    records = tacvox.tak_records(batch)
    assert [(r["callsign"], r["_slot"]) for r in records] == [
        ("ALPHA-1", "friendly"), ("contact east", "hostile"), ("FARM", "neutral"), ("UNK", "unknown"), ("NOTYPE", "unknown")]
    assert records[0]["cot_type"] == "a-f-G-U-C" and records[0]["uid"] == "tacvox-tak-1"


def test_radio_entries_are_named_after_the_speaker_and_stay_unknown():
    (record,) = tacvox.entry_records({"entries": [
        {"from": "BRAVO-2", "lat": 54.6, "lon": 25.3, "altM": 120, "headingDeg": 90, "motion": "walking"},
        {"from": "NOFIX", "lat": 0, "lon": 0}]})
    assert record["callsign"] == "BRAVO-2" and record["_slot"] == "unknown" and record["heading_deg"] == 90
    assert record["uid"] == "tacvox-BRAVO-2" and record["alt_m"] == 120


def test_unnamed_items_still_get_a_readable_callsign():
    (record,) = tacvox.tak_records({"tak": [{"id": 9, "lat": 1.0, "lon": 2.0}]})
    assert record["callsign"] == "tacvox TAK"


def test_a_marker_is_named_after_its_label_when_a_decoder_sets_no_callsign():
    from layers.vendors.tak import tak_layer  # noqa: E402
    assert tak_layer._callsign({"label": "INTERCEPTOR-7"}, "UID-1") == "INTERCEPTOR-7"
    assert tak_layer._callsign({"callsign": "ALPHA", "label": "ignored"}, "UID-1") == "ALPHA"
    assert tak_layer._callsign({}, "UID-ABCDEFGHIJKLMNOP") == "EFGHIJKLMNOP"


def test_retract_uids_builds_one_forced_delete_per_distinct_uid():
    from layers.vendors.tak import retract_uids  # noqa: E402
    events = retract_uids.delete_events(["EFDI-UID-MAPA-1-TRAIL\n", "  \n", "EFDI-UID-MAPA-2-TRAIL", "EFDI-UID-MAPA-1-TRAIL"])
    assert len(events) == 2 and all('type="t-x-d-d"' in e and 'type="u-d-f"' in e and "<__forcedelete" in e for e in events)
    assert 'uid="EFDI-UID-MAPA-2-TRAIL"' in events[1]
