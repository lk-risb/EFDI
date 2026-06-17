#!/usr/bin/env bash
# stop.sh — stop all bridges started by run.sh
#
# Usage:
#   ./stop.sh           # stop all bridges + zenoh router
#   ./stop.sh bridges   # bridges only
#   ./stop.sh layers    # layers only
#   ./stop.sh zenoh     # zenoh router only

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

case "$MODE" in
    all)
        echo "=== Stopping all ==="
        stop_scripts "*"
        stop_zenoh
        ;;
    bridges)
        echo "=== Stopping source bridges ==="
        for name in airplaneslive aisstream aprs fr24 opensky openmeteo meteolt yrno osm n2yo purpleair windy here-traffic notam; do
            stop_scripts "$name"
        done
        ;;
    layers)
        echo "=== Stopping protocol layers ==="
        for name in cot-udp cot-tcp cat62 sapient nffi; do
            stop_scripts "$name"
        done
        ;;
    zenoh)
        stop_zenoh
        ;;
    *)
        echo "Usage: $0 [all|bridges|layers|zenoh]"
        exit 1
        ;;
esac

echo "Done."
