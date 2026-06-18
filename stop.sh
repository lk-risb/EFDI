#!/usr/bin/env bash
# stop.sh — stop all bridges started by run.sh
#
# Usage:
#   ./stop.sh           # stop everything (bridges + layers + zenoh)
#   ./stop.sh giraffe   # stop open-API bridges only; leave cat48/21/20/link16 + cot-udp running
#   ./stop.sh bridges   # stop open-API source bridges only
#   ./stop.sh layers    # stop protocol layers only
#   ./stop.sh zenoh     # stop zenoh router only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR/.pids"
MODE="${1:-all}"

stop_scripts() {
    local pattern="${1:-*}"
    if [[ ! -d "$PID_DIR" ]]; then return; fi
    for pid_file in "$PID_DIR"/$pattern.pid; do
        [[ -f "$pid_file" ]] || continue
        name="$(basename "$pid_file" .pid)"
        pid="$(cat "$pid_file")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && echo "  [stop] $name (pid $pid)"
        else
            echo "  [gone] $name was not running"
        fi
        rm -f "$pid_file"
    done
}

stop_zenoh() {
    echo "  [stop] zenoh-router (Docker)"
    docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" stop zenoh-router 2>/dev/null || true
}

_OPEN_API_BRIDGES=(airplaneslive aisstream aprs fr24 opensky openmeteo meteolt yrno osm n2yo purpleair windy here-traffic notam)
_GIRAFFE_BRIDGES=(cat48 cat21 cat20 link16)
_LAYERS=(cot-udp cot-tcp cat62 cat48 cat21 cat20 link16 sapient nffi sitaware)

case "$MODE" in
    all)
        echo "=== Stopping all ==="
        stop_scripts "*"
        stop_zenoh
        ;;
    giraffe)
        echo "=== Stopping open-API bridges (leaving giraffe sensors + cot-udp) ==="
        for name in "${_OPEN_API_BRIDGES[@]}"; do
            stop_scripts "$name"
        done
        ;;
    bridges)
        echo "=== Stopping open-API source bridges ==="
        for name in "${_OPEN_API_BRIDGES[@]}"; do
            stop_scripts "$name"
        done
        ;;
    layers)
        echo "=== Stopping protocol layers ==="
        for name in "${_LAYERS[@]}"; do
            stop_scripts "$name"
        done
        ;;
    zenoh)
        stop_zenoh
        ;;
    *)
        echo "Usage: $0 [all|giraffe|bridges|layers|zenoh]"
        exit 1
        ;;
esac

echo "Done."
