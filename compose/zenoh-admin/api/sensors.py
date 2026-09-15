"""Live in-memory cache of dronuradaras.lt acoustic sensors, fed by a
background Zenoh subscriber held for the app's lifespan — same shape as
topics.py's start_topic_observer(), except this one deliberately DOES read
payload content. topics.py's own observer explicitly avoids that (it only
tracks key/encoding/sample-count metadata); this is a separate, narrowly
scoped subscriber (one topic family, not "**") that exists specifically to
surface each sensor's last-known position and detection state — including
last_detection_audio_url, which nothing else in this API exposes today —
to the WebUI.
"""

import json
import threading

from fastapi import APIRouter, Depends

from .deps import require_role
from .local_zenoh import open_local_session

router = APIRouter(prefix="/api/sensors", tags=["sensors"])

_LOCK = threading.Lock()
_SENSORS: dict[str, dict] = {}   # sensor_id -> latest known payload

# Every device's status/alert payload lands on one shared key (see
# dronuradaras_bridge.py's topic_dev) regardless of namespace prefix or
# partner UUID, so a "**" wildcard on both sides matches this pod's actual
# topic without needing to read the prefix files this module has no other
# reason to depend on.
_KEY_EXPR = "**/land/dronuradaras/**"


def _observe(sample) -> None:
    try:
        payload = json.loads(bytes(sample.payload).decode())
    except (ValueError, TypeError, UnicodeDecodeError):
        return
    sensor_id = payload.get("sensor_id")
    if not sensor_id:
        return
    with _LOCK:
        if payload.get("_delete"):
            _SENSORS.pop(sensor_id, None)
            return
        _SENSORS[sensor_id] = payload


def start_dronuradaras_observer():
    try:
        session = open_local_session()
        session.declare_subscriber(_KEY_EXPR, _observe)
        return session
    except Exception as exc:
        print(f"[sensors] dronuradaras observer not started: {exc}", flush=True)
        return None


@router.get("/dronuradaras")
async def list_dronuradaras_sensors(_=Depends(require_role("readonly", "admin", "superadmin"))):
    with _LOCK:
        return {"sensors": list(_SENSORS.values())}
