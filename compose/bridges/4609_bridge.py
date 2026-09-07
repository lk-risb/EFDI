#!/usr/bin/env python3
"""STANAG 4609 SRT/KLV ingress -> Zenoh raw bytes.

Reads an SRT transport carrying MPEG-TS with KLV metadata, extracts the KLV
packet stream with ffmpeg, and publishes each raw MISB KLV packet onto the EFDI
fabric's raw namespace. Decoding into canonical tracks is the protocol's job:
protocols/vendors/stanag/4609.py subscribes to this raw topic and emits the
SAPIENT / JSON / protobuf views. This bridge never decodes ST 0601 and never
transcodes the video essence — the video, when relayed at all (see below), is
a remux (-c copy) of exactly the same bytes ffmpeg already demuxed.

This bridge owns the SRT connection as the *listener* (mode=listener — the
sensor/GCS connects IN), so mediamtx cannot also bind that port to get the
video into the WebUI Streams tab, and routing the feed through mediamtx
*first* isn't an option either: MediaMTX has no support for passing through
an MPEG-TS KLV/data track today (open upstream feature request, not
implemented), so the metadata would be silently dropped before this bridge
ever saw it. The only place both concerns can share one connection is here.

Optional video relay (compose/.env):
  STANAG4609_VIDEO_RELAY_ENABLE=1        # off by default
  STANAG4609_VIDEO_PATH=stanag4609       # path name mediamtx (and the WebUI
                                          # Streams tab) will show it under

When enabled, ffmpeg's own `tee` muxer splits ONE demux of the SRT input into
two independent outputs: the KLV data track (unchanged, to this process'
stdout) and a best-effort remux of the video/audio tracks into mediamtx's
RTMP ingest (127.0.0.1:1935). The video slave is tagged onfail=ignore, so if
mediamtx is down, slow to start, or restarted mid-stream, ffmpeg silently
carries on writing KLV to stdout regardless — the metadata pipeline this
bridge exists for never depends on the video relay's success. Two outputs
from one already-open connection, not two listeners on one port.

Config (compose/.env):
  STANAG4609_SRT_URL=srt://host:port?mode=listener  # required
  STANAG4609_SOURCE=optional-stream-name            # optional source tag

Run:
  venv/bin/python3 bridges/4609_bridge.py
"""

from __future__ import annotations

import argparse
from importlib import import_module
import json
import os
import subprocess
import threading
import time
from urllib.parse import urlsplit


from namespace_prefix import topic_root
import zenoh
from protocols.gateway import open_session

# The KLV wire format lives in the protocol module (single, test-covered source
# of truth). The bridge reuses only the streaming framer and the BER encoder to
# reassemble each packet's exact bytes; it does no ST 0601 decoding.
_codec = import_module("protocols.vendors.stanag.stanag")
_parse_klv_packets = _codec._4609_parse_klv_packets
_encode_ber = _codec._4609_encode_ber

TOPIC_ROOT = topic_root()
_SRT_URL = os.environ.get("STANAG4609_SRT_URL", "").strip()
SOURCE = os.environ.get("STANAG4609_SOURCE", "klv").strip() or "klv"
_FFMPEG_BIN = os.environ.get("STANAG4609_FFMPEG_BIN", "ffmpeg")
_RECONNECT_S = float(os.environ.get("STANAG4609_RECONNECT_S", "10"))
RAW_TOPIC = "{}/raw/stanag_4609/{}".format(TOPIC_ROOT, SOURCE)

_VIDEO_RELAY_ENABLE = os.environ.get("STANAG4609_VIDEO_RELAY_ENABLE", "") == "1"
_VIDEO_PATH = os.environ.get("STANAG4609_VIDEO_PATH", "stanag4609").strip() or "stanag4609"
_VIDEO_RELAY_URL = "rtmp://127.0.0.1:1935/{}".format(_VIDEO_PATH)


def _safe_stream_label(url: str) -> str:
    """Return a useful endpoint label without credentials or SRT options."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "configured-host"
        port = ":{}".format(parsed.port) if parsed.port is not None else ""
        return "{}://{}{}".format(parsed.scheme or "srt", host, port)
    except ValueError:
        return "srt://configured-host"


def _ffmpeg_proc() -> "subprocess.Popen[bytes]":
    if not _SRT_URL:
        raise SystemExit("Set STANAG4609_SRT_URL in .env")
    if not _VIDEO_RELAY_ENABLE:
        cmd = [
            _FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-i", _SRT_URL,
            "-map", "0:d:0?",
            "-c", "copy",
            "-f", "data",
            "pipe:1",
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # tee splits ONE demux into two independent outputs instead of opening a
    # second connection (this bridge already owns the only listener on
    # STANAG4609_SRT_URL — see module docstring). onfail=ignore on the video
    # slave means a down/restarting mediamtx never affects the KLV slave.
    tee_spec = "[select=d:f=data]pipe:1|[select=v,a:onfail=ignore:f=flv]{}".format(_VIDEO_RELAY_URL)
    cmd = [
        _FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-i", _SRT_URL,
        "-map", "0",
        "-c", "copy",
        "-f", "tee",
        tee_spec,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _stderr_pump(proc: "subprocess.Popen[bytes]") -> None:
    if proc.stderr is None:
        return
    for raw in iter(proc.stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            print("STANAG4609 ffmpeg: {}".format(line), flush=True)


def run(args):
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("STANAG4609 Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    publisher = session.declare_publisher(RAW_TOPIC)
    print("STANAG 4609 SRT/KLV ingress started", flush=True)
    print("  SRT   : {}".format(_safe_stream_label(_SRT_URL)), flush=True)
    print("  Raw   : {}".format(RAW_TOPIC), flush=True)

    while True:
        proc = None
        stderr_thread = None
        try:
            proc = _ffmpeg_proc()
            assert proc.stdout is not None
            stderr_thread = threading.Thread(target=_stderr_pump, args=(proc,), daemon=True)
            stderr_thread.start()
            print("STANAG4609 ffmpeg connected", flush=True)
            count = 0
            for key, value in _parse_klv_packets(proc.stdout):
                raw_packet = key + _encode_ber(len(value)) + value
                publisher.put(raw_packet, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                count += 1
                if args.verbose:
                    print("STANAG4609 RAW {} key={} len={}".format(
                        RAW_TOPIC, key.hex()[:12], len(value)), flush=True)
            rc = proc.wait(timeout=5)
            raise RuntimeError("ffmpeg exited with code {} (published {} packets)".format(rc, count))
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("STANAG4609 error: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass

    session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="STANAG 4609 SRT/KLV ingress -> Zenoh raw bytes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
