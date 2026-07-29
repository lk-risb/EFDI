"""dronuradaras publishes online sensors and one-shot offline tombstones."""

import json
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "bridges"))

import dronuradaras_bridge as bridge  # noqa: E402


class FakePublisher:
    def __init__(self):
        self.records = []

    def put(self, payload, **_kwargs):
        self.records.append(json.loads(payload))


def device(device_id="device-0001", online=False):
    return {
        "id": device_id,
        "display_name": "Test radar",
        "latitude": 54.7,
        "longitude": 25.3,
        "is_online": online,
    }


@pytest.fixture(autouse=True)
def reset_device_state():
    with bridge._device_lock:
        bridge._device_names.clear()
        bridge._device_positions.clear()
        bridge._last_detection.clear()
        bridge._online_devices.clear()
        bridge._offline_announced.clear()


def run_polls(monkeypatch, polls):
    publisher = FakePublisher()
    responses = iter({"devices": devices} for devices in polls)
    monkeypatch.setattr(bridge, "_get", lambda _path: next(responses))
    sleep_count = 0

    def stop_after_polls(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= len(polls):
            raise StopIteration

    monkeypatch.setattr(bridge.time, "sleep", stop_after_polls)
    with pytest.raises(StopIteration):
        bridge.run_devices(publisher, verbose=False)
    return publisher.records


def test_offline_tombstone_is_not_republished_every_poll(monkeypatch):
    records = run_polls(
        monkeypatch,
        [
            [device(online=False)],
            [device(online=False)],
            [device(online=True)],
            [device(online=False)],
        ],
    )

    assert [record.get("_delete", False) for record in records] == [
        True,
        False,
        True,
    ]
    assert {record["sensor_id"] for record in records} == {"DRONU-device-0"}


def test_disappeared_online_device_gets_last_position_tombstone(monkeypatch):
    records = run_polls(monkeypatch, [[device(online=True)], []])

    assert len(records) == 2
    assert records[0]["is_online"] is True
    assert records[1]["is_online"] is False
    assert records[1]["_delete"] is True
    assert records[1]["lat_deg"] == records[0]["lat_deg"]
    assert records[1]["lon_deg"] == records[0]["lon_deg"]
