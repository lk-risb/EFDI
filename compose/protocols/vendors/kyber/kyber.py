#!/usr/bin/env python3
"""Kyber's SAPIENT feed on the EFDI Backbone trial fabric -> EFDI tracks.

Topic "<slot>/raw/kyber/kyber/default/edge/ungrouped/<uuid>/report" (one
sub-topic per device) — protobuf:SapientMessage, the exact same BSI Flex
335 v2.0 wire format EFDI's own protocols/vendors/sapient/flex335.py already
decodes for its direct TCP-fed SAPIENT integration. Rather than write a
second SAPIENT parser, this reuses that file's SapientDecoder and
topic_for_track directly, and publishes through the same publish_dual/
publish_native calls flex335.py's own _publish() uses — so a Kyber track
lands on exactly the same topic shape (…/sapient/…) that tak_layer.py/
sitaware_layer.py already subscribe to for EFDI's own live SAPIENT feed.

The schema was uploaded "by operator" rather than by Kyber itself — likely
the portal's standard SAPIENT template offered to any participant using
that format, not something Kyber wrote themselves. Doesn't change the wire
format: SAPIENT is SAPIENT regardless of who registered the schema.

Unlike flex335.py's own run_zenoh_raw() (built for EFDI's own SAPIENT-over-
Zenoh path via flex335_bridge.py), this does NOT reassemble a 4-byte
length-prefixed byte stream — that framing is flex335_bridge.py's own
TCP-to-Zenoh convention. Each sample on the backbone's raw topic is already
one complete SapientMessage, exactly like every other backbone decoder in
this codebase (socbx.py, palantir.py, tytan.py, ...) — decode directly, no
buffering.
"""

from __future__ import annotations

import time

from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, publish_dual, publish_native, subscribe
from protocols.proto.flex335_pb2 import SapientFlex335Track
from protocols.track_views import native_topic, semantic_topic
from protocols.vendors.sapient.flex335 import SapientDecoder, topic_for_track

_TOPIC_MARKER = "/kyber/"
_TOPIC_SUFFIX = "/report"
INPUT_TOPIC = TOPIC_ROOT + "/raw/backbone/**"


def run() -> None:
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("kyber: Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    decoder = SapientDecoder()

    def on_sample(sample) -> None:
        key = str(sample.key_expr)
        if _TOPIC_MARKER not in key or not key.endswith(_TOPIC_SUFFIX):
            return
        frame = payload_bytes(sample)
        try:
            event = decoder.decode(frame)
        except ValueError as exc:
            print("kyber decode error on {}: {}".format(key, exc), flush=True)
            return
        except Exception as exc:
            print("kyber error on {}: {}".format(key, exc), flush=True)
            return
        if event.warning or event.track is None:
            return
        topic = topic_for_track(TOPIC_ROOT, event.track)
        publish_dual(session, topic, event.track, SapientFlex335Track)
        try:
            publish_native(session, native_topic(semantic_topic(topic, event.track)),
                            frame, "sapient",
                            profile="bsi-flex-335-v2", content_type="application/protobuf")
        except Exception as exc:  # noqa: BLE001 — best-effort, dual view already published
            print("kyber native view failed for {}: {}".format(topic, exc), flush=True)

    subscriber = subscribe(session, INPUT_TOPIC, on_sample)
    print("kyber: {} -> tracks (SAPIENT, reusing flex335.py's decoder)".format(INPUT_TOPIC), flush=True)
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
