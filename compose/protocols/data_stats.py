"""Process-local byte/frame counters, periodically snapshotted to disk.

Not a metrics platform. EFDI never measured how much wire data it has
received/translated/republished — logs are print statements sized for
debugging, not counters — so this exists purely to answer "how much data has
this pod moved" without re-deriving it from log file sizes (which is not the
same thing and was never accurate for that).

Each OS process (one per ASTERIX category, one per bridge, etc.) keeps its
own in-memory counters and flushes them to its own file under
compose/state/stats/<key>.json, so concurrent processes never clobber each
other's snapshot. Summing those files gives the pod total.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.environ.get(
    "EFDI_STATS_DIR",
    os.path.join(os.path.dirname(_HERE), "state", "stats"),
)

_FLUSH_INTERVAL_S = 30.0

_lock = threading.Lock()
_counters: dict[str, dict] = {}
_last_flush: dict[str, float] = {}


def record_in(key: str, nbytes: int) -> None:
    """Count `nbytes` received (pre-decode wire bytes) under stats key `key`."""
    _bump(key, "bytes_in", "frames_in", nbytes)


def record_out(key: str, nbytes: int) -> None:
    """Count `nbytes` published (post-translation payload) under stats key `key`."""
    _bump(key, "bytes_out", "frames_out", nbytes)


def _bump(key: str, byte_field: str, frame_field: str, nbytes: int) -> None:
    now = time.time()
    with _lock:
        c = _counters.setdefault(key, {
            "bytes_in": 0, "bytes_out": 0,
            "frames_in": 0, "frames_out": 0,
            "since": now,
        })
        c[byte_field] += max(0, int(nbytes))
        c[frame_field] += 1
        snapshot = dict(c)
        due = now - _last_flush.get(key, 0.0) >= _FLUSH_INTERVAL_S
        if due:
            _last_flush[key] = now
    if due:
        _flush(key, snapshot)


def _flush(key: str, snapshot: dict) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        path = os.path.join(_STATE_DIR, "{}.json".format(key))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _flush_all_on_exit() -> None:
    with _lock:
        items = {k: dict(v) for k, v in _counters.items()}
    for key, snapshot in items.items():
        _flush(key, snapshot)


atexit.register(_flush_all_on_exit)
