#!/usr/bin/env bash
# run.sh — start EFDI bridges as background scripts (no Docker required).
# The Zenoh router is still started via Docker (single container, compiled binary).
#
# Usage:
#   ./run.sh            # giraffe mode (default) — radar/Link-16 sensors + CoT-UDP only
#   ./run.sh giraffe    # same as above — CAT-48/21/20, Link-16, cot-udp
#   ./run.sh all        # everything — all open-API bridges + all layers
#   ./run.sh bridges    # open-API bridges only (skip zenoh + layers)
#   ./run.sh layers     # all protocol layers only (cot, cat62, sapient, nffi, …)
#
# Logs go to logs/<name>.log  PIDs saved to .pids/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/compose/bridge"
ENV_FILE="$SCRIPT_DIR/compose/.env"
VENV="$BRIDGE_DIR/venv"
LOG_DIR="$SCRIPT_DIR/logs"
PID_DIR="$SCRIPT_DIR/.pids"

mkdir -p "$LOG_DIR" "$PID_DIR"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [[ -f "$ENV_FILE" ]]; then
    # Parse .env manually — split key/value before export so special chars
    # in values (|, &, ;) are never interpreted as shell operators.
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%$'\r'}"                              # strip Windows CR
        [[ -z "$line" || "$line" == \#* ]] && continue   # skip blank/comments
        [[ "$line" != *=* ]] && continue                  # skip lines without =
        key="${line%%=*}"
        val="${line#*=}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue  # valid var name only
        printf -v "$key" '%s' "$val"   # set variable safely — no shell re-parsing
        export "$key"
    done < "$ENV_FILE"
else
    echo "WARNING: $ENV_FILE not found — API keys will be missing"
fi

export ZENOH_LOCAL_ENDPOINT="${ZENOH_LOCAL_ENDPOINT:-tcp/127.0.0.1:7448}"
export GOAT_CERT_DIR="${BUNDLE_DIR:-$HOME/goat-bundle}"

# ---------------------------------------------------------------------------
# Venv setup
# ---------------------------------------------------------------------------
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "Creating venv at $VENV …"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet eclipse-zenoh==1.9.0
    echo "Venv ready."
fi
PYTHON="$VENV/bin/python3"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
start() {
    local name="$1"; shift
    local script="$1"; shift
    local pid_file="$PID_DIR/$name.pid"

    if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        echo "  [skip] $name already running (pid $(cat "$pid_file"))"
        return
    fi

    "$PYTHON" "$BRIDGE_DIR/$script" "$@" \
        >> "$LOG_DIR/$name.log" 2>&1 &
    echo $! > "$pid_file"
    echo "  [start] $name  (pid $!)"
}

skip_if_no_key() {
    local key_val="${!1:-}"
    [[ -z "$key_val" ]]
}

# ---------------------------------------------------------------------------
# Zenoh router — one Docker container, everything else is native
# ---------------------------------------------------------------------------
start_zenoh() {
    if docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" ps zenoh-router \
            --format "{{.Status}}" 2>/dev/null | grep -q "healthy\|Up"; then
        echo "  [skip] zenoh-router already running"
    else
        echo "  [start] zenoh-router (Docker)"
        docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" up -d zenoh-router
        echo -n "  Waiting for zenoh-router healthy"
        for _ in $(seq 1 20); do
            sleep 1
            if docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" ps zenoh-router \
                    --format "{{.Status}}" 2>/dev/null | grep -q "healthy"; then
                echo " OK"
                return
            fi
            echo -n "."
        done
        echo " timeout — continuing anyway"
    fi
}

# ---------------------------------------------------------------------------
# Source bridges (external data → Zenoh)
# ---------------------------------------------------------------------------
start_bridges() {
    echo ""
    echo "=== Source bridges ==="

    start airplaneslive bridges/airplaneslive_adsb_bridge.py

    if skip_if_no_key AISSTREAM_KEY; then
        echo "  [skip] aisstream — AISSTREAM_KEY not set"
    else
        start aisstream bridges/aisstream_ws_bridge.py --apikey "$AISSTREAM_KEY"
    fi

    start aprs bridges/aprsis_bridge.py

    if skip_if_no_key FR24_KEY; then
        echo "  [skip] fr24 — FR24_KEY not set"
    else
        start fr24 bridges/fr24_live_bridge.py --key "$FR24_KEY"
    fi

    start opensky bridges/opensky_states_bridge.py
    start openmeteo bridges/openmeteo_forecast_bridge.py
    start meteolt bridges/meteolt_forecast_bridge.py
    start yrno bridges/yrno_forecast_bridge.py
    start osm bridges/osm_overpass_bridge.py

    if skip_if_no_key N2YO_KEY; then
        echo "  [skip] n2yo — N2YO_KEY not set"
    else
        start n2yo bridges/n2yo_satpos_bridge.py --key "$N2YO_KEY"
    fi

    if skip_if_no_key PURPLEAIR_KEY; then
        echo "  [skip] purpleair — PURPLEAIR_KEY not set"
    else
        start purpleair bridges/purpleair_sensor_bridge.py --key "$PURPLEAIR_KEY"
    fi

    if skip_if_no_key WINDY_KEY; then
        echo "  [skip] windy — WINDY_KEY not set"
    else
        start windy bridges/windy_forecast_bridge.py --key "$WINDY_KEY"
    fi

    if skip_if_no_key HERE_KEY; then
        echo "  [skip] here-traffic — HERE_KEY not set"
    else
        start here-traffic bridges/here_traffic_bridge.py --key "$HERE_KEY"
    fi

    if skip_if_no_key ICAO_NOTAM_KEY; then
        echo "  [skip] notam — ICAO_NOTAM_KEY not set"
    else
        start notam bridges/icao_notam_bridge.py --key "$ICAO_NOTAM_KEY"
    fi
}

# ---------------------------------------------------------------------------
# Protocol layers (Zenoh → TAK / external systems)
# ---------------------------------------------------------------------------
start_layers() {
    echo ""
    echo "=== Protocol layers ==="

    # CoT → ATAK UDP multicast (no TAK Server needed)
    start cot-udp layers/cot_layer.py --udp --host 239.2.3.1 --port 6969

    # CoT → FreeTAKServer TCP (only if TAK_HOST is set to something reachable)
    if [[ "${TAK_HOST:-127.0.0.1}" != "127.0.0.1" ]] || \
       nc -z "${TAK_HOST:-127.0.0.1}" "${TAK_PORT:-8087}" 2>/dev/null; then
        start cot-tcp layers/cot_layer.py --host "${TAK_HOST:-127.0.0.1}" --port "${TAK_PORT:-8087}"
    else
        echo "  [skip] cot-tcp — TAK Server not reachable at ${TAK_HOST:-127.0.0.1}:${TAK_PORT:-8087}"
    fi

    # CAT62 radar — outbound connect to a track server
    if [[ "${RADAR_HOST:-}" && "${RADAR_HOST}" != "127.0.0.1" ]]; then
        start cat62 layers/cat62_layer.py --radar-host "$RADAR_HOST" --radar-port "${RADAR_PORT:-30002}"
    else
        echo "  [skip] cat62 — set RADAR_HOST in .env to enable"
    fi

    # CAT48/34 radar — inbound listener (Giraffe connects/sends to us)
    # Set CAT48_PORT to activate. Optional: CAT48_RADAR_LAT/LON for polar→WGS84.
    # Set CAT48_TCP=1 if the radar uses TCP instead of UDP.
    if [[ "${CAT48_PORT:-}" ]]; then
        tcp_flag=""
        [[ "${CAT48_TCP:-}" == "1" ]] && tcp_flag="--tcp"
        start cat48 bridges/cat48_bridge.py \
            --port "$CAT48_PORT" \
            ${tcp_flag:+"$tcp_flag"} \
            ${CAT48_RADAR_LAT:+--radar-lat "$CAT48_RADAR_LAT"} \
            ${CAT48_RADAR_LON:+--radar-lon "$CAT48_RADAR_LON"}
    else
        echo "  [skip] cat48 — set CAT48_PORT in .env to enable"
    fi

    # CAT-021 ADS-B — inbound ASTERIX from a Mode-S ground station
    if [[ "${CAT21_PORT:-}" ]]; then
        tcp21=""
        [[ "${CAT21_TCP:-}" == "1" ]] && tcp21="--tcp"
        start cat21 bridges/cat21_bridge.py --port "$CAT21_PORT" ${tcp21:+"$tcp21"}
    else
        echo "  [skip] cat21 — set CAT21_PORT in .env to enable"
    fi

    # CAT-020 MLAT — inbound ASTERIX from a multilateration network
    if [[ "${CAT20_PORT:-}" ]]; then
        tcp20=""
        [[ "${CAT20_TCP:-}" == "1" ]] && tcp20="--tcp"
        start cat20 bridges/cat20_bridge.py --port "$CAT20_PORT" ${tcp20:+"$tcp20"}
    else
        echo "  [skip] cat20 — set CAT20_PORT in .env to enable"
    fi

    # SitaWare — REST poll for unit positions (friendly force tracking)
    if [[ "${SITAWARE_URL:-}" ]]; then
        sitaware_flags=""
        [[ "${SITAWARE_DISCOVER:-}" == "1" ]] && sitaware_flags="--discover"
        start sitaware bridges/sitaware_bridge.py ${sitaware_flags:+"$sitaware_flags"}
    else
        echo "  [skip] sitaware — set SITAWARE_URL in .env to enable"
    fi

    # CoT receiver — inbound CoT from external source (e.g. Giraffe radar)
    # Set COT_RX_PORT to open a listener (they connect to us)
    # Set COT_RX_HOST to connect outbound (we connect to them, format IP:PORT)
    if [[ "${COT_RX_PORT:-}" ]]; then
        start cot-rx layers/cot_receiver_bridge.py --listen "$COT_RX_PORT"
    elif [[ "${COT_RX_HOST:-}" ]]; then
        start cot-rx layers/cot_receiver_bridge.py --connect "$COT_RX_HOST"
    else
        echo "  [skip] cot-rx — set COT_RX_PORT or COT_RX_HOST in .env to enable"
    fi

    # SAPIENT — only if SAPIENT_HOST set
    if [[ "${SAPIENT_HOST:-}" ]]; then
        start sapient layers/sapient_layer.py --host "$SAPIENT_HOST" --port "${SAPIENT_PORT:-7001}"
    else
        echo "  [skip] sapient — set SAPIENT_HOST in .env to enable"
    fi

    # NATO NFFI — only if NFFI_HOST set
    if [[ "${NFFI_HOST:-}" ]]; then
        start nffi layers/nato_nffi_layer.py --host "$NFFI_HOST" --port "${NFFI_PORT:-7010}"
    else
        echo "  [skip] nffi — set NFFI_HOST in .env to enable"
    fi

    # Link 16 JREAP-C — inbound UDP/TCP listener
    # Set LINK16_PORT to activate. Set LINK16_TCP=1 for TCP mode.
    if [[ "${LINK16_PORT:-}" ]]; then
        tcp16=""
        [[ "${LINK16_TCP:-}" == "1" ]] && tcp16="--tcp"
        start link16 bridges/link16_bridge.py --port "$LINK16_PORT" ${tcp16:+"$tcp16"}
    else
        echo "  [skip] link16 — set LINK16_PORT in .env to enable"
    fi
}

# ---------------------------------------------------------------------------
# Giraffe-only subset: ASTERIX + Link-16 inbound bridges, then CoT-UDP out
# ---------------------------------------------------------------------------
start_giraffe_bridges() {
    echo ""
    echo "=== Giraffe sensor bridges ==="

    if [[ "${CAT48_PORT:-}" ]]; then
        tcp_flag=""
        [[ "${CAT48_TCP:-}" == "1" ]] && tcp_flag="--tcp"
        start cat48 bridges/cat48_bridge.py \
            --port "$CAT48_PORT" \
            ${tcp_flag:+"$tcp_flag"} \
            ${CAT48_RADAR_LAT:+--radar-lat "$CAT48_RADAR_LAT"} \
            ${CAT48_RADAR_LON:+--radar-lon "$CAT48_RADAR_LON"}
    else
        echo "  [skip] cat48 — set CAT48_PORT in .env to enable"
    fi

    if [[ "${CAT21_PORT:-}" ]]; then
        tcp21=""
        [[ "${CAT21_TCP:-}" == "1" ]] && tcp21="--tcp"
        start cat21 bridges/cat21_bridge.py --port "$CAT21_PORT" ${tcp21:+"$tcp21"}
    else
        echo "  [skip] cat21 — set CAT21_PORT in .env to enable"
    fi

    if [[ "${CAT20_PORT:-}" ]]; then
        tcp20=""
        [[ "${CAT20_TCP:-}" == "1" ]] && tcp20="--tcp"
        start cat20 bridges/cat20_bridge.py --port "$CAT20_PORT" ${tcp20:+"$tcp20"}
    else
        echo "  [skip] cat20 — set CAT20_PORT in .env to enable"
    fi

    if [[ "${LINK16_PORT:-}" ]]; then
        tcp16=""
        [[ "${LINK16_TCP:-}" == "1" ]] && tcp16="--tcp"
        start link16 bridges/link16_bridge.py --port "$LINK16_PORT" ${tcp16:+"$tcp16"}
    else
        echo "  [skip] link16 — set LINK16_PORT in .env to enable"
    fi

    if [[ "${MAVLINK_PORT:-}" ]]; then
        tcp_mav=""
        [[ "${MAVLINK_TCP:-}" == "1" ]] && tcp_mav="--tcp"
        start mavlink bridges/mavlink_bridge.py --port "$MAVLINK_PORT" ${tcp_mav:+"$tcp_mav"}
    else
        echo "  [skip] mavlink — set MAVLINK_PORT in .env to enable"
    fi

    if [[ "${VMF_PORT:-}" ]]; then
        tcp_vmf=""
        [[ "${VMF_TCP:-}" == "1" ]] && tcp_vmf="--tcp"
        start vmf bridges/vmf_bridge.py --port "$VMF_PORT" ${tcp_vmf:+"$tcp_vmf"}
    else
        echo "  [skip] vmf — set VMF_PORT in .env to enable"
    fi

    if [[ "${COT_RX_PORT:-}" ]]; then
        start cot-rx layers/cot_receiver_bridge.py --listen "$COT_RX_PORT"
    elif [[ "${COT_RX_HOST:-}" ]]; then
        start cot-rx layers/cot_receiver_bridge.py --connect "$COT_RX_HOST"
    else
        echo "  [skip] cot-rx — set COT_RX_PORT or COT_RX_HOST in .env to enable"
    fi
}

start_giraffe_layers() {
    echo ""
    echo "=== Protocol layers (radar → CoT) ==="

    # CoT → ATAK UDP multicast
    start cot-udp layers/cot_layer.py --udp --host 239.2.3.1 --port 6969

    # CoT → FreeTAKServer TCP (only if reachable)
    if [[ "${TAK_HOST:-127.0.0.1}" != "127.0.0.1" ]] || \
       nc -z "${TAK_HOST:-127.0.0.1}" "${TAK_PORT:-8087}" 2>/dev/null; then
        start cot-tcp layers/cot_layer.py --host "${TAK_HOST:-127.0.0.1}" --port "${TAK_PORT:-8087}"
    else
        echo "  [skip] cot-tcp — TAK Server not reachable at ${TAK_HOST:-127.0.0.1}:${TAK_PORT:-8087}"
    fi

    # Track fusion — always start in giraffe mode (correlates radar + any ADS-B)
    start track-fusion layers/track_fusion_layer.py

    # STANAG 4586 UAS interface — only if VSM host is configured
    if [[ "${STANAG4586_HOST:-}" ]]; then
        start stanag4586 layers/stanag4586_layer.py --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
    else
        echo "  [skip] stanag4586 — set STANAG4586_HOST in .env to enable"
    fi
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
MODE="${1:-giraffe}"

case "$MODE" in
    giraffe)
        start_zenoh
        start_giraffe_bridges
        start_giraffe_layers
        ;;
    all)
        start_zenoh
        start_bridges
        start_layers
        ;;
    bridges)
        start_bridges
        ;;
    layers)
        start_layers
        ;;
    *)
        echo "Usage: $0 [giraffe|all|bridges|layers]"
        exit 1
        ;;
esac

echo ""
echo "Logs: $LOG_DIR/"
echo "Stop: ./stop.sh"
