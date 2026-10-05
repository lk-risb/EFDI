"""Recognise our own sensors when a partner sends them back.

A participant on the backbone fabric can ingest what this pod exports and publish it
again under its own object ids (for example "SENSOR:MAINLINE-DRONU-0478D5C6" for our
sensor MAINLINE-DRONU-0478D5C6). Decoded as a normal track it gets a different uid from
the original, so every such sensor shows twice. EchoFilter remembers the sensor ids this
pod publishes itself, so the decoder can drop those copies.
"""

import json
import threading
import time

# Shorter ids could appear inside an unrelated uid by accident.
MIN_ID_LEN = 12


class EchoFilter:
    def __init__(self, ttl_s: float = 1800.0):
        self._ttl = ttl_s
        self._seen: dict = {}
        self._lock = threading.Lock()

    def on_sample(self, sample) -> None:
        """Zenoh callback for this pod's own tracks (any JSON dict carrying a sensor_id).
        Records that came in from the backbone are not ours and are ignored."""
        try:
            data = json.loads(bytes(sample.payload).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict) or not data.get("sensor_id"):
            return
        if str(data.get("_src") or "").startswith("backbone:"):
            return
        with self._lock:
            self._seen[str(data["sensor_id"]).upper()] = time.time()

    def is_echo(self, record: dict, now: float | None = None) -> bool:
        """True when the record's uid embeds one of our own sensor ids."""
        uid = str(record.get("uid") or "").upper()
        now = time.time() if now is None else now
        with self._lock:
            ids = [i for i, seen in self._seen.items() if now - seen <= self._ttl and len(i) >= MIN_ID_LEN]
        return any(i in uid for i in ids)
