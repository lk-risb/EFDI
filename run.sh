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
        # shellcheck disable=SC2163  # $key holds a variable NAME (set above via printf -v),
        # not the value to export — exporting by that name is the intended idiom here.
        export "$key"
    done < "$ENV_FILE"
else
    echo "WARNING: $ENV_FILE not found — API keys will be missing"
fi

export ZENOH_LOCAL_ENDPOINT="${ZENOH_LOCAL_ENDPOINT:-tcp/127.0.0.1:7448}"
export BUNDLE_DIR="${BUNDLE_DIR:-$SCRIPT_DIR/compose/certs}"
export EFDI_CERT_DIR="$BUNDLE_DIR"
export POD_STATE_DIR="${POD_STATE_DIR:-$SCRIPT_DIR/compose/state}"
export NAMESPACE_PREFIX_FILE="${POD_STATE_DIR}/namespace-prefix"
export PYTHONPATH="$BRIDGE_DIR${PYTHONPATH:+:$PYTHONPATH}"
LOG_DIR="$POD_STATE_DIR/logs"
PID_DIR="$POD_STATE_DIR/.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# ---------------------------------------------------------------------------
# Venv setup
# ---------------------------------------------------------------------------
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "Creating venv at $VENV …"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet -r "$BRIDGE_DIR/requirements.txt"
    echo "Venv ready."
fi
PYTHON="$VENV/bin/python3"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
is_bridge_pid() {
    local pid="$1" expected_script="$2" arg
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && [[ -r "/proc/$pid/cmdline" ]] || return 1
    while IFS= read -r -d '' arg; do
        [[ "$arg" == "$BRIDGE_DIR/$expected_script" ]] && return 0
    done < "/proc/$pid/cmdline"
    return 1
}

start() {
    local name="$1"; shift
    local script="$1"; shift
    local pid_file="$PID_DIR/$name.pid"

    if [[ -f "$pid_file" ]]; then
        IFS= read -r pid < "$pid_file"
        if is_bridge_pid "$pid" "$script"; then
            echo "  [skip] $name already running (pid $pid)"
            return
        fi
        rm -f "$pid_file"
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
    start dronuradaras bridges/dronuradaras_bridge.py

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

    if [[ -z "${COPERNICUSMARINE_SERVICE_USERNAME:-}" || \
          -z "${COPERNICUSMARINE_SERVICE_PASSWORD:-}" ]]; then
        echo "  [skip] cmems — Copernicus Marine credentials not set"
    elif ! "$PYTHON" -c 'import copernicusmarine' >/dev/null 2>&1; then
        echo "  [skip] cmems — optional package missing; run: $VENV/bin/pip install copernicusmarine"
    else
        start cmems bridges/cmems_marine_bridge.py
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

    # Optional direct UDP-unicast CoT output for a WinTAK/ATAK client whose
    # matching UDP input is reachable on the LAN/VPN. Keep this separate from
    # TAK_HOST/TAK_PORT, which describe a TAK Server TCP stream.
    if [[ "${TAK_UDP_HOST:-}" || "${TAK_UDP_HOST_FALLBACK:-}" ]]; then
        _tak_udp_hosts=()
        [[ "${TAK_UDP_HOST:-}" ]] && _tak_udp_hosts+=(--host "$TAK_UDP_HOST")
        [[ "${TAK_UDP_HOST_FALLBACK:-}" ]] && \
            _tak_udp_hosts+=(--host "$TAK_UDP_HOST_FALLBACK")
        start cot-udp-tak layers/cot_layer.py --udp "${_tak_udp_hosts[@]}" \
            --port "${TAK_UDP_PORT:-8087}"
    else
        echo "  [skip] cot-udp-tak — set TAK_UDP_HOST to enable direct client output"
    fi

    # CoT → TAK Server TCP (Option A: plaintext :8087, Option B: mTLS :8089)
    if [[ "${TAK_HOST:-127.0.0.1}" != "127.0.0.1" ]] || \
       nc -z "${TAK_HOST:-127.0.0.1}" "${TAK_PORT:-8087}" 2>/dev/null; then
        _cot_args=(--host "${TAK_HOST:-127.0.0.1}" --port "${TAK_PORT:-8087}")
        if [[ "${TAK_TLS:-}" == "1" ]]; then
            _cot_args+=(--tls --cert "${TAK_CERT}" --key "${TAK_KEY}" --ca "${TAK_CA}")
        fi
        start cot-tcp layers/cot_layer.py "${_cot_args[@]}"
    else
        echo "  [skip] cot-tcp — TAK Server not reachable at ${TAK_HOST:-127.0.0.1}:${TAK_PORT:-8087}"
    fi

    # Unified ASTERIX bridge (CAT-048/34 + CAT-021 + CAT-020 + CAT-062)
    _ax=()
    if [[ "${CAT48_PORT:-}" ]]; then
        _ax+=(--cat48-port "$CAT48_PORT")
        [[ "${CAT48_TCP:-}" == "1" ]]    && _ax+=(--cat48-tcp)
        [[ "${CAT48_RADAR_LAT:-}" ]]     && _ax+=(--radar-lat "$CAT48_RADAR_LAT")
        [[ "${CAT48_RADAR_LON:-}" ]]     && _ax+=(--radar-lon "$CAT48_RADAR_LON")
        [[ "${CAT48_RADAR_NAME:-}" ]]    && _ax+=(--radar-name "$CAT48_RADAR_NAME")
    fi
    if [[ "${CAT21_PORT:-}" ]]; then
        _ax+=(--cat21-port "$CAT21_PORT")
        [[ "${CAT21_TCP:-}" == "1" ]]    && _ax+=(--cat21-tcp)
    fi
    if [[ "${CAT20_PORT:-}" ]]; then
        _ax+=(--cat20-port "$CAT20_PORT")
        [[ "${CAT20_TCP:-}" == "1" ]]    && _ax+=(--cat20-tcp)
    fi
    if [[ "${RADAR_HOST:-}" && "${RADAR_HOST}" != "127.0.0.1" ]]; then
        _ax+=(--cat62-host "$RADAR_HOST" --cat62-port "${RADAR_PORT:-30002}")
    fi
    if [[ ${#_ax[@]} -gt 0 ]]; then
        start asterix bridges/asterix_bridge.py "${_ax[@]}"
    else
        echo "  [skip] asterix — set CAT48/CAT21/CAT20_PORT or RADAR_HOST in .env to enable"
    fi

    # SitaWare — REST poll for unit positions (friendly force tracking)
    if [[ "${SITAWARE_URL:-}" ]]; then
        sitaware_flags=""
        [[ "${SITAWARE_DISCOVER:-}" == "1" ]] && sitaware_flags="--discover"
        start sitaware bridges/sitaware_bridge.py ${sitaware_flags:+"$sitaware_flags"}
    else
        echo "  [skip] sitaware — set SITAWARE_URL in .env to enable"
    fi

    # SitaWare Edge NVG is a distinct outbound adapter with separate product,
    # credentials, direction, and lifecycle from the HQ inbound REST poller.
    if [[ "${SITAWARE_NVG_URL:-}" && "${SITAWARE_NVG_USER:-}" ]]; then
        start sitaware-nvg layers/nato_nvg_layer.py
    else
        echo "  [skip] sitaware-nvg — set SITAWARE_NVG_URL and SITAWARE_NVG_USER to enable"
    fi

    # SitaWare HQ NVG Import Subscription pulls a complete NVG snapshot from
    # this native HTTP(S) feed. This is separate from the Edge REST adapter.
    if [[ "${SITAWARE_HQ_NVG_ENABLE:-}" == "1" ]]; then
        start sitaware-hq-nvg layers/sitaware_hq_nvg_feed.py
    else
        echo "  [skip] sitaware-hq-nvg — set SITAWARE_HQ_NVG_ENABLE=1 to enable"
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

    # MAVLink and VMF tactical sensor inputs.
    if [[ "${MAVLINK_PORT:-}" ]]; then
        mav_args=()
        [[ "${MAVLINK_TCP:-}" == "1" ]] && mav_args+=(--tcp)
        start mavlink bridges/mavlink_bridge.py --port "$MAVLINK_PORT" "${mav_args[@]}"
    else
        echo "  [skip] mavlink — set MAVLINK_PORT in .env to enable"
    fi

    if [[ "${VMF_PORT:-}" ]]; then
        vmf_args=()
        [[ "${VMF_TCP:-}" == "1" ]] && vmf_args+=(--tcp)
        start vmf bridges/vmf_bridge.py --port "$VMF_PORT" "${vmf_args[@]}"
    else
        echo "  [skip] vmf — set VMF_PORT in .env to enable"
    fi

    # Link 16 JREAP-C — inbound UDP listener. TCP is intentionally unavailable
    # until the attached gateway's stream framing ICD is known.
    if [[ "${LINK16_PORT:-}" ]]; then
        start link16 bridges/link16_bridge.py --port "$LINK16_PORT"
    else
        echo "  [skip] link16 — set LINK16_PORT in .env to enable"
    fi

    # Output/correlation layers that consume the combined native bridge feed.
    start track-fusion layers/track_fusion_layer.py

    if [[ "${STANAG4586_HOST:-}" ]]; then
        start stanag4586 layers/stanag4586_layer.py \
            --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
    else
        echo "  [skip] stanag4586 — set STANAG4586_HOST in .env to enable"
    fi
}

# ---------------------------------------------------------------------------
# Giraffe-only subset: ASTERIX + Link-16 inbound bridges, then CoT-UDP out
# ---------------------------------------------------------------------------
start_giraffe_bridges() {
    echo ""
    echo "=== Giraffe sensor bridges ==="

    # Unified ASTERIX bridge (CAT-048/34 + CAT-021 + CAT-020)
    _ax=()
    if [[ "${CAT48_PORT:-}" ]]; then
        _ax+=(--cat48-port "$CAT48_PORT")
        [[ "${CAT48_TCP:-}" == "1" ]]    && _ax+=(--cat48-tcp)
        [[ "${CAT48_RADAR_LAT:-}" ]]     && _ax+=(--radar-lat "$CAT48_RADAR_LAT")
        [[ "${CAT48_RADAR_LON:-}" ]]     && _ax+=(--radar-lon "$CAT48_RADAR_LON")
        [[ "${CAT48_RADAR_NAME:-}" ]]    && _ax+=(--radar-name "$CAT48_RADAR_NAME")
    fi
    if [[ "${CAT21_PORT:-}" ]]; then
        _ax+=(--cat21-port "$CAT21_PORT")
        [[ "${CAT21_TCP:-}" == "1" ]]    && _ax+=(--cat21-tcp)
    fi
    if [[ "${CAT20_PORT:-}" ]]; then
        _ax+=(--cat20-port "$CAT20_PORT")
        [[ "${CAT20_TCP:-}" == "1" ]]    && _ax+=(--cat20-tcp)
    fi
    if [[ ${#_ax[@]} -gt 0 ]]; then
        start asterix bridges/asterix_bridge.py "${_ax[@]}"
    else
        echo "  [skip] asterix — set CAT48/CAT21/CAT20_PORT in .env to enable"
    fi

    if [[ "${LINK16_PORT:-}" ]]; then
        start link16 bridges/link16_bridge.py --port "$LINK16_PORT"
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

    # CoT → TAK Server TCP (Option A: plaintext :8087, Option B: mTLS :8089)
    if [[ "${TAK_HOST:-127.0.0.1}" != "127.0.0.1" ]] || \
       nc -z "${TAK_HOST:-127.0.0.1}" "${TAK_PORT:-8087}" 2>/dev/null; then
        _cot_args=(--host "${TAK_HOST:-127.0.0.1}" --port "${TAK_PORT:-8087}")
        if [[ "${TAK_TLS:-}" == "1" ]]; then
            _cot_args+=(--tls --cert "${TAK_CERT}" --key "${TAK_KEY}" --ca "${TAK_CA}")
        fi
        start cot-tcp layers/cot_layer.py "${_cot_args[@]}"
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
