#!/usr/bin/env python3
"""SkyLord's CoT-XML feeds on the EFDI Backbone trial fabric -> EFDI tracks.

Two known topics on this slot, both wire format "xml:event" against the
standard MITRE/TAK CoT schema (not a custom dialect — confirmed against the
portal's uploaded takcot.xsd, a consolidated copy of the same public schema
bridges/tak_bridge.py already parses):

  <slot>/skylord/ads-b/rtl-sdr-01/tracks/cot  — their own RTL-SDR ADS-B
    receiver, republished as CoT.
  <slot>/skylord/anduril/heimdall/tracks/cot  — Anduril Heimdall counter-UAS
    radar tracks, republished as CoT.

Both share one topic suffix ("/tracks/cot"), so one decoder covers both. Real
data (unlike Palantir's "tak/test/cot" topic) — no cot_test tag here.

Reuses bridges/tak_bridge.py's existing inbound CoT decoder instead of
writing a second one, same as protocols/vendors/palantir/palantir.py. Same
_ingress fix applies here too: _normalize_event() tags every record
"_ingress": "tak_server", which is tak_layer.py's echo guard for tracks that
arrived from the real TAK server connection — must be stripped, or
tak_layer.py would silently drop these before they ever reached the map.
"""

from __future__ import annotations

import json
import time

import bridges.tak_bridge as tak_bridge
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

_TOPIC_SUFFIX = "/tracks/cot"
_SUB_FEED_MARKER = "/skylord/"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"


def _sub_feed(key: str) -> str:
    """"<slot>/skylord/ads-b/rtl-sdr-01/tracks/cot" -> "ads-b/rtl-sdr-01" —
    which of SkyLord's feeds this came from, for _src/labeling."""
    marker = key.find(_SUB_FEED_MARKER)
    if marker == -1:
        return "unknown"
    start = marker + len(_SUB_FEED_MARKER)
    end = key.rfind(_TOPIC_SUFFIX)
    return key[start:end] if end > start else "unknown"


def cot_record(xml_bytes: bytes, sub_feed: str) -> tuple[str, dict] | None:
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
    record["_src"] = "backbone:skylord:{}".format(sub_feed)
    # See module docstring — this did not come from the real TAK server
    # connection, so tak_layer.py's echo guard must not see this tag.
    record.pop("_ingress", None)
    topic = "{}/{}/backbone/{}/unit/tracks/v1".format(TOPIC_ROOT, domain, affiliation)
    return topic, record


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("skylord: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        if not key.endswith(_TOPIC_SUFFIX):
            return
        try:
            result = cot_record(payload_bytes(sample), _sub_feed(key))
            if result:
                topic, record = result
                session.put(topic, json.dumps(record).encode())
        except Exception as exc:
            print("skylord decode error on {}: {}".format(key, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("skylord: {} -> tracks (*/tracks/cot)".format(INPUT_TOPIC), flush=True)
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
