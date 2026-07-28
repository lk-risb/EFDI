import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))

from protocols.random.normalized_track_pb2 import NormalizedTrack  # noqa: E402
from protocols.protobuf_codec import (  # noqa: E402
    normalized_track_message,
    source_message_to_track,
    source_track_to_message,
)


def test_source_track_round_trip_preserves_optional_zero_and_false():
    source = {
        "_ts": 123.5,
        "_src": "partner-adsb",
        "uid": "abc123",
        "lat_deg": 54.1,
        "lon_deg": 25.2,
        "baro_alt_m": 0,
        "on_ground": False,
    }
    encoded = source_track_to_message(NormalizedTrack, source).SerializeToString()
    parsed = NormalizedTrack.FromString(encoded)
    restored = source_message_to_track(parsed)

    assert restored["_src"] == "partner-adsb"
    assert restored["baro_alt_m"] == 0
    assert restored["on_ground"] is False


def test_normalized_v2_uses_metric_units_and_bounded_metadata():
    message = normalized_track_message(NormalizedTrack, {
        "_ts": 10,
        "_src": "partner-adsb",
        "icao24": "abc123",
        "lat_deg": 54,
        "lon_deg": 25,
        "alt_baro_ft": 1000,
        "ground_speed_kts": 100,
        "baro_vr_fpm": -500,
    }, "civ")

    assert round(message.baro_alt_m, 1) == 304.8
    assert round(message.speed_ms, 3) == 51.444
    assert round(message.vertical_rate_ms, 2) == -2.54
    assert message.source_metadata["icao24"] == "abc123"
