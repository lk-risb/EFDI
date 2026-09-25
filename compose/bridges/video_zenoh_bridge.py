#!/usr/bin/env python3
"""Zenoh-native video prototype: one drone's H.264 feed, subscribed straight
off the fabric via GStreamer's zenohsrc element (github.com/p13marc/gst-plugin-zenoh)
instead of a separate RTMP/RTSP relay process on that leg.

The publish side (the drone/GCS companion computer) runs its OWN GStreamer
pipeline with zenohsink, publishing H.264 access units onto KEY_EXPR below —
that side is partner/vendor hardware, not something this repo runs. For local
testing without real drone hardware, see scripts/video-zenoh-publish-test.sh,
which publishes a synthetic test pattern to the same key.

This bridge only owns the RECEIVE leg, and deliberately still hands the result
to mediamtx over RTSP (rtspclientsink -> mediamtx's RTSP publish port) rather
than trying to get a browser to speak Zenoh directly — browsers have no Zenoh
transport, so the WHEP leg StreamsPanel.tsx already does (mediamtx's own
WebRTC egress) is still required downstream of this. What zenoh replaces here
is the network leg from the drone to the gateway, not the leg from the
gateway to the browser tile. Once this publishes into mediamtx under
VIDEO_ZENOH_RTSP_PATH, it shows up in the existing "video wall" grid with
zero changes to StreamsPanel.tsx — mediamtx doesn't know or care whether a
path was populated over RTMP, RTSP, or (as here) an RTSP push fed by Zenoh.

This wraps gst-launch-1.0 as a supervised subprocess (same pattern as
bridges/4609_bridge.py's ffmpeg wrapper) rather than binding PyGObject —
keeps this pod's Python dependency surface unchanged; the only new
requirement is a GStreamer install with gst-plugin-zenoh's .so on
GST_PLUGIN_PATH (see VIDEO_ZENOH_PLUGIN_PATH below).

That build is provisioned automatically: install.sh, update.sh, and
health.sh each call scripts/ensure-gst-zenoh.sh, which installs GStreamer's
dev packages, an isolated rustup toolchain (gst-plugin-zenoh's MSRV outruns
every distro-packaged rustc), and builds gst-plugin-zenoh itself under
POD_STATE_DIR — idempotent, and it writes VIDEO_ZENOH_PLUGIN_PATH into
compose/.env once done. Best-effort and never fatal to any of those three
scripts: until it succeeds, start.sh's video-zenoh-bridge svc_ready() just
leaves this one service unavailable. Manual re-run: scripts/ensure-gst-zenoh.sh.

Config (compose/.env):
  VIDEO_ZENOH_KEY               # zenoh key-expr to subscribe (default:
                                 # "<TOPIC_ROOT>/video/<VIDEO_ZENOH_DRONE>/h264")
  VIDEO_ZENOH_DRONE=drone1      # used to build the default key above and,
                                 # unless overridden, the mediamtx path name
  VIDEO_ZENOH_RTSP_PATH         # mediamtx path this shows up under in the
                                 # video wall (default: VIDEO_ZENOH_DRONE)
  VIDEO_ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448  # matches gateway.ENDPOINT
  VIDEO_ZENOH_RTSP_URL          # override the full mediamtx RTSP URL instead
                                 # of building it from RTSP_PATH
  VIDEO_ZENOH_RECEIVE_TIMEOUT_MS=2000
  VIDEO_ZENOH_GST_LAUNCH_BIN=gst-launch-1.0
  VIDEO_ZENOH_PLUGIN_PATH       # extra GST_PLUGIN_PATH entry for gst-plugin-zenoh's
                                 # built .so, if it isn't already on the system path
  VIDEO_ZENOH_RECONNECT_S=10

Run:
  venv/bin/python3 bridges/video_zenoh_bridge.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import threading
import time

from namespace_prefix import topic_root
from bridges.gst_zenoh_config import render as render_zenoh_config

TOPIC_ROOT = topic_root()
_DRONE = os.environ.get("VIDEO_ZENOH_DRONE", "drone1").strip() or "drone1"
KEY_EXPR = os.environ.get("VIDEO_ZENOH_KEY", "").strip() or "{}/video/{}/h264".format(TOPIC_ROOT, _DRONE)
_RTSP_PATH = os.environ.get("VIDEO_ZENOH_RTSP_PATH", "").strip() or _DRONE
_LOCAL_ENDPOINT = os.environ.get("VIDEO_ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448").strip()
_RTSP_URL = os.environ.get("VIDEO_ZENOH_RTSP_URL", "").strip() or "rtsp://127.0.0.1:8554/{}".format(_RTSP_PATH)
_RECEIVE_TIMEOUT_MS = os.environ.get("VIDEO_ZENOH_RECEIVE_TIMEOUT_MS", "2000").strip() or "2000"
_GST_LAUNCH_BIN = os.environ.get("VIDEO_ZENOH_GST_LAUNCH_BIN", "gst-launch-1.0")
_PLUGIN_PATH = os.environ.get("VIDEO_ZENOH_PLUGIN_PATH", "").strip()
_RECONNECT_S = float(os.environ.get("VIDEO_ZENOH_RECONNECT_S", "10"))


def _gst_env() -> dict:
    env = os.environ.copy()
    if _PLUGIN_PATH:
        existing = env.get("GST_PLUGIN_PATH", "")
        env["GST_PLUGIN_PATH"] = "{}:{}".format(_PLUGIN_PATH, existing) if existing else _PLUGIN_PATH
    return env


def _gst_proc(config_path: str) -> "subprocess.Popen[bytes]":
    cmd = [
        _GST_LAUNCH_BIN, "-q",
        "zenohsrc", "key-expr={}".format(KEY_EXPR), "config={}".format(config_path),
        "receive-timeout-ms={}".format(_RECEIVE_TIMEOUT_MS),
        "!", "queue",
        "!", "h264parse", "config-interval=-1",
        "!", "rtspclientsink", "location={}".format(_RTSP_URL), "latency=0",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_gst_env())


def _stderr_pump(proc: "subprocess.Popen[bytes]") -> None:
    if proc.stderr is None:
        return
    for raw in iter(proc.stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            print("video-zenoh-bridge gst-launch: {}".format(line), flush=True)


def run(args: argparse.Namespace) -> None:
    print("Zenoh-native video bridge started", flush=True)
    print("  Key   : {}".format(KEY_EXPR), flush=True)
    print("  RTSP  : {}".format(_RTSP_URL), flush=True)

    while True:
        proc = None
        stderr_thread = None
        try:
            config_path = render_zenoh_config(_LOCAL_ENDPOINT)
            proc = _gst_proc(config_path)
            stderr_thread = threading.Thread(target=_stderr_pump, args=(proc,), daemon=True)
            stderr_thread.start()
            print("video-zenoh-bridge pipeline running (pid {})".format(proc.pid), flush=True)
            rc = proc.wait()
            raise RuntimeError("gst-launch-1.0 exited with code {}".format(rc))
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("video-zenoh-bridge error: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Zenoh-native video prototype: zenohsrc -> mediamtx RTSP")
    parser.add_argument("--verbose", "-v", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
