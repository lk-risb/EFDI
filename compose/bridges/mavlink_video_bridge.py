#!/usr/bin/env python3
"""MAVLink video-stream auto-discovery -> MediaMTX pulled path, zero
per-drone config.

============================================================================
STATUS: untested shell, not a confirmed-working bridge yet
============================================================================
No drone/simulator was connected while this was written (same caveat as
mavlink_command_bridge.py — see that file for what IS/ISN'T verified about
mavutil.mavlink_connection/wait_heartbeat in this environment, which this
bridge relies on identically). What's additionally unverified here:
  - That a real autopilot/camera component actually answers
    MAV_CMD_SET_MESSAGE_INTERVAL for VIDEO_STREAM_INFORMATION (id 269) the
    way QGroundControl's camera manager relies on — this is the documented,
    standard MAVLink mechanism (mavlink.io/en/services/camera.html), not
    guessed, but needs a real camera-capable autopilot to confirm.
  - VIDEO_STREAM_INFORMATION.uri's exact wire encoding (str vs bytes vs
    list[int]) across pymavlink versions — _decode_char_field handles all
    three it's documented to produce, but only unit-tested, not seen live.
  - MediaMTX's /v3/config/paths/add + /v3/config/paths/patch behavior for a
    pulled RTSP/RTP/TCP-MPEG source at runtime (vs. list_streams' GET-only
    /v3/paths/list, which streams.py already uses live) — API shape is from
    MediaMTX's own OpenAPI spec, not exercised against a running instance
    yet from this bridge specifically.
============================================================================

MAVLink itself never carries video frames — a vehicle's VIDEO_STREAM_INFORMATION
message (MAVLink common dialect) only ADVERTISES a URI (almost always plain
RTSP, e.g. rtsp://<vehicle-ip>:8554/live) that the real video travels over
separately. Historically an operator had to notice that URI (QGroundControl's
own video settings page, or the vehicle's documentation) and hand-enter it
into mediamtx.yml as a path's `source:`. This bridge does that step
automatically: it asks every MAVLink system seen on MAVLINK_ENDPOINT to send
VIDEO_STREAM_INFORMATION (MAV_CMD_SET_MESSAGE_INTERVAL), and the moment one
arrives, registers that URI as a MediaMTX pulled path via MediaMTX's own
runtime config API — no mediamtx.yml edit, no restart, no operator action.
From there it's the same pull-and-serve path a DJI RTSP source already uses;
the drone's video just appears in the video wall (StreamsPanel.tsx) under a
mavlink-<system>-<name> tile the moment the vehicle starts advertising it.

Not for DJI — their SDK has no MAVLink VIDEO_STREAM_INFORMATION path (see
video_zenoh_bridge.py's docstring for the Zenoh-native drone-video path, and
mediamtx.yml's rtmp/rtsp/srt ingest for a DJI dock pushing or serving
directly — those already work with zero code, just point the dock at this
gateway). This is specifically for MAVLink-speaking airframes
(ArduPilot/PX4-class), same scope note as mavlink_command_bridge.py.

Config (compose/.env):
  MAVLINK_ENDPOINT=udp:127.0.0.1:14550   # same connection convention as
                                          # mavlink_command_bridge.py — a
                                          # MAVLink router/relay fanning out
                                          # to multiple listeners is assumed,
                                          # same as that bridge already does
  MAVLINK_VIDEO_REQUEST_INTERVAL_S=5     # how often to (re-)ask each system
                                          # for VIDEO_STREAM_INFORMATION
  MEDIAMTX_API_URL=http://127.0.0.1:9997 # matches streams.py's own default

Run:
  venv/bin/python3 bridges/mavlink_video_bridge.py
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

from pymavlink import mavutil
from pymavlink.dialects.v20 import common as mavlink_common

MAVLINK_ENDPOINT = os.environ.get("MAVLINK_ENDPOINT", "udp:127.0.0.1:14550")
_REQUEST_INTERVAL_S = float(os.environ.get("MAVLINK_VIDEO_REQUEST_INTERVAL_S", "5"))
_MEDIAMTX_API_URL = os.environ.get("MEDIAMTX_API_URL", "http://127.0.0.1:9997").rstrip("/")
_MSG_ID_VIDEO_STREAM_INFORMATION = mavlink_common.MAVLINK_MSG_ID_VIDEO_STREAM_INFORMATION
_MAV_CMD_SET_MESSAGE_INTERVAL = mavlink_common.MAV_CMD_SET_MESSAGE_INTERVAL
RECONNECT_S = 10


def _decode_char_field(value) -> str:
    """VIDEO_STREAM_INFORMATION's uri/name fields decode as either a str, a
    bytes, or a list[int] depending on pymavlink version — normalize to a
    trimmed str either way."""
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        raw = bytes(b for b in value if b)
        return raw.decode("utf-8", "replace")
    return str(value).split("\x00", 1)[0]


def _sanitize_path_name(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")
    return safe


def _mediamtx_add_path(name: str, source: str) -> bool:
    """Registers a pulled path with MediaMTX. Returns True if the path is
    now configured with this source (whether just-added or already there),
    False on an error worth retrying later."""
    body = json.dumps({"source": source}).encode()

    def _post(method: str, endpoint: str) -> bool:
        request = urllib.request.Request(
            f"{_MEDIAMTX_API_URL}/v3/config/paths/{endpoint}/{name}",
            data=body, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5):
            return True

    try:
        return _post("POST", "add")
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            print(f"mavlink_video_bridge: MediaMTX add path {name!r} failed: HTTP {exc.code}", flush=True)
            return False
        # 400 here means the path already exists (MediaMTX has no
        # add-if-absent) — patch it instead, in case the advertised URI
        # changed (e.g. the vehicle rebooted with a different camera).
        try:
            return _post("PATCH", "patch")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as patch_exc:
            print(f"mavlink_video_bridge: MediaMTX patch path {name!r} failed: {patch_exc}", flush=True)
            return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"mavlink_video_bridge: MediaMTX API unavailable: {exc}", flush=True)
        return False


class _VideoDiscoveryLink:
    def __init__(self):
        self._master = None
        self._requested_systems: dict[int, float] = {}
        self._registered: dict[tuple[int, int], str] = {}  # (system_id, stream_id) -> uri
        self._connect()

    def _connect(self) -> None:
        self._master = mavutil.mavlink_connection(MAVLINK_ENDPOINT)
        print(f"mavlink_video_bridge connecting to {MAVLINK_ENDPOINT} ...", flush=True)
        self._master.wait_heartbeat(timeout=30)
        print(f"mavlink_video_bridge got heartbeat from system {self._master.target_system}", flush=True)

    def _request_video_stream_info(self, system_id: int, component_id: int) -> None:
        # Re-requested periodically (not just once) since a camera component
        # can show up after the autopilot's own first heartbeat, and a
        # single lost COMMAND_LONG shouldn't mean this system's video never
        # gets discovered for the rest of the session.
        now = time.monotonic()
        if now - self._requested_systems.get(system_id, 0.0) < _REQUEST_INTERVAL_S:
            return
        self._requested_systems[system_id] = now
        self._master.mav.command_long_send(
            system_id, component_id,
            _MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            _MSG_ID_VIDEO_STREAM_INFORMATION,
            int(_REQUEST_INTERVAL_S * 1_000_000),
            0, 0, 0, 0, 0,
        )

    def _handle_video_stream_information(self, msg) -> None:
        uri = _decode_char_field(msg.uri)
        if not uri:
            return  # camera reports a stream slot with nothing configured yet
        system_id = msg.get_srcSystem()
        key = (system_id, msg.stream_id)
        if self._registered.get(key) == uri:
            return  # unchanged since last time — nothing to do
        label = _sanitize_path_name(_decode_char_field(msg.name)) or f"stream{msg.stream_id}"
        path_name = f"mavlink-{system_id}-{label}"
        if _mediamtx_add_path(path_name, uri):
            self._registered[key] = uri
            print(f"mavlink_video_bridge: registered {path_name!r} -> {uri}", flush=True)

    def run(self) -> None:
        while True:
            msg = self._master.recv_match(blocking=True, timeout=2)
            if msg is None:
                continue
            msg_type = msg.get_type()
            if msg_type == "HEARTBEAT":
                self._request_video_stream_info(msg.get_srcSystem(), msg.get_srcComponent())
            elif msg_type == "VIDEO_STREAM_INFORMATION":
                self._handle_video_stream_information(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description="MAVLink video-stream auto-discovery -> MediaMTX")
    ap.parse_args()

    while True:
        try:
            link = _VideoDiscoveryLink()
            print(f"mavlink_video_bridge ready, polling {MAVLINK_ENDPOINT}", flush=True)
            link.run()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"mavlink_video_bridge connection error: {exc} — retry in {RECONNECT_S}s", flush=True)
            time.sleep(RECONNECT_S)


if __name__ == "__main__":
    main()
