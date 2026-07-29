#!/usr/bin/env python3
"""Relay raw ASTERIX topics from an upstream Zenoh router to this pod.

The radar-side publisher owns UDP framing and publishes complete ASTERIX data
blocks on ``<root>/raw/asterix/catN``. This bridge subscribes to every category
at the upstream router, validates the ASTERIX header, and republishes the exact
bytes to the equivalent local topic. Category-specific ``cat.py`` processes do
the decoding after the frame has entered the local router.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
from collections import OrderedDict

import zenoh

from namespace_prefix import topic_root
from zenoh_auth import apply_zenoh_auth


UPSTREAM_ENDPOINT = os.environ.get(
    "ASTERIX_ZENOH_UPSTREAM_ENDPOINT", ""
).strip()
UPSTREAM_ROOT = os.environ.get(
    "ASTERIX_ZENOH_UPSTREAM_ROOT", topic_root()
).strip().strip("/")
LOCAL_ENDPOINT = os.environ.get(
    "ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448"
).strip()
LOCAL_ROOT = topic_root().strip("/")

_RECENT_TTL_S = 1.0
_RECENT_LIMIT = 4096


def category_from_key(key: str, root: str) -> int:
    prefix = root.strip("/") + "/raw/asterix/cat"
    if not key.startswith(prefix):
        raise ValueError("key is outside the configured ASTERIX raw root")
    suffix = key[len(prefix):]
    if not suffix.isdigit():
        raise ValueError("ASTERIX topic category is not numeric")
    category = int(suffix)
    if not 0 <= category <= 255:
        raise ValueError("ASTERIX topic category is outside 0..255")
    return category


def validate_frame(frame: bytes, expected_category: int) -> None:
    if len(frame) < 3:
        raise ValueError("ASTERIX frame is shorter than its header")
    category, declared_length = struct.unpack(">BH", frame[:3])
    if category != expected_category:
        raise ValueError(
            "topic CAT-{} contains CAT-{}".format(expected_category, category)
        )
    if declared_length != len(frame):
        raise ValueError(
            "ASTERIX length {} does not match {} received bytes".format(
                declared_length, len(frame)
            )
        )


class RecentFrames:
    """Bounded short-lived fingerprint cache used to stop router reflections."""

    def __init__(
        self,
        ttl_s: float = _RECENT_TTL_S,
        limit: int = _RECENT_LIMIT,
    ) -> None:
        self.ttl_s = ttl_s
        self.limit = limit
        self._items: OrderedDict[bytes, float] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, key: str, frame: bytes, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else now
        fingerprint = hashlib.blake2s(
            key.encode("utf-8") + b"\0" + frame,
            digest_size=16,
        ).digest()
        with self._lock:
            cutoff = timestamp - self.ttl_s
            while self._items:
                _, oldest = next(iter(self._items.items()))
                if oldest >= cutoff:
                    break
                self._items.popitem(last=False)
            if fingerprint in self._items:
                self._items.move_to_end(fingerprint)
                self._items[fingerprint] = timestamp
                return True
            self._items[fingerprint] = timestamp
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
        return False


def make_config(endpoint: str, *, local: bool) -> "zenoh.Config":
    config = zenoh.Config()
    config.insert_json5("mode", '"client"')
    config.insert_json5("connect/endpoints", json.dumps([endpoint]))
    if local:
        apply_zenoh_auth(config)
    return config


def relay_sample(
    local_session,
    sample,
    *,
    upstream_root: str = UPSTREAM_ROOT,
    local_root: str = LOCAL_ROOT,
    recent: RecentFrames,
) -> str | None:
    source_key = str(sample.key_expr)
    category = category_from_key(source_key, upstream_root)
    frame = bytes(sample.payload)
    validate_frame(frame, category)
    destination = "{}/raw/asterix/cat{}".format(local_root.strip("/"), category)
    if recent.seen(destination, frame):
        return None
    local_session.put(
        destination,
        frame,
        encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM,
    )
    return destination


def run() -> None:
    if not UPSTREAM_ENDPOINT:
        raise SystemExit(
            "ASTERIX_ZENOH_UPSTREAM_ENDPOINT is not set "
            "(for the current plaintext test, use tcp/<router-host>:7448)"
        )
    if not UPSTREAM_ROOT or not LOCAL_ROOT:
        raise SystemExit("ASTERIX upstream and local topic roots must not be empty")

    local_session = zenoh.open(make_config(LOCAL_ENDPOINT, local=True))
    upstream_session = zenoh.open(
        make_config(UPSTREAM_ENDPOINT, local=False)
    )
    recent = RecentFrames()
    selector = UPSTREAM_ROOT + "/raw/asterix/*"
    relayed = 0
    rejected = 0
    counter_lock = threading.Lock()

    def on_sample(sample) -> None:
        nonlocal relayed, rejected
        try:
            destination = relay_sample(
                local_session,
                sample,
                recent=recent,
            )
        except (TypeError, ValueError) as exc:
            with counter_lock:
                rejected += 1
            print(
                "ASTERIX upstream frame rejected on {}: {}".format(
                    sample.key_expr, exc
                ),
                flush=True,
            )
            return
        if destination is None:
            return
        with counter_lock:
            relayed += 1
            count = relayed
        if count == 1 or count % 1000 == 0:
            print(
                "ASTERIX upstream relay: {} frame(s), latest {}".format(
                    count, destination
                ),
                flush=True,
            )

    subscriber = upstream_session.declare_subscriber(selector, on_sample)
    print(
        "ASTERIX Zenoh ingress {} {} -> {} {}/raw/asterix/catN".format(
            UPSTREAM_ENDPOINT,
            selector,
            LOCAL_ENDPOINT,
            LOCAL_ROOT,
        ),
        flush=True,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        upstream_session.close()
        local_session.close()
        print(
            "ASTERIX Zenoh ingress stopped: {} relayed, {} rejected".format(
                relayed, rejected
            ),
            flush=True,
        )


if __name__ == "__main__":
    run()
