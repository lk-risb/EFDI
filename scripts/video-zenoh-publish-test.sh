#!/usr/bin/env bash
# video-zenoh-publish-test.sh — local test publisher for the zenoh-native
# video prototype (see compose/bridges/video_zenoh_bridge.py).
#
# Publishes a synthetic test pattern over Zenoh via GStreamer's zenohsink
# element, standing in for a real drone/GCS companion computer's own
# zenohsink pipeline — that side runs on partner hardware and isn't part of
# this repo. Use this to validate the receive leg (video_zenoh_bridge.py ->
# mediamtx -> the StreamsPanel video wall) end to end before wiring a real
# feed.
#
# Requires GStreamer + gst-plugin-zenoh built and on GST_PLUGIN_PATH (same
# requirement as the receive-side bridge).
#
# Usage:
#   ./scripts/video-zenoh-publish-test.sh [drone-id]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/../compose"
DRONE="${1:-${VIDEO_ZENOH_DRONE:-drone1}}"
ENDPOINT="${VIDEO_ZENOH_LOCAL_ENDPOINT:-tcp/127.0.0.1:7448}"
KEY_EXPR="${VIDEO_ZENOH_KEY:-EFDI/video/${DRONE}/h264}"
GST_LAUNCH_BIN="${VIDEO_ZENOH_GST_LAUNCH_BIN:-gst-launch-1.0}"

if [[ -n "${VIDEO_ZENOH_PLUGIN_PATH:-}" ]]; then
    export GST_PLUGIN_PATH="${VIDEO_ZENOH_PLUGIN_PATH}${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"
fi

CONFIG_PATH="$(cd "$COMPOSE_DIR" && PYTHONPATH="$COMPOSE_DIR:$COMPOSE_DIR/control" python3 -m bridges.gst_zenoh_config "$ENDPOINT")"

# x264enc (gst-plugins-ugly, patent-encumbered) isn't installed on every dev
# box — openh264enc (gst-plugins-bad, BSD-licensed) is a drop-in fallback for
# this smoke test. Real drone-side encoders are the partner's own choice;
# this only needs to produce valid H.264, not any specific encoder.
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

echo "Publishing synthetic test pattern to key: $KEY_EXPR"
echo "Zenoh config: $CONFIG_PATH"
echo "Encoder: $ENCODER"

exec "$GST_LAUNCH_BIN" -e \
    videotestsrc is-live=true pattern=smpte \
    ! video/x-raw,width=1280,height=720,framerate=30/1 \
    ! timeoverlay \
    ! videoconvert \
    ! $ENCODER \
    ! h264parse config-interval=1 \
    ! zenohsink key-expr="$KEY_EXPR" config="$CONFIG_PATH" reliability=reliable
