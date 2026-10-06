"""Recognise our own tracks (sensors, alert zones, markers) when a partner sends them back.

A participant on the backbone fabric can ingest what this pod exports and publish it
again under its own ids. The soc-bx feed uses object ids such as
"SENSOR:MAINLINE-DRONU-0478D5C6" for our sensor MAINLINE-DRONU-0478D5C6; a generic JSON
feed uses a UUID but keeps our display name ("radar-30684") as the callsign. Decoded as
normal tracks these get a uid unlike the original, so every such sensor shows twice. The
same happens to our alert zones: a partner's copy arrived as a ground marker called
"DA-ZONE-UA-R:..." (our uid as its callsign) or "UA Vyshhorodskyi District YELLOW" (our
callsign) beside the real polygon. EchoFilter remembers the ids (sensor id, uid) and names
(sensor name, callsign) of what this pod publishes under land/**, so a decoder can drop
those copies.
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
        """Zenoh callback for this pod's own tracks under land/**. Records that came in from the
        backbone are not ours, and tombstones carry nothing to remember."""
        try:
            data = json.loads(bytes(sample.payload).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict) or data.get("_delete"):
            return
        if str(data.get("_src") or "").startswith("backbone:"):
            return
        now = time.time()
        ids = {str(data[k]).upper() for k in ("sensor_id", "uid") if data.get(k)}
        names = {str(data[k]).strip().lower() for k in ("sensor_name", "callsign") if data.get(k)}
        with self._lock:
            for identifier in ids:
                self._ids[identifier] = now
            for name in names:
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
