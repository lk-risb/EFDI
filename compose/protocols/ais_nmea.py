#!/usr/bin/env python3
"""NMEA 0183 AIS (AIVDM/AIVDO) carried by Zenoh -> vessel tracks."""

from __future__ import annotations

import argparse
import json
import os
import re
import time

import zenoh

from namespace_prefix import prefix
from translation_common import TOPIC_ROOT, make_config, payload_bytes, put_json


INPUT_TOPIC = os.environ.get("AIS_NMEA_INPUT_TOPIC") or TOPIC_ROOT + "/raw/ais/**"
OUTPUT_TOPIC = TOPIC_ROOT + "/sea/ais/nmea/civ/vessel/tracks/v1"
_LINE = re.compile(r"^!(?:AIVDM|AIVDO),(\d+),(\d+),([^,]*),([^,]*),([^,]*),(\d*)\*(?:[0-9A-Fa-f]{2})$")


def _sixbit(char: str) -> int:
    value = ord(char) - 48
    return value - 8 if value > 40 else value


def _bits(payload: str) -> str:
    return "".join(format(_sixbit(char), "06b") for char in payload)


def _u(bits: str, start: int, size: int) -> int:
    return int(bits[start:start + size], 2)


def _s(bits: str, start: int, size: int) -> int:
    value = _u(bits, start, size)
    return value - (1 << size) if value & (1 << (size - 1)) else value


def _text(bits: str, start: int, size: int) -> str:
    alphabet = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
    return "".join(alphabet[_u(bits, pos, 6)] for pos in range(start, start + size, 6)).strip(" @")


def parse_line(line: str) -> tuple[dict, str] | None:
    line = line.strip()
    match = _LINE.match(line)
    if not match:
        return None
    total, number, sequence, channel, payload, fill = match.groups()
    return {"total": int(total), "number": int(number), "sequence": sequence,
            "channel": channel, "payload": payload, "fill": int(fill or 0)}, line


def decode_payload(payload: str, fill: int = 0, now: float | None = None) -> dict | None:
    bits = _bits(payload)
    if fill:
        bits = bits[:-fill]
    if len(bits) < 38:
        return None
    message_type = _u(bits, 0, 6)
    mmsi = _u(bits, 8, 30)
    if mmsi <= 0:
        return None
    result = {"_ts": time.time() if now is None else float(now), "_src": "ais_nmea",
              "uid": "AIS-{}".format(mmsi), "mmsi": str(mmsi),
              "ais_message_type": message_type, "source_kind": "ais_nmea"}
    if message_type in (1, 2, 3):
        result["nav_status"] = _u(bits, 38, 4)
        sog = _u(bits, 50, 10)
        cog = _u(bits, 116, 12)
        heading = _u(bits, 128, 9)
        lon = _s(bits, 61, 28) / 600000.0
        lat = _s(bits, 89, 27) / 600000.0
        if -180 <= lon <= 180 and -90 <= lat <= 90 and not (lat == 91 or lon == 181):
            result.update(lat_deg=round(lat, 7), lon_deg=round(lon, 7))
        if sog < 1023:
            result["speed_ms"] = round(sog / 10.0 * 0.514444, 2)
        if cog < 3600:
            result["heading_deg"] = round(cog / 10.0, 1)
        if heading < 511:
            result["ais_true_heading_deg"] = heading
    elif message_type == 18:
        lon = _s(bits, 57, 28) / 600000.0
        lat = _s(bits, 85, 27) / 600000.0
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            result.update(lat_deg=round(lat, 7), lon_deg=round(lon, 7))
        sog = _u(bits, 46, 10)
        cog = _u(bits, 112, 12)
        if sog < 1023:
            result["speed_ms"] = round(sog / 10.0 * 0.514444, 2)
        if cog < 3600:
            result["heading_deg"] = round(cog / 10.0, 1)
    elif message_type == 19:
        lon = _s(bits, 57, 28) / 600000.0
        lat = _s(bits, 85, 27) / 600000.0
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            result.update(lat_deg=round(lat, 7), lon_deg=round(lon, 7))
        result["vessel_name"] = _text(bits, 143, 120)
    elif message_type == 5 and len(bits) >= 424:
        result["callsign"] = _text(bits, 70, 42)
        result["vessel_name"] = _text(bits, 112, 120)
        result["imo"] = str(_u(bits, 40, 30))
    elif message_type == 24:
        part = _u(bits, 38, 2)
        if part == 0 and len(bits) >= 168:
            result["vessel_name"] = _text(bits, 40, 120)
        elif part == 1 and len(bits) >= 168:
            result["callsign"] = _text(bits, 90, 42)
    else:
        return None
    return result


def run() -> None:
    fragments: dict[tuple[str, str], dict[int, str]] = {}
    static: dict[str, dict] = {}
    session = zenoh.open(make_config())
    publisher = session.declare_publisher(OUTPUT_TOPIC)

    def on_sample(sample) -> None:
        try:
            text = payload_bytes(sample).decode("ascii", errors="ignore")
            for line in text.splitlines():
                parsed = parse_line(line)
                if not parsed:
                    continue
                fields, _ = parsed
                total = fields["total"]
                if total == 1:
                    record = decode_payload(fields["payload"], fields["fill"])
                    if record:
                        mmsi = record.get("mmsi")
                        if "lat_deg" not in record and mmsi:
                            static[mmsi] = record
                        elif "lat_deg" in record:
                            record.update({k: v for k, v in static.get(mmsi, {}).items()
                                            if k not in record})
                            put_json(publisher, record)
                    continue
                key = (fields["sequence"] or "_", fields["channel"] or "_")
                state = fragments.setdefault(key, {"total": total, "fill": fields["fill"], "parts": {}})
                state["parts"][fields["number"]] = fields["payload"]
                if len(state["parts"]) == total:
                    payload = "".join(state["parts"][index] for index in range(1, total + 1))
                    record = decode_payload(payload, state["fill"])
                    fragments.pop(key, None)
                    if record:
                        mmsi = record.get("mmsi")
                        if "lat_deg" not in record and mmsi:
                            static[mmsi] = record
                        elif "lat_deg" in record:
                            record.update({k: v for k, v in static.get(mmsi, {}).items()
                                            if k not in record})
                            put_json(publisher, record)
        except Exception as exc:
            print("AIS NMEA decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(INPUT_TOPIC, on_sample)
    print("AIS NMEA translator: {} -> {}".format(INPUT_TOPIC, OUTPUT_TOPIC), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        publisher.undeclare()
        session.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description="AIS NMEA on Zenoh -> vessel tracks").parse_args()
    run()
