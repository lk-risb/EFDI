#!/usr/bin/env bash
# video-zenoh-publish-file.sh — publish a real video file over Zenoh via
# zenohsink, looping it indefinitely. Same receive leg as
# video-zenoh-publish-test.sh (video_zenoh_bridge.py -> mediamtx -> the
# StreamsPanel video wall) — this just swaps the synthetic SMPTE pattern for
# actual footage, so a demo shows real motion instead of color bars.
#
# Usage:
#   ./scripts/video-zenoh-publish-file.sh [/path/to/video.mp4] [drone-id]
#
# With no file argument, defaults to scripts/fixtures/video-zenoh-test.mp4 —
# a small (~1.5MB) committed sample clip, so a fresh checkout has something
# to demo against with zero extra setup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/../compose"
DEFAULT_VIDEO_FILE="$SCRIPT_DIR/fixtures/video-zenoh-test.mp4"

VIDEO_FILE="${1:-$DEFAULT_VIDEO_FILE}"
[[ -f "$VIDEO_FILE" ]] || { echo "No such file: $VIDEO_FILE" >&2; exit 1; }
VIDEO_FILE="$(cd "$(dirname "$VIDEO_FILE")" && pwd)/$(basename "$VIDEO_FILE")"

DRONE="${2:-${VIDEO_ZENOH_DRONE:-drone1}}"
ENDPOINT="${VIDEO_ZENOH_LOCAL_ENDPOINT:-tcp/127.0.0.1:7448}"
KEY_EXPR="${VIDEO_ZENOH_KEY:-EFDI/video/${DRONE}/h264}"
GST_LAUNCH_BIN="${VIDEO_ZENOH_GST_LAUNCH_BIN:-gst-launch-1.0}"

if [[ -n "${VIDEO_ZENOH_PLUGIN_PATH:-}" ]]; then
    export GST_PLUGIN_PATH="${VIDEO_ZENOH_PLUGIN_PATH}${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"
fi

CONFIG_PATH="$(cd "$COMPOSE_DIR" && PYTHONPATH="$COMPOSE_DIR:$COMPOSE_DIR/control" python3 -m bridges.gst_zenoh_config "$ENDPOINT")"

# Same x264enc/openh264enc fallback as video-zenoh-publish-test.sh — pick
# whichever's actually installed rather than hardcoding one.
ENCODER="${VIDEO_ZENOH_TEST_ENCODER:-}"
if [[ -z "$ENCODER" ]]; then
    if GST_PLUGIN_PATH="$GST_PLUGIN_PATH" gst-inspect-1.0 x264enc >/dev/null 2>&1; then
        ENCODER="x264enc tune=zerolatency bitrate=2048 key-int-max=30"
    elif GST_PLUGIN_PATH="$GST_PLUGIN_PATH" gst-inspect-1.0 openh264enc >/dev/null 2>&1; then
        ENCODER="openh264enc"
    else
        echo "No H.264 encoder found (tried x264enc, openh264enc)." >&2
        echo "Install gst-plugins-ugly (x264enc) or gst-plugins-bad (openh264enc)." >&2
        exit 1
    fi
fi

echo "Publishing $VIDEO_FILE (looped) to key: $KEY_EXPR"
echo "Zenoh config: $CONFIG_PATH"
echo "Encoder: $ENCODER"
echo "Ctrl-C to stop."

# Source files vary in resolution/framerate; videoscale+videorate+capsfilter
# normalize decodebin's output to one fixed format so zenohsink's caps stay
# stable across a loop restart (or a different file) instead of changing
# mid-stream on the receive side. decodebin may also expose an audio pad —
# left unconnected on purpose, this leg is video-only.
trap 'exit 0' INT TERM
while true; do
    "$GST_LAUNCH_BIN" -e \
        filesrc location="$VIDEO_FILE" \
        ! decodebin \
        ! videoconvert \
        ! videoscale \
        ! videorate \
        ! video/x-raw,width=1280,height=720,framerate=30/1 \
        ! timeoverlay \
        ! $ENCODER \
        ! h264parse config-interval=1 \
        ! zenohsink key-expr="$KEY_EXPR" config="$CONFIG_PATH" reliability=reliable \
        || true
    echo "Playback ended — looping ($VIDEO_FILE)..."
    sleep 1
done
