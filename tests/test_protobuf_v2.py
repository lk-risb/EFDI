import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))

from protocols.adsblol_track_pb2 import AdsbLolTrack  # noqa: E402
from protocols.normalized_track_pb2 import NormalizedTrack  # noqa: E402
from protocols.protobuf_codec import (  # noqa: E402
    normalized_track_message,
    source_message_to_track,
    source_track_to_message,
)


def test_source_track_round_trip_preserves_optional_zero_and_false():
    source = {
        "_ts": 123.5,
        "_src": "adsblol",
        "icao24": "abc123",
        "lat_deg": 54.1,
        "lon_deg": 25.2,
        "alt_baro_ft": 0,
        "on_ground": False,
    }
    encoded = source_track_to_message(AdsbLolTrack, source).SerializeToString()
    parsed = AdsbLolTrack.FromString(encoded)
    restored = source_message_to_track(parsed)

    assert restored["_src"] == "adsblol"
    assert restored["alt_baro_ft"] == 0
    assert restored["on_ground"] is False


def test_normalized_v2_uses_metric_units_and_bounded_metadata():
    message = normalized_track_message(NormalizedTrack, {
        "_ts": 10,
        "_src": "adsblol",
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
