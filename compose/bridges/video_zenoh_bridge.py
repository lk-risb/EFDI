#!/usr/bin/env python3
"""Zenoh-native video prototype: one drone's H.264 feed, subscribed straight
off the fabric via GStreamer's zenohsrc element (github.com/p13marc/gst-plugin-zenoh)
instead of a separate RTMP/RTSP relay process on that leg.

The publish side (the drone/GCS companion computer) runs its OWN GStreamer
pipeline with zenohsink, publishing H.264 access units onto KEY_EXPR below —
that side is partner/vendor hardware, not something this repo runs. For local
testing without real drone hardware, see scripts/video-zenoh-publish-test.sh
(synthetic pattern) or scripts/video-zenoh-publish-file.sh (a real video file,
looped).

This bridge only owns the RECEIVE leg, and deliberately still hands the
result to mediamtx over RTMP (flvmux ! rtmp2sink -> mediamtx's own RTMP
ingest port) rather than trying to get a browser to speak Zenoh directly —
browsers have no Zenoh transport, so the WHEP leg StreamsPanel.tsx already
does (mediamtx's own WebRTC egress) is still required downstream of this.
What zenoh replaces here is the network leg from the drone to the gateway,
not the leg from the gateway to the browser tile. RTMP, not RTSP: this is
the exact ingest mediamtx.yml already documents for a real drone's own RTMP
push ("One drone push (RTMP, arbitrary operator-typed path...)") — reusing
it here means zero new mediamtx config, and sidesteps gst-plugin-rsrtsp's
rtspclientsink, which some distro builds (Arch's included) don't register at
all (only rtspsrc2, the source side). Once this publishes into mediamtx
under VIDEO_ZENOH_MEDIAMTX_PATH, it shows up in the existing "video wall"
grid with zero changes to StreamsPanel.tsx — mediamtx doesn't know or care
whether a path was populated over a drone's own RTMP push or (as here) an
RTMP push fed by Zenoh.

This wraps gst-launch-1.0 as a supervised subprocess (same pattern as
bridges/4609_bridge.py's ffmpeg wrapper) rather than binding PyGObject —
keeps this pod's Python dependency surface unchanged; the only new
requirement is a GStreamer install with gst-plugin-zenoh's .so on
GST_PLUGIN_PATH (see VIDEO_ZENOH_PLUGIN_PATH below), plus gst-plugins-good's
flvmux and gst-plugins-rs's rtmp2sink — both far more commonly packaged than
gst-plugin-rsrtsp's sink side.

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
  VIDEO_ZENOH_MEDIAMTX_PATH     # mediamtx path this shows up under in the
                                 # video wall (default: VIDEO_ZENOH_DRONE)
  VIDEO_ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448  # matches gateway.ENDPOINT
  VIDEO_ZENOH_RTMP_HOST=127.0.0.1
  VIDEO_ZENOH_RTMP_PORT=1935
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
from ecs_log import get_logger

log = get_logger("video-zenoh-bridge")

TOPIC_ROOT = topic_root()
_DRONE = os.environ.get("VIDEO_ZENOH_DRONE", "drone1").strip() or "drone1"
KEY_EXPR = os.environ.get("VIDEO_ZENOH_KEY", "").strip() or "{}/video/{}/h264".format(TOPIC_ROOT, _DRONE)
_MEDIAMTX_PATH = os.environ.get("VIDEO_ZENOH_MEDIAMTX_PATH", "").strip() or _DRONE
_LOCAL_ENDPOINT = os.environ.get("VIDEO_ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448").strip()
_RTMP_HOST = os.environ.get("VIDEO_ZENOH_RTMP_HOST", "127.0.0.1").strip() or "127.0.0.1"
_RTMP_PORT = os.environ.get("VIDEO_ZENOH_RTMP_PORT", "1935").strip() or "1935"
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
        "!", "flvmux", "streamable=true",
        # Explicit host/port/application/stream, not `location=<url>` — some
        # distro builds of rtmp2sink (Debian trixie's gst-plugins-bad
        # included) fail the location URI's host parse with "Host is not
        # set" even for a well-formed rtmp://host:port/path string, while the
        # same build's own separate properties connect fine. mediamtx forms
        # the registered path as "<application>/<stream>" — an empty
        # application gets the RTMP publish rejected outright ("connection
        # closed remotely") rather than falling back to just <stream>, so
        # MEDIAMTX_PATH becomes the app and "live" is a fixed stream key;
        # the video wall tile ends up named "<drone>/live" rather than a bare
        # "<drone>", which StreamsPanel doesn't care about either way.
        "!", "rtmp2sink", "host={}".format(_RTMP_HOST), "port={}".format(_RTMP_PORT),
        "application={}".format(_MEDIAMTX_PATH), "stream=live",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_gst_env())


def _stderr_pump(proc: "subprocess.Popen[bytes]") -> None:
    if proc.stderr is None:
        return
    for raw in iter(proc.stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            log.info(line, extra={"source": "gst-launch"})


def run(args: argparse.Namespace) -> None:
    log.info("Zenoh-native video bridge started", extra={
        "key_expr": KEY_EXPR, "rtmp_host": _RTMP_HOST, "rtmp_port": _RTMP_PORT, "mediamtx_path": _MEDIAMTX_PATH,
    })

    while True:
        proc = None
        stderr_thread = None
        try:
            config_path = render_zenoh_config(_LOCAL_ENDPOINT)
            proc = _gst_proc(config_path)
            stderr_thread = threading.Thread(target=_stderr_pump, args=(proc,), daemon=True)
            stderr_thread.start()
            log.info("pipeline running", extra={"pid": proc.pid})
            rc = proc.wait()
            raise RuntimeError("gst-launch-1.0 exited with code {}".format(rc))
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error("pipeline error, retrying", extra={"error": str(exc), "retry_s": _RECONNECT_S})
            time.sleep(_RECONNECT_S)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Zenoh-native video prototype: zenohsrc -> mediamtx RTMP")
    parser.add_argument("--verbose", "-v", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
