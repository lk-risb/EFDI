#!/usr/bin/env bash
# stop.sh — stop native bridges/layers started by start.sh or run.sh
#
# Usage:
#   ./stop.sh           # stop everything (bridges + layers + zenoh)
#   ./stop.sh giraffe   # stop open-API bridges only; leave cat48/21/20/link16 + cot-udp running
#   ./stop.sh bridges   # stop open-API source bridges only
#   ./stop.sh layers    # stop protocol layers only
#   ./stop.sh zenoh     # stop zenoh router only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/compose/bridge"
ENV_FILE="$SCRIPT_DIR/compose/.env"
MODE="${1:-all}"

# ── Load .env (safe — no shell re-parsing of values, same as start.sh) ────
if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%$'\r'}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" != *=* ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        printf -v "$key" '%s' "$val"
        # shellcheck disable=SC2163  # $key holds a variable NAME (set above via printf -v),
        # not the value to export — exporting by that name is the intended idiom here.
        export "$key"
    done < "$ENV_FILE"
fi

# Must match start.sh/run.sh's in-repo default.
PID_DIR="${POD_STATE_DIR:-$SCRIPT_DIR/compose/state}/.pids"

is_bridge_pid() {
    local pid="$1" arg
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && [[ -r "/proc/$pid/cmdline" ]] || return 1
    while IFS= read -r -d '' arg; do
        [[ "$arg" == "$BRIDGE_DIR/"* ]] && return 0
    done < "/proc/$pid/cmdline"
    return 1
}

stop_scripts() {
    local pattern="${1:-*}"
    if [[ ! -d "$PID_DIR" ]]; then return; fi
    for pid_file in "$PID_DIR"/$pattern.pid; do
        [[ -f "$pid_file" ]] || continue
        name="$(basename "$pid_file" .pid)"
        pid="$(cat "$pid_file")"
        if is_bridge_pid "$pid"; then
            kill "$pid" 2>/dev/null && echo "  [stop] $name (pid $pid)"
        else
            echo "  [gone] $name PID file is stale or belongs to another process"
        fi
        rm -f "$pid_file"
    done
}

stop_zenoh() {
    echo "  [stop] zenoh-router (Docker)"
    docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" stop zenoh-router 2>/dev/null || true
}

_OPEN_API_BRIDGES=(airplaneslive aisstream aprs fr24 opensky openmeteo meteolt here-traffic notam dronuradaras cmems)
_GIRAFFE_BRIDGES=(cat48 cat21 cat20 link16 mavlink vmf cot-rx)
_LAYERS=(cot-udp cot-udp-tak cot-tcp cat62 cat48 cat21 cat20 link16 mavlink vmf cot-rx track-fusion sapient nffi sitaware sitaware-nvg sitaware-hq-nvg stanag4586)

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
