#!/usr/bin/env python3
"""Vendor-neutral RF/spectrum observation JSON on Zenoh -> C2 observations."""

from __future__ import annotations

import argparse
import json
import math
import os
import time

import zenoh

from namespace_prefix import prefix
from protocols.protobuf_codec import publish_dual
from protocols.random.spectrum_observation_pb2 import SpectrumObservation
from translation_common import TOPIC_ROOT, make_config, payload_json


INPUT_TOPIC = os.environ.get("SPECTRUM_INPUT_TOPIC") or TOPIC_ROOT + "/raw/spectrum/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/land/spectrum/neutral/sensor/observations/v1"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def normalize(payload: dict, now: float | None = None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    now = time.time() if now is None else float(now)
    uid = str(payload.get("uid") or payload.get("emitter_id") or "spectrum-" + str(int(now * 1000)))
    record = {"_ts": now, "_src": "spectrum", "uid": uid[:160],
              "source_kind": "spectrum_observation", "sensor_type": "passive_rf"}
    aliases = {
        "frequency_hz": ("frequency_hz", "frequency", "freq_hz"),
        "bandwidth_hz": ("bandwidth_hz", "bandwidth", "bw_hz"),
        "power_dbm": ("power_dbm", "power", "rssi_dbm"),
        "bearing_deg": ("bearing_deg", "bearing", "azimuth_deg"),
        "confidence": ("confidence", "probability"),
        "lat_deg": ("lat_deg", "latitude", "lat"),
        "lon_deg": ("lon_deg", "longitude", "lon"),
    }
    for target, names in aliases.items():
        for name in names:
            value = _number(payload.get(name))
            if value is not None:
                record[target] = value
                break
    if "lat_deg" not in record or "lon_deg" not in record:
        # A bearing-only observation is still useful as metadata, but it must
        # not be rendered as a map marker without a sensor location.
        if "bearing_deg" not in record:
            return None
    for target, names in {
        "sensor_id": ("sensor_id", "sensor", "node_id"),
        "emitter_id": ("emitter_id", "transmitter_id", "signal_id"),
        "classification": ("classification", "label", "signal_type"),
    }.items():
        for name in names:
            value = payload.get(name)
            if isinstance(value, (str, int, float)):
                record[target] = str(value)[:256]
                break
    return record


def run() -> None:
    session = zenoh.open(make_config())

    def on_sample(sample) -> None:
        try:
            value = payload_json(sample)
            values = value if isinstance(value, list) else [value]
            for item in values:
                record = normalize(item)
                if record:
                    publish_dual(session, OUTPUT_TOPIC, record, SpectrumObservation, zenoh)
        except Exception as exc:
            print("spectrum decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("Spectrum translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="Spectrum JSON on Zenoh -> EFDI").parse_args()
    run()
