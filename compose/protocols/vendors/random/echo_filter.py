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
those copies. A copy that carries none of our ids or names (a UUID with the tail of the UUID as
its callsign, seen live on top of MAINLINE-DRONU-595EFFB1) is still recognised by position: it sits
within a few metres of one of our fixed sensors.
"""

import json
import os
import re
import threading
import time

# Shorter ids could appear inside an unrelated uid by accident.
MIN_ID_LEN = 12
# Shorter names are too generic to treat as ours, and the bridge's own fallback name
# ("dronu-sensor") says nothing about which sensor it is.
MIN_NAME_LEN = 8
# A fixed sensor of ours and a partner's copy of it share coordinates, so a partner object within
# POSITION_CELL_DEG (about 11 m, checked with its neighbouring cells) of one counts as its copy.
POSITION_CELL_DEG = 1e-4
_GENERIC_NAMES = {"dronu-sensor", "sensor", "unknown"}
# Operators can drop a partner's test or junk objects without a code change: a regex matched
# against uid, callsign and label of each backbone record (restart socbx / generic_json after setting it).
_DROP = re.compile(os.environ["BACKBONE_DROP_REGEX"], re.I) if os.environ.get("BACKBONE_DROP_REGEX") else None


# Topics whose tracks this pod publishes and a partner could send back: every land track, and the
# drones, missiles and bombs from the open-data bridges (they have no ICAO address to merge on).
OWN_TOPICS = ("land/**", "air/*/hostile/**")


def subscribe_own(session, topic_root: str, echoes: "EchoFilter", subscribe) -> list:
    """Declare the subscriptions that feed `echoes`; returns them so the caller can undeclare."""
    return [subscribe(session, "{}/{}".format(topic_root, pattern), echoes.on_sample) for pattern in OWN_TOPICS]


class EchoFilter:
    def __init__(self, ttl_s: float = 1800.0):
        self._ttl = ttl_s
        self._ids: dict = {}
        self._names: dict = {}
        self._cells: dict = {}
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
            if data.get("sensor_id") and data.get("lat_deg") is not None and data.get("lon_deg") is not None:
                self._cells[self._cell(data["lat_deg"], data["lon_deg"])] = now
            for identifier in ids:
                self._ids[identifier] = now
            for name in names:
                if len(name) >= MIN_NAME_LEN and name not in _GENERIC_NAMES:
                    self._names[name] = now

    @staticmethod
    def _cell(lat, lon) -> tuple:
        return (int(float(lat) / POSITION_CELL_DEG), int(float(lon) / POSITION_CELL_DEG))

    def _at_a_sensor(self, record: dict, now: float) -> bool:
        if record.get("icao24") or record.get("lat_deg") is None or record.get("lon_deg") is None:
            return False
        try:
            row, col = self._cell(record["lat_deg"], record["lon_deg"])
        except (TypeError, ValueError):
            return False
        with self._lock:
            return any(now - self._cells.get((row + dr, col + dc), float("-inf")) <= self._ttl
                       for dr in (-1, 0, 1) for dc in (-1, 0, 1))

    def is_echo(self, record: dict, now: float | None = None) -> bool:
        """True when the record's uid, callsign or label embeds one of our ids (a partner copy often
        uses our uid as its callsign), or its callsign or label is one of our names."""
        if _DROP is not None and any(_DROP.search(str(record.get(k) or "")) for k in ("uid", "callsign", "label")):
            return True
        uid = str(record.get("uid") or "").upper()
        labels = {str(record.get(k) or "").strip().lower() for k in ("callsign", "label")} - {""}
        now = time.time() if now is None else now
        with self._lock:
            ids = [i for i, seen in self._ids.items() if now - seen <= self._ttl and len(i) >= MIN_ID_LEN]
            names = {n for n, seen in self._names.items() if now - seen <= self._ttl}
        texts = {uid} | {label.upper() for label in labels}
        return any(i in text for i in ids for text in texts) or bool(labels & names) or self._at_a_sensor(record, now)
