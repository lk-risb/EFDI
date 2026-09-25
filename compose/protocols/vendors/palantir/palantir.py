#!/usr/bin/env python3
"""Palantir's topics on the EFDI Backbone trial fabric -> EFDI tracks.

Two of Palantir's ten registered topics carry map-relevant data (the rest —
demo/v1 Ping, robocar/v1 and TESTRoboCar/cmd/* — are heartbeat/drive-command
channels with no location field at all, and robocar/camera/v1/frame's
CameraFrame schema has no lat/lon either, just camera_id/sequence/jpeg bytes
for a mobile car with no known fixed site — nothing to place on a map for any
of those):

  <slot>/adsb/protobuf/v1  (protobuf:AdsbBatch) — real ADS-B tracks (per the
    portal's own topic description, "synthetic until readsb is fixed", i.e.
    simulated until their real receiver is wired up). Schema vendored at
    compose/schemas/vendors/palantir/palantir_adsb.proto. Straightforward
    flat decode.

  <slot>/raw/gaia/tak/test/cot  (xml, no schema needed) — CoT XML, explicitly
    named as test data by its own topic path ("tak/test/cot"). Rather than
    write a second CoT parser, this reuses bridges/tak_bridge.py's existing
    inbound CoT decoder (_parse_event_xml/_normalize_event) — that file
    already does exactly this for EFDI's real TAK Server ingress, and CoT is
    CoT regardless of transport. Only the topic naming and _src tag are
    overridden below, so this clearly reads as backbone-sourced test data,
    not real TAK Server traffic.
"""

from __future__ import annotations

import json
import time

import bridges.tak_bridge as tak_bridge
from google.protobuf.message import DecodeError
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe
from schemas.vendors.palantir.palantir_adsb_pb2 import AdsbBatch

_ADSB_SUFFIX = "/adsb/protobuf/v1"
_COT_SUFFIX = "/raw/gaia/tak/test/cot"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"

_FT_TO_M = 0.3048
_KT_TO_MPS = 0.514444


def adsb_records(payload: bytes) -> list[dict]:
    batch = AdsbBatch()
    batch.ParseFromString(payload)
    out = []
    for track in batch.tracks:
        if track.lat == 0 and track.lon == 0:
            continue
        record = {
            "_ts": time.time(),
            "_src": "backbone:palantir:adsb:{}".format(track.station_id or batch.station_id or "unknown"),
            "uid": track.icao24 or "adsb-{}".format(track.squawk),
            "lat_deg": track.lat,
            "lon_deg": track.lon,
            "label": track.callsign.strip() or track.icao24,
            "affiliation": "unknown",
        }
        if track.alt_baro_ft:
            record["alt_m"] = track.alt_baro_ft * _FT_TO_M
        if track.ground_speed_kt:
            record["speed_mps"] = track.ground_speed_kt * _KT_TO_MPS
        if track.track_deg:
            record["heading_deg"] = track.track_deg
        if track.squawk:
            record["squawk"] = track.squawk
        out.append(record)
    return out


def cot_record(xml_bytes: bytes) -> tuple[str, dict] | None:
    try:
        xml = xml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    event = tak_bridge._parse_event_xml(xml)
    if event is None:
        return None
    normalized = tak_bridge._normalize_event(event)
    if normalized is None:
        return None
    _tak_topic, record = normalized
    domain = record.get("cot_domain", "land")
    affiliation = record.get("cot_affiliation", "unknown")
    record["_src"] = "backbone:palantir:cot-test"
    record["cot_test"] = True
    # _normalize_event() tags every record "_ingress": "tak_server" — that's
    # tak_layer.py's own echo guard (line ~1655: drop_tak_ingress) stopping a
    # track that arrived FROM the real TAK server connection from being sent
    # straight back into it. This record did not come from that connection;
    # leaving the tag on would make tak_layer silently drop it before it ever
    # reached the map — the opposite of the point of decoding it.
    record.pop("_ingress", None)
    topic = "{}/{}/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, domain, affiliation)
    return topic, record


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("palantir: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        payload = payload_bytes(sample)
        try:
            if key.endswith(_ADSB_SUFFIX):
                for record in adsb_records(payload):
                    topic = "{}/air/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, record.pop("affiliation"))
                    session.put(topic, json.dumps(record).encode())
            elif key.endswith(_COT_SUFFIX):
                result = cot_record(payload)
                if result:
                    topic, record = result
                    session.put(topic, json.dumps(record).encode())
        except DecodeError as exc:
            print("palantir decode error on {}: {}".format(key, exc), flush=True)
        except Exception as exc:
            print("palantir error on {}: {}".format(key, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("palantir: {} -> tracks (adsb/protobuf/v1, raw/gaia/tak/test/cot)".format(INPUT_TOPIC), flush=True)
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
