#!/usr/bin/env python3
"""USAREUR's ADS-B snapshot feed on the EFDI Backbone trial fabric ->
EFDI tracks.

Topic "<slot>/adsb/aircraft/snapshot/v1" (protobuf:AircraftBatch) — real ADS-B
tracks sourced from the airplanes.live v2 REST API and republished as one
batch per poll cycle (per the schema's own field comments). Richer than
Palantir's AdsbBatch: also carries geometric altitude, baro rate, squawk,
category, emergency code, registration, and aircraft type.
"""

from __future__ import annotations

import json
import time

from google.protobuf.message import DecodeError
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe
from schemas.vendors.usareur.adsb_aircraft_pb2 import AircraftBatch

_TOPIC_SUFFIX = "/adsb/aircraft/snapshot/v1"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

_FT_TO_M = 0.3048
_KT_TO_MPS = 0.514444


def aircraft_records(payload: bytes) -> list[dict]:
    batch = AircraftBatch()
    batch.ParseFromString(payload)
    out = []
    for aircraft in batch.aircraft:
        if aircraft.lat == 0 and aircraft.lon == 0:
            continue
        record = {
            "_ts": time.time(),
            "_src": "backbone:usareur:adsb:{}".format(aircraft.source or "unknown"),
            "uid": aircraft.icao_hex or "usareur-adsb-{}".format(aircraft.squawk),
            "lat_deg": aircraft.lat,
            "lon_deg": aircraft.lon,
            "label": aircraft.callsign.strip() or aircraft.registration or aircraft.icao_hex,
            "affiliation": "unknown",
        }
        if aircraft.alt_baro_ft:
            record["alt_m"] = aircraft.alt_baro_ft * _FT_TO_M
        elif aircraft.alt_geom_ft:
            record["alt_m"] = aircraft.alt_geom_ft * _FT_TO_M
        if aircraft.ground_kt:
            record["speed_mps"] = aircraft.ground_kt * _KT_TO_MPS
        if aircraft.track_deg:
            record["heading_deg"] = aircraft.track_deg
        if aircraft.squawk:
            record["squawk"] = aircraft.squawk
        if aircraft.category:
            record["category"] = aircraft.category
        if aircraft.emergency and aircraft.emergency.lower() != "none":
            record["emergency"] = aircraft.emergency
        if aircraft.registration:
            record["registration"] = aircraft.registration
        if aircraft.ac_type:
            record["ac_type"] = aircraft.ac_type
        out.append(record)
    return out


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("usareur: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        if not key.endswith(_TOPIC_SUFFIX):
            return
        try:
            for record in aircraft_records(payload_bytes(sample)):
                topic = "{}/air/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, record.pop("affiliation"))
                session.put(topic, json.dumps(record).encode())
        except DecodeError as exc:
            print("usareur decode error on {}: {}".format(key, exc), flush=True)
        except Exception as exc:
            print("usareur error on {}: {}".format(key, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("usareur: {} -> tracks (adsb/aircraft/snapshot/v1)".format(INPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


if __name__ == "__main__":
    run()
