#!/usr/bin/env python3
"""STANAG 4609 SRT transport ingress → raw KLV on Zenoh."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time

import zenoh

from protocols._vendor.stanag.stanag4609_common import RAW_TOPIC, make_config, parse_klv_packets

_SRT_URL = os.environ.get("STANAG4609_SRT_URL", "").strip()
_FFMPEG_BIN = os.environ.get("STANAG4609_FFMPEG_BIN", "ffmpeg")
_RECONNECT_S = float(os.environ.get("STANAG4609_RECONNECT_S", "10"))
_READ_CHUNK = int(os.environ.get("STANAG4609_READ_CHUNK", "65536"))
_STREAM_ID = os.environ.get("STANAG4609_STREAM_ID")


def _ffmpeg_proc() -> subprocess.Popen[bytes]:
    if not _SRT_URL:
        raise SystemExit("Set STANAG4609_SRT_URL in .env")
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


def _stderr_pump(proc: subprocess.Popen[bytes]) -> None:
    if proc.stderr is None:
        return
    for raw in iter(proc.stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            print("STANAG4609 ffmpeg: {}".format(line), flush=True)


def run(args):
    while True:
        try:
            session = zenoh.open(make_config())
            break
        except Exception as exc:
            print("STANAG4609 SRT Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    pub = session.declare_publisher(RAW_TOPIC)
    print("STANAG 4609 SRT bridge started", flush=True)
    print("  SRT    : {}".format(_SRT_URL), flush=True)
    print("  Topic  : {}".format(RAW_TOPIC), flush=True)

    while True:
        proc = None
        try:
            proc = _ffmpeg_proc()
            assert proc.stdout is not None
            threading.Thread(target=_stderr_pump, args=(proc,), daemon=True).start()
            print("STANAG4609 ffmpeg connected", flush=True)
            packet_index = 0
            for key, value in parse_klv_packets(proc.stdout):
                raw_packet = key + value
                pub.put(raw_packet, encoding=zenoh.Encoding.APPLICATION_OCTET_STREAM)
                packet_index += 1
            rc = proc.wait(timeout=5)
            raise RuntimeError("ffmpeg exited with code {}".format(rc))
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print("STANAG4609 SRT error: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass

    pub.undeclare()
    session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="STANAG 4609 SRT → Zenoh raw KLV")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
