#!/usr/bin/env bash
# run.sh — start EFDI bridges as background scripts (no Docker required).
# The Zenoh router is still started via Docker (single container, compiled binary).
#
# Usage:
#   ./run.sh            # giraffe mode (default) — radar/Link-16 sensors + CoT-UDP only
#   ./run.sh giraffe    # same as above — CAT-48/21/20, Link-16, cot-udp
#   ./run.sh all        # everything — bridges, input protocols, and output layers
#   ./run.sh bridges    # source-specific bridges only (skip zenoh + layers)
#   ./run.sh protocols  # reusable input protocols only
#   ./run.sh layers     # TAK and SitaWare output/pointer layers only
#
# Logs go to logs/<name>.log  PIDs saved to .pids/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/compose"
ENV_FILE="$SCRIPT_DIR/compose/.env"
VENV="$COMPOSE_DIR/venv"

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
export PYTHONPATH="$COMPOSE_DIR${PYTHONPATH:+:$PYTHONPATH}"
LOG_DIR="$POD_STATE_DIR/logs"
PID_DIR="$POD_STATE_DIR/.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# ---------------------------------------------------------------------------
# Venv setup
# ---------------------------------------------------------------------------
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "Creating venv at $VENV …"
    python3 -m venv "$VENV"
    "$VENV/bin/python3" -m pip install --quiet -r "$COMPOSE_DIR/requirements.txt"
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
        [[ "$arg" == "$COMPOSE_DIR/$expected_script" ]] && return 0
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

    if [[ "$name" == "admin-control" ]]; then
        ( exec setsid "$PYTHON" "$COMPOSE_DIR/$script" "$@" \
            >> "$LOG_DIR/$name.log" 2>&1 ) &
    else
        "$PYTHON" "$COMPOSE_DIR/$script" "$@" \
            >> "$LOG_DIR/$name.log" 2>&1 &
    fi
    echo $! > "$pid_file"
    echo "  [start] $name  (pid $!)"
}

skip_if_no_key() {
    local key_val="${!1:-}"
    [[ -z "$key_val" ]]
}

asterix_category_uses_raw() {
    local wanted="$1" item
    [[ -n "${ASTERIX_PORT:-}" ]] || return 1
    IFS=',' read -r -a _asterix_categories <<< "${ASTERIX_CATEGORIES:-34,48}"
    for item in "${_asterix_categories[@]}"; do
        item="${item//[[:space:]]/}"
        [[ "$item" == "$wanted" ]] && return 0
    done
    return 1
}

start_asterix_udp_bridge() {
    if [[ "${ASTERIX_PORT:-}" ]]; then
        start asterix-udp bridges/asterix_udp_bridge.py
    else
        echo "  [skip] asterix-udp — set ASTERIX_PORT for a mixed UDP feed"
    fi
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
                break
            fi
            echo -n "."
        done
        echo " timeout — continuing anyway"
    fi
    start admin-control admin_control.py
}

start_asterix_protocols() {
    local category port tcp_var tcp_args host
    for category in 10 20 21 34 48; do
        case "$category" in
            10) port="${CAT10_PORT:-}"; tcp_var="${CAT10_TCP:-}" ;;
            20) port="${CAT20_PORT:-}"; tcp_var="${CAT20_TCP:-}" ;;
            21) port="${CAT21_PORT:-}"; tcp_var="${CAT21_TCP:-}" ;;
            34) port="${CAT34_PORT:-}"; tcp_var="${CAT34_TCP:-}" ;;
            48) port="${CAT48_PORT:-}"; tcp_var="${CAT48_TCP:-}" ;;
        esac
        if asterix_category_uses_raw "$category"; then
            start "asterix-cat${category}" "protocols/asterix_cat${category}.py" --zenoh-raw
        elif [[ "$port" ]]; then
            tcp_args=(); [[ "$tcp_var" == "1" ]] && tcp_args=(--tcp)
            start "asterix-cat${category}" "protocols/asterix_cat${category}.py" \
                --port "$port" "${tcp_args[@]}"
        else
            echo "  [skip] asterix-cat${category} — set CAT${category}_PORT in .env to enable"
        fi
    done

    host="${CAT62_HOST:-${RADAR_HOST:-}}"
    if asterix_category_uses_raw 62; then
        start asterix-cat62 protocols/asterix_cat62.py --zenoh-raw
    elif [[ "$host" && "$host" != "127.0.0.1" ]]; then
        start asterix-cat62 protocols/asterix_cat62.py --host "$host" \
            --port "${CAT62_PORT:-${RADAR_PORT:-50062}}"
    elif [[ "${CAT62_UDP:-}" == "1" ]]; then
        start asterix-cat62 protocols/asterix_cat62.py --udp --port "${CAT62_PORT:-50062}"
    else
        echo "  [skip] asterix-cat62 — set CAT62_HOST or CAT62_UDP=1 in .env to enable"
    fi
}

# ---------------------------------------------------------------------------
# Source bridges (external data → Zenoh)
# ---------------------------------------------------------------------------
start_bridges() {
    echo ""
    echo "=== Source bridges ==="

    start_asterix_udp_bridge

    start airplaneslive bridges/airplaneslive_adsb_bridge.py
    start adsblol bridges/adsblol_bridge.py
    start dronuradaras bridges/dronuradaras_bridge.py

    if [[ "${DJI_MQTT_HOST:-}" ]]; then
        start dji-cloud bridges/dji_cloud_api_bridge.py
    else
        echo "  [skip] dji-cloud — set DJI_MQTT_HOST in .env to enable"
    fi

    if [[ "${UTM_ANS_API_URL:-}" ]]; then
        start utm-ans bridges/utm_ans_bridge.py
    else
        echo "  [skip] utm-ans — set UTM_ANS_API_URL to an authorized JSON/GeoJSON feed"
    fi

    if skip_if_no_key AISSTREAM_KEY; then
        echo "  [skip] aisstream — AISSTREAM_KEY not set"
    else
        start aisstream bridges/aisstream_ws_bridge.py
    fi

    start aprs bridges/aprsis_bridge.py

    start openmeteo bridges/openmeteo_forecast_bridge.py
    start meteolt bridges/meteolt_forecast_bridge.py

    if [[ "${SITAWARE_URL:-}" ]]; then
        if [[ -z "${SITAWARE_API_PATH:-}" && "${SITAWARE_DISCOVER:-}" != "1" ]]; then
            echo "  [skip] sitaware — set SITAWARE_API_PATH or SITAWARE_DISCOVER=1"
        else
        sitaware_flags=""
        [[ "${SITAWARE_DISCOVER:-}" == "1" ]] && sitaware_flags="--discover"
        start sitaware bridges/sitaware_bridge.py ${sitaware_flags:+"$sitaware_flags"}
        fi
    else
        echo "  [skip] sitaware — set SITAWARE_URL in .env to enable"
    fi

    start track-fusion bridges/track_fusion_bridge.py

    # Optional transport-only ingress.  These processes publish bytes; the
    # matching protocol translators below perform all decoding.
    [[ "${MAVLINK_RAW_PORT:-}" ]] && start mavlink-raw bridges/mavlink_raw_bridge.py --port "$MAVLINK_RAW_PORT" || true
    [[ "${LINK16_RAW_PORT:-}" ]] && start link16-raw bridges/link16_jreap_bridge.py --port "$LINK16_RAW_PORT" || true
    [[ "${VMF_RAW_PORT:-}" ]] && start vmf-raw bridges/vmf_bridge.py --port "$VMF_RAW_PORT" || true
    [[ "${SAPIENT_RAW_PORT:-}" ]] && start sapient-raw bridges/sapient_flex335_bridge.py --tcp --port "${SAPIENT_RAW_PORT:-7001}" || true
    [[ "${STANAG4586_RAW_PORT:-}" ]] && start stanag4586-raw bridges/stanag4586_bridge.py --tcp --port "${STANAG4586_RAW_PORT:-4586}" || true
}

# ---------------------------------------------------------------------------
# Reusable input protocols (external wire/API contract → Zenoh)
# ---------------------------------------------------------------------------
start_protocols() {
    echo ""
    echo "=== Input protocols ==="

    start_asterix_protocols

    # Receiver/detection nodes publish raw ASTM/ASD-STAN messages into Zenoh.
    # This idle-safe translator runs on the data plane and needs no radio.
    start opendroneid protocols/opendroneid.py

    start cap protocols/cap.py
    start geojson protocols/geojson_features.py
    start ais-nmea protocols/ais_nmea.py
    start spectrum protocols/spectrum_observation.py
    start sensor-health protocols/sensor_health.py
    start mission-route protocols/mission_route.py

    if [[ "${SAPIENT_ZENOH_RAW:-}" == "1" ]]; then
        start sapient protocols/sapient_flex335.py --zenoh-raw --raw-topic "${SAPIENT_RAW_TOPIC:-}"
    elif [[ "${SAPIENT_LISTEN_PORT:-}" ]]; then
        sapient_args=(--listen "$SAPIENT_LISTEN_PORT" --bind "${SAPIENT_BIND:-127.0.0.1}")
        [[ "${SAPIENT_ALLOW_PEER:-}" ]] && sapient_args+=(--allow-peer "$SAPIENT_ALLOW_PEER")
        start sapient protocols/sapient_flex335.py "${sapient_args[@]}"
    elif [[ "${SAPIENT_HOST:-}" ]]; then
        start sapient protocols/sapient_flex335.py --host "$SAPIENT_HOST" --port "${SAPIENT_PORT:-7001}"
    else
        start sapient protocols/sapient_flex335.py --zenoh-raw --raw-topic "${SAPIENT_RAW_TOPIC:-}"
    fi

    start nffi protocols/nffi.py

    if [[ "${MAVLINK_ZENOH_RAW:-}" == "1" ]]; then
        start mavlink protocols/mavlink.py --zenoh-raw --raw-topic "${MAVLINK_RAW_TOPIC:-}"
    elif [[ "${MAVLINK_PORT:-}" ]]; then
        mav_args=(); [[ "${MAVLINK_TCP:-}" == "1" ]] && mav_args=(--tcp)
        start mavlink protocols/mavlink.py --port "$MAVLINK_PORT" "${mav_args[@]}"
    else
        echo "  [skip] mavlink — set MAVLINK_PORT in .env to enable"
    fi

    if [[ "${VMF_ZENOH_RAW:-}" == "1" ]]; then
        start vmf protocols/vmf.py --zenoh-raw --raw-topic "${VMF_RAW_TOPIC:-}"
    elif [[ "${VMF_PORT:-}" ]]; then
        vmf_args=(); [[ "${VMF_TCP:-}" == "1" ]] && vmf_args=(--tcp)
        start vmf protocols/vmf.py --port "$VMF_PORT" "${vmf_args[@]}"
    else
        echo "  [skip] vmf — set VMF_PORT in .env to enable"
    fi

    if [[ "${LINK16_ZENOH_RAW:-}" == "1" ]]; then
        start link16 protocols/link16.py --zenoh-raw --raw-topic "${LINK16_RAW_TOPIC:-}"
    elif [[ "${LINK16_PORT:-}" ]]; then
        start link16 protocols/link16.py --port "$LINK16_PORT"
    else
        echo "  [skip] link16 — set LINK16_PORT in .env to enable"
    fi

    if [[ "${STANAG4586_ZENOH_RAW:-}" == "1" ]]; then
        start stanag4586 protocols/stanag4586.py --zenoh-raw --raw-topic "${STANAG4586_RAW_TOPIC:-}"
    elif [[ "${STANAG4586_HOST:-}" ]]; then
        start stanag4586 protocols/stanag4586.py \
            --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
    else
        echo "  [skip] stanag4586 — set STANAG4586_HOST in .env to enable"
    fi
}

# ---------------------------------------------------------------------------
# TAK and SitaWare pointer/output layers
# ---------------------------------------------------------------------------
start_layers() {
    echo ""
    echo "=== TAK and SitaWare layers ==="

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

    # SitaWare HQ NVG Import Subscription pulls a complete NVG snapshot from
    # this native HTTP(S) feed. This is separate from the Edge REST adapter.
    if [[ "${SITAWARE_HQ_NVG_ENABLE:-}" == "1" ]]; then
        start sitaware-hq-nvg bridges/nvg_bridge.py
    else
        echo "  [skip] sitaware-hq-nvg — set SITAWARE_HQ_NVG_ENABLE=1 to enable"
    fi

    # TAK Server receiver — direct CoT user-SA stream over TCP/TLS.
    # TLS credentials and TAK-user filtering are read from COT_RX_* environment variables.
    # Set COT_RX_PORT to open a listener (they connect to us)
    # Set COT_RX_HOST to connect outbound (we connect to them, format IP:PORT)
    if [[ "${COT_RX_PORT:-}" ]]; then
        start cot-rx bridges/tak_bridge.py --listen "$COT_RX_PORT" --source tak_rx
    elif [[ "${COT_RX_HOST:-}" ]]; then
        start cot-rx bridges/tak_bridge.py --connect "$COT_RX_HOST" --source tak_rx
    else
        echo "  [skip] cot-rx — set COT_RX_PORT or COT_RX_HOST in .env to enable"
    fi

}

# ---------------------------------------------------------------------------
# Giraffe-only subset: ASTERIX + Link-16 inbound bridges, then CoT-UDP out
# ---------------------------------------------------------------------------
start_giraffe_bridges() {
    echo ""
    echo "=== Giraffe sensor bridges ==="

    start_asterix_udp_bridge
    start_asterix_protocols

    if [[ "${LINK16_PORT:-}" ]]; then
        start link16 protocols/link16.py --port "$LINK16_PORT"
    else
        echo "  [skip] link16 — set LINK16_PORT in .env to enable"
    fi

    if [[ "${MAVLINK_PORT:-}" ]]; then
        tcp_mav=""
        [[ "${MAVLINK_TCP:-}" == "1" ]] && tcp_mav="--tcp"
        start mavlink protocols/mavlink.py --port "$MAVLINK_PORT" ${tcp_mav:+"$tcp_mav"}
    else
        echo "  [skip] mavlink — set MAVLINK_PORT in .env to enable"
    fi

    if [[ "${DJI_MQTT_HOST:-}" ]]; then
        start dji-cloud bridges/dji_cloud_api_bridge.py
    else
        echo "  [skip] dji-cloud — set DJI_MQTT_HOST in .env to enable"
    fi

    if [[ "${VMF_PORT:-}" ]]; then
        tcp_vmf=""
        [[ "${VMF_TCP:-}" == "1" ]] && tcp_vmf="--tcp"
        start vmf protocols/vmf.py --port "$VMF_PORT" ${tcp_vmf:+"$tcp_vmf"}
    else
        echo "  [skip] vmf — set VMF_PORT in .env to enable"
    fi

    if [[ "${COT_RX_PORT:-}" ]]; then
        start cot-rx bridges/tak_bridge.py --listen "$COT_RX_PORT" --source tak_rx
    elif [[ "${COT_RX_HOST:-}" ]]; then
        start cot-rx bridges/tak_bridge.py --connect "$COT_RX_HOST" --source tak_rx
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
    start track-fusion bridges/track_fusion_bridge.py

    # STANAG 4586 UAS interface — only if VSM host is configured
    if [[ "${STANAG4586_HOST:-}" ]]; then
        start stanag4586 protocols/stanag4586.py --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
    else
        start stanag4586 protocols/stanag4586.py --zenoh-raw
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
        start_protocols
        start_layers
        ;;
    bridges)
        start_bridges
        ;;
    protocols)
        start_protocols
        ;;
    layers)
        start_layers
        ;;
    *)
        echo "Usage: $0 [giraffe|all|bridges|protocols|layers]"
        exit 1
        ;;
esac

echo ""
echo "Logs: $LOG_DIR/"
echo "Stop: ./stop.sh"
