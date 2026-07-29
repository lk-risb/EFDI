#!/usr/bin/env bash
# stop.sh — stop native bridges/layers started by start.sh or run.sh
#
# Usage:
#   ./stop.sh           # stop everything (bridges + protocols + layers + zenoh)
#   ./stop.sh giraffe   # stop source/API bridges; leave sensor protocols + output running
#   ./stop.sh bridges   # stop source-specific bridges only
#   ./stop.sh protocols # stop reusable input protocols only
#   ./stop.sh layers    # stop TAK and SitaWare layers only
#   ./stop.sh zenoh     # stop zenoh router only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/compose"
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
        [[ "$arg" == "$COMPOSE_DIR/"* || "$arg" == "$SCRIPT_DIR/scripts/"* ]] && return 0
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

_SOURCE_BRIDGES=(meteolt
                 dronuradaras sitaware udp-ingress track-fusion)
_PROTOCOLS=(asterix-cat10 asterix-cat20 asterix-cat21 asterix-cat34
            asterix-cat48 asterix-cat62 asterix-cat10-raw asterix-cat20-raw
            asterix-cat21-raw asterix-cat34-raw asterix-cat48-raw
            asterix-cat62-raw sapient
            nffi stanag4586 stanag4609)
_LAYERS=(cot_layer nvg_bridge nvg_layer)

case "$MODE" in
    all)
        echo "=== Stopping all ==="
        stop_scripts "*"
        stop_zenoh
        ;;
    giraffe)
        echo "=== Stopping open-API bridges (leaving giraffe sensors + layers) ==="
        for name in "${_SOURCE_BRIDGES[@]}"; do
            stop_scripts "$name"
        done
        ;;
    bridges)
        echo "=== Stopping source-specific bridges ==="
        for name in "${_SOURCE_BRIDGES[@]}"; do
            stop_scripts "$name"
        done
        ;;
    protocols)
        echo "=== Stopping input protocols ==="
        for name in "${_PROTOCOLS[@]}"; do
            stop_scripts "$name"
        done
        ;;
    layers)
        echo "=== Stopping TAK and SitaWare layers ==="
        for name in "${_LAYERS[@]}"; do
            stop_scripts "$name"
        done
        ;;
    zenoh)
        stop_zenoh
        ;;
    *)
        case "$MODE" in
            admin-control|cert-renewer|meteolt|dronuradaras|sitaware|tak-bridge|asterix|udp-ingress|track-fusion|asterix-cat10|asterix-cat20|asterix-cat21|asterix-cat34|asterix-cat48|asterix-cat62|asterix-cat10-raw|asterix-cat20-raw|asterix-cat21-raw|asterix-cat34-raw|asterix-cat48-raw|asterix-cat62-raw|sapient|nffi|stanag4586|stanag4609|sapient-raw|stanag4586-raw|stanag4609-raw|cap|geojson|mqtt|mqtt-raw|sensorthings|sensorthings-raw|sparkplug|spectrum|sensor-health|mission-route|cot_layer|nvg_bridge|nvg_layer)
                echo "=== Stopping $MODE ==="
                stop_scripts "$MODE"
                echo "Done."
                exit 0
                ;;
        esac
        echo "Usage: $0 [all|giraffe|bridges|protocols|layers|zenoh]"
        exit 1
        ;;
esac

echo "Done."
