"""Recognise our own sensors when a partner sends them back.

A participant on the backbone fabric can ingest what this pod exports and publish it
again under its own ids. The soc-bx feed uses object ids such as
"SENSOR:MAINLINE-DRONU-0478D5C6" for our sensor MAINLINE-DRONU-0478D5C6; a generic JSON
feed uses a UUID but keeps our display name ("radar-30684") as the callsign. Decoded as
normal tracks these get a uid unlike the original, so every such sensor shows twice.
EchoFilter remembers the sensor ids and display names this pod publishes itself, so a
decoder can drop those copies.
"""

import json
import threading
import time

# Shorter ids could appear inside an unrelated uid by accident.
MIN_ID_LEN = 12
# Shorter names are too generic to treat as ours, and the bridge's own fallback name
# ("dronu-sensor") says nothing about which sensor it is.
MIN_NAME_LEN = 8
_GENERIC_NAMES = {"dronu-sensor", "sensor", "unknown"}


class EchoFilter:
    def __init__(self, ttl_s: float = 1800.0):
        self._ttl = ttl_s
        self._ids: dict = {}
        self._names: dict = {}
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
        now = time.time()
        name = str(data.get("sensor_name") or "").strip().lower()
        with self._lock:
            self._ids[str(data["sensor_id"]).upper()] = now
            if len(name) >= MIN_NAME_LEN and name not in _GENERIC_NAMES:
                self._names[name] = now

    def is_echo(self, record: dict, now: float | None = None) -> bool:
        """True when the record's uid embeds one of our sensor ids, or its callsign or
        label is one of our sensor names."""
        uid = str(record.get("uid") or "").upper()
        labels = {str(record.get(k) or "").strip().lower() for k in ("callsign", "label")} - {""}
        now = time.time() if now is None else now
        with self._lock:
            ids = [i for i, seen in self._ids.items() if now - seen <= self._ttl and len(i) >= MIN_ID_LEN]
            names = {n for n, seen in self._names.items() if now - seen <= self._ttl}
        return any(i in uid for i in ids) or bool(labels & names)
