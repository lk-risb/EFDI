#!/usr/bin/env python3
"""dronuradaras_bridge.py — dronuradaras.lt drone radar network → Zenoh bridge.

Polls radar-api.mainline.inc (the backend for https://dronuradaras.lt) for:
  1. Radar sensor node positions  → published as land/neutral/radar site markers
  2. Drone detections             → published as air/unknown/uav tracks (30 s stale)

Zenoh topics:
  <ORG>/land/dronuradaras/neutral/radar/status/v1   — sensor nodes
  <ORG>/air/dronuradaras/hostile/uav/tracks/v1      — drone detections

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
ORG    = "1851281db70ccc0409dad4ecfc874cf5"
HERE   = os.path.dirname(os.path.abspath(__file__))

_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

API_BASE          = "https://radar-api.mainline.inc/api/v1/public"
ORIGIN_HEADER     = "https://dronuradaras.lt"
REFERER_HEADER    = "https://dronuradaras.lt/"

DEVICE_POLL_S     = 60    # radar nodes move rarely
DETECT_POLL_S     = 10    # drone detections — low latency
DETECT_WINDOW_S   = 300   # ignore detections older than 5 minutes


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
    topic_suffix = "land/dronuradaras/neutral/radar/status/v1"
    print("Device poll topic: {}/{}".format(ORG, topic_suffix), flush=True)

    while True:
        data = _get("devices")
        if data:
            devices = data.get("devices") or []
            online = [d for d in devices if d.get("is_online")]
            for dev in online:
                lat = dev.get("latitude")
                lon = dev.get("longitude")
                if lat is None or lon is None:
                    continue

                payload = {
                    "_src":        "dronuradaras",
                    "_ts":         time.time(),
                    "sensor_type": "radar",
                    "sensor_id":   "DRONU-{}".format(dev["id"][:8]),
                    "sensor_name": dev.get("display_name", "dronu-radar"),
                    "lat_deg":     round(lat, 6),
                    "lon_deg":     round(lon, 6),
                    "is_online":   dev.get("is_online", False),
                    "last_seen":   dev.get("last_seen_at", ""),
                }

                pub.put(json.dumps(payload).encode(),
                        encoding=zenoh.Encoding.APPLICATION_JSON)
                if verbose:
                    print("DEV", payload["sensor_name"],
                          "online={}".format(payload["is_online"]),
                          "{:.4f},{:.4f}".format(lat, lon), flush=True)

            print("Devices: {}/{} online published".format(len(online), len(devices)), flush=True)

        time.sleep(DEVICE_POLL_S)


# ---------------------------------------------------------------------------
# Detection publisher
# ---------------------------------------------------------------------------

def run_detections(pub: "zenoh.Publisher", verbose: bool):
    topic_suffix = "air/dronuradaras/hostile/uav/tracks/v1"
    print("Detection poll topic: {}/{}".format(ORG, topic_suffix), flush=True)

    seen: dict[str, float] = {}   # detection_id → published_at timestamp

    while True:
        now = time.time()

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

                lat = det.get("latitude")
                lon = det.get("longitude")
                if lat is None or lon is None:
                    continue

                # Filter by age — ignore detections older than DETECT_WINDOW_S
                detected_ms = det.get("detected_at", 0)
                detected_s  = detected_ms / 1000.0
                if (now - detected_s) > DETECT_WINDOW_S:
                    continue

                payload = {
                    "_src":           "dronuradaras",
                    "_ts":            detected_s,
                    "sensor_id":      "DRONU-DET-{}".format(det_id[:8]),
                    "callsign":       "DRONE",
                    "lat_deg":        round(lat, 6),
                    "lon_deg":        round(lon, 6),
                    "device_id":      det.get("device_id", ""),
                    "audio_available": det.get("audio_available", False),
                }

                pub.put(json.dumps(payload).encode(),
                        encoding=zenoh.Encoding.APPLICATION_JSON)
                seen[det_id] = now
                new_count += 1

                if verbose:
                    print("DET", det_id[:8],
                          "{:.4f},{:.4f}".format(lat, lon),
                          "audio={}".format(payload["audio_available"]), flush=True)

            if new_count:
                print("Detections: {} new".format(new_count), flush=True)

        time.sleep(DETECT_POLL_S)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="dronuradaras.lt → Zenoh bridge")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    session = zenoh.open(make_config())

    topic_dev = "{}/land/dronuradaras/neutral/radar/status/v1".format(ORG)
    topic_det = "{}/air/dronuradaras/hostile/uav/tracks/v1".format(ORG)

    pub_dev = session.declare_publisher(topic_dev)
    pub_det = session.declare_publisher(topic_det)

    print("dronuradaras bridge starting", flush=True)
    print("  Radar nodes  →", topic_dev, flush=True)
    print("  Detections   →", topic_det, flush=True)

    t_dev = threading.Thread(target=run_devices,    args=(pub_dev, args.verbose), daemon=True)
    t_det = threading.Thread(target=run_detections, args=(pub_det, args.verbose), daemon=True)

    t_dev.start()
    t_det.start()

    try:
        t_dev.join()
    except KeyboardInterrupt:
        pass
    finally:
        pub_dev.undeclare()
        pub_det.undeclare()
        session.close()


if __name__ == "__main__":
    main()
