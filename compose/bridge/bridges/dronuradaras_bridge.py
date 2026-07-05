#!/usr/bin/env python3
"""dronuradaras_bridge.py — dronuradaras.lt drone radar network → Zenoh bridge.

Polls radar-api.mainline.inc (the backend for https://dronuradaras.lt) for:
  1. Radar sensor node positions  → published as land/neutral/sensor site markers
  2. Drone detections             → recolor the reporting sensor's OWN marker

There is no separate "drone" marker/icon. A detection doesn't carry a reliable
drone position (the API only reports which acoustic sensor heard it), so
instead of placing a second icon near the sensor, the sensor's own marker
changes color while an alert is active — same icon (a-?-G-E-S), only the
MIL-STD-2525C affiliation letter changes: neutral (green, no alert) → unknown
(yellow, cooling down) → hostile (red, active detection in the last 60 s).
See cot_layer.py's _sensor_alert_cot_type().

Zenoh topic:
  <ORG>/land/dronuradaras/acoustic/neutral/sensor/status/v1   — sensor nodes
  (carries last_detection_ts when a detection has occurred recently)

No API key required — uses the same public CORS origin as the website.
"""

import argparse
import json
import os
import time
import threading
import urllib.error
import urllib.request

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = "LTU/CISB/" + ORG   # organization prefix precedes the pod namespace
HERE   = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

API_BASE          = "https://radar-api.mainline.inc/api/v1/public"
AUDIO_URL         = API_BASE + "/detections/{}/audio"
ORIGIN_HEADER     = "https://dronuradaras.lt"
REFERER_HEADER    = "https://dronuradaras.lt/"

# Shared device state — written by run_devices, read/written by run_detections.
_device_names:     dict[str, str]   = {}   # device_id → display_name
_device_positions: dict[str, tuple] = {}   # device_id → (lat, lon)
_last_detection:   dict[str, float] = {}   # device_id → epoch of most recent detection
_device_lock  = threading.Lock()

DEVICE_POLL_S     = 60    # radar nodes move rarely
DETECT_POLL_S     = 10    # drone detections — low latency; also the recolor-decay tick
DETECT_WINDOW_S   = 300   # ignore detections older than 5 minutes
ALERT_HOT_S       = 60    # marker shows hostile (red) while a detection is this fresh
ALERT_WARM_S      = DETECT_WINDOW_S  # marker shows unknown (yellow) until this old, then reverts to neutral


# ---------------------------------------------------------------------------
# Zenoh
# ---------------------------------------------------------------------------

def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_HEADERS = {
    "Origin":  ORIGIN_HEADER,
    "Referer": REFERER_HEADER,
    "User-Agent": "EFDI-Bridge/1.0",
    "Accept": "application/json",
}


def _get(path: str) -> dict | None:
    """GET {API_BASE}/{path}, return parsed JSON or None on error."""
    url = "{}/{}".format(API_BASE, path)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print("dronuradaras fetch error [{}]: {}".format(path, exc), flush=True)
        return None


# ---------------------------------------------------------------------------
# Device (sensor node) publisher
# ---------------------------------------------------------------------------

def run_devices(pub: "zenoh.Publisher", verbose: bool):
    topic_suffix = "land/dronuradaras/acoustic/neutral/sensor/status/v1"
    print("Device poll topic: {}/{}".format(TOPIC_ROOT, topic_suffix), flush=True)

    while True:
        data = _get("devices")
        if data:
            devices = data.get("devices") or []
            # Update shared name cache for all devices (online or not)
            with _device_lock:
                for dev in devices:
                    if dev.get("id") and dev.get("display_name"):
                        _device_names[dev["id"]] = dev["display_name"]

            # Publish every device with a known position, not just currently-online
            # ones — "is_online" is a narrow live flag (most registered sensors read
            # false at any given moment even when recently active), so gating on it
            # dropped the vast majority of real sensors the public site still shows.
            # is_online stays in the payload for status display.
            with_position = [d for d in devices if d.get("latitude") is not None and d.get("longitude") is not None]
            for dev in with_position:
                lat = dev["latitude"]
                lon = dev["longitude"]

                dev_id = dev["id"]
                with _device_lock:
                    _device_positions[dev_id] = (lat, lon)
                    last_det = _last_detection.get(dev_id)

                payload = {
                    "_src":        "dronuradaras.lt",
                    "_ts":         time.time(),
                    "sensor_type": "acoustic",
                    "sensor_id":   "DRONU-{}".format(dev_id[:8]),
                    "sensor_name": dev.get("display_name", "dronu-sensor"),
                    "lat_deg":     round(lat, 6),
                    "lon_deg":     round(lon, 6),
                    "is_online":   dev.get("is_online", False),
                    "last_seen":   dev.get("last_seen_at", ""),
                }
                if last_det is not None:
                    payload["last_detection_ts"] = last_det

                pub.put(json.dumps(payload).encode(),
                        encoding=zenoh.Encoding.APPLICATION_JSON)
                if verbose:
                    print("DEV", payload["sensor_name"],
                          "online={}".format(payload["is_online"]),
                          "{:.4f},{:.4f}".format(lat, lon), flush=True)

            online_count = sum(1 for d in with_position if d.get("is_online"))
            print("Devices: {} published ({} online, {} total registered)".format(
                len(with_position), online_count, len(devices)), flush=True)

        time.sleep(DEVICE_POLL_S)


# ---------------------------------------------------------------------------
# Detection publisher
# ---------------------------------------------------------------------------

def _publish_sensor_alert(pub_dev: "zenoh.Publisher", dev_id: str, last_detection_ts: float,
                           audio_url: str | None = None):
    """Republish the reporting sensor's OWN status marker with a fresh
    last_detection_ts — this is what recolors it in cot_layer.py, instead of
    spawning a separate nearby drone icon."""
    with _device_lock:
        pos  = _device_positions.get(dev_id)
        name = _device_names.get(dev_id, dev_id[:8] if dev_id else "unknown")
    if pos is None:
        return  # haven't seen this device's position yet — next device poll will catch up
    lat, lon = pos
    payload = {
        "_src":              "dronuradaras.lt",
        "_ts":                time.time(),
        "sensor_type":        "acoustic",
        "sensor_id":          "DRONU-{}".format(dev_id[:8]),
        "sensor_name":        name,
        "lat_deg":            round(lat, 6),
        "lon_deg":            round(lon, 6),
        "is_online":          True,
        "last_detection_ts":  last_detection_ts,
    }
    if audio_url:
        payload["last_detection_audio_url"] = audio_url
    pub_dev.put(json.dumps(payload).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)


def run_detections(pub_dev: "zenoh.Publisher", verbose: bool):
    print("Detection poll — recolors sensor markers on {}/land/dronuradaras/acoustic/neutral/sensor/status/v1"
          .format(TOPIC_ROOT), flush=True)

    seen: dict[str, float] = {}   # detection_id → published_at timestamp

    while True:
        now = time.time()
        touched_devices: set[str] = set()

        # Evict stale entries from seen cache
        cutoff = now - DETECT_WINDOW_S
        seen = {k: v for k, v in seen.items() if v > cutoff}

        data = _get("detections")
        if data:
            detections = data.get("detections") or []
            new_count = 0

            for det in detections:
                det_id = det.get("id")
                if not det_id or det_id in seen:
                    continue

                # Filter by age — ignore detections older than DETECT_WINDOW_S
                detected_ms = det.get("detected_at", 0)
                detected_s  = detected_ms / 1000.0
                if (now - detected_s) > DETECT_WINDOW_S:
                    continue

                dev_id = det.get("device_id", "")
                if not dev_id:
                    continue

                has_audio = bool(det.get("audio_available"))
                audio_url = AUDIO_URL.format(det_id) if has_audio else None

                with _device_lock:
                    _last_detection[dev_id] = now
                _publish_sensor_alert(pub_dev, dev_id, now, audio_url)
                touched_devices.add(dev_id)

                seen[det_id] = now
                new_count += 1

                if verbose:
                    with _device_lock:
                        dev_name = _device_names.get(dev_id, dev_id[:8])
                    print("DET", det_id[:8], "sensor={}".format(dev_name),
                          "audio={}".format(has_audio), flush=True)

            if new_count:
                print("Detections: {} new".format(new_count), flush=True)

        # Decay tick — keep recoloring (red → yellow → green) any sensor with a
        # still-recent detection, even if no new detection arrived this cycle,
        # so the marker visibly cools down instead of jumping straight back to
        # green at the next 60s device-status poll.
        with _device_lock:
            still_warm = [d for d, ts in _last_detection.items()
                          if now - ts <= ALERT_WARM_S and d not in touched_devices]
        for dev_id in still_warm:
            with _device_lock:
                last_ts = _last_detection.get(dev_id)
            if last_ts is not None:
                _publish_sensor_alert(pub_dev, dev_id, last_ts)

        time.sleep(DETECT_POLL_S)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="dronuradaras.lt → Zenoh bridge")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    session = zenoh.open(make_config())

    topic_dev = "{}/land/dronuradaras/acoustic/neutral/sensor/status/v1".format(TOPIC_ROOT)
    pub_dev = session.declare_publisher(topic_dev)

    print("dronuradaras bridge starting", flush=True)
    print("  Sensor markers (recolor on detection) →", topic_dev, flush=True)

    t_dev = threading.Thread(target=run_devices,    args=(pub_dev, args.verbose), daemon=True)
    t_det = threading.Thread(target=run_detections, args=(pub_dev, args.verbose), daemon=True)

    t_dev.start()
    t_det.start()

    try:
        t_dev.join()
    except KeyboardInterrupt:
        pass
    finally:
        pub_dev.undeclare()
        session.close()


if __name__ == "__main__":
    main()
