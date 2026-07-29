#!/usr/bin/env bash
# run.sh — start EFDI bridges as background scripts (no Docker required).
# The Zenoh router is still started via Docker (single container, compiled binary).
#
# Usage:
#   ./run.sh            # giraffe mode (default) — radar sensors + CoT-UDP only
#   ./run.sh giraffe    # same as above — ASTERIX CAT categories + cot-udp
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
export EFDI_CERT_DIR="${EFDI_CERT_DIR:-$BUNDLE_DIR/efdi}"
export POD_STATE_DIR="${POD_STATE_DIR:-$SCRIPT_DIR/compose/state}"
export NAMESPACE_PREFIX_FILE="${POD_STATE_DIR}/namespace-prefix"
export DATA_NAMESPACE_PREFIX_FILE="${POD_STATE_DIR}/data-topic-prefix"
export PYTHONPATH="$COMPOSE_DIR/generated:$COMPOSE_DIR/generated/protocols:$COMPOSE_DIR${PYTHONPATH:+:$PYTHONPATH}"
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
if ! "$PYTHON" -c 'import grpc_tools.protoc, google.protobuf, zenoh, defusedxml' 2>/dev/null; then
    "$PYTHON" -m pip install --quiet -r "$COMPOSE_DIR/requirements.txt"
fi
EFDI_PROTOC_PYTHON="$PYTHON" "$SCRIPT_DIR/scripts/generate-protobuf.sh" >/dev/null

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
is_bridge_pid() {
    local pid="$1" expected_script="$2" arg
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && [[ -r "/proc/$pid/cmdline" ]] || return 1
    local efdi_process=1
    while IFS= read -r -d '' arg; do
        [[ "$arg" == "$COMPOSE_DIR/$expected_script" || "$arg" == "$SCRIPT_DIR/$expected_script" ]] && return 0
        [[ "$arg" == "$COMPOSE_DIR/"* ]] && efdi_process=0
    done < "/proc/$pid/cmdline"
    # Keep a live PID-file process authoritative across implementation-path
    # changes so run.sh cannot launch a second subscriber for the same service.
    [[ "$efdi_process" == "0" ]]
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
        if [[ -z "${ZENOH_ADMIN_SECRET_KEY:-}" && -z "${EFDI_CONTROL_TOKEN:-}" ]]; then
            echo "  [skip] admin-control requires ZENOH_ADMIN_SECRET_KEY or EFDI_CONTROL_TOKEN"
            return 1
        fi
        ( exec setsid "$PYTHON" "$COMPOSE_DIR/$script" "$@" \
            >> "$LOG_DIR/$name.log" 2>&1 ) &
    else
        ( exec setsid "$PYTHON" "$COMPOSE_DIR/$script" "$@" \
            >> "$LOG_DIR/$name.log" 2>&1 ) &
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
    [[ -n "${UDP_INGRESS_PORT:-${ASTERIX_PORT:-}}" ]] || return 1
    IFS=',' read -r -a _asterix_categories <<< "${ASTERIX_CATEGORIES:-34,48}"
    for item in "${_asterix_categories[@]}"; do
        item="${item//[[:space:]]/}"
        [[ "$item" == "$wanted" ]] && return 0
    done
    return 1
}

start_udp_ingress_bridge() {
    if [[ "${UDP_INGRESS_PORT:-${ASTERIX_PORT:-}}" ]]; then
        start udp-ingress bridges/udp_ingress_bridge.py
    else
        echo "  [skip] udp-ingress — set UDP_INGRESS_PORT for generic UDP capture"
    fi
}

# ---------------------------------------------------------------------------
# Zenoh router — one Docker container, everything else is native
# ---------------------------------------------------------------------------
start_zenoh() {
    if [[ -f "${POD_STATE_DIR}/pki/step-ca/config/ca.json" ]]; then
        mesh_ip=$(netbird status 2>/dev/null | sed -n 's/.*NetBird IP:[[:space:]]*\([0-9.]*\).*/\1/p' | head -1 || true)
        if [[ "$mesh_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            "$SCRIPT_DIR/scripts/pki/configure-step-ca-names.sh" \
                "${POD_STATE_DIR}/pki/step-ca" "$mesh_ip" >/dev/null
        fi
        docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" --profile managed-ca up -d step-ca
    fi
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
    if [[ -n "${EFDI_STEP_CA_URL:-}" ]]; then
        renew_cert="${EFDI_STEP_RENEW_CERT_PATH:-${EFDI_CERT_DIR}/${PARTNER_NAMESPACE}-cert.pem}"
        renew_key="${EFDI_STEP_RENEW_KEY_PATH:-${EFDI_CERT_DIR}/${PARTNER_NAMESPACE}-key.pem}"
        renew_root="${EFDI_STEP_RENEW_ROOT_PATH:-${EFDI_CERT_DIR}/efdi-ca-root.pem}"
        if [[ -f "$renew_cert" && -f "$renew_key" && -f "$renew_root" ]]; then
            export EFDI_STEP_RENEW_RUNTIME_CERT_PATH="${EFDI_STEP_RENEW_RUNTIME_CERT_PATH:-${POD_STATE_DIR}/zenoh/tls/pod-cert.pem}"
            if [[ ! -f "$PID_DIR/cert-renewer.pid" ]] || ! is_bridge_pid "$(cat "$PID_DIR/cert-renewer.pid" 2>/dev/null)" "scripts/pki/renew-step-identities.sh"; then
                ( exec setsid "$SCRIPT_DIR/scripts/pki/renew-step-identities.sh" --daemon \
                    "$EFDI_STEP_CA_URL" "$renew_root" "$renew_cert:$renew_key" \
                    >> "$LOG_DIR/cert-renewer.log" 2>&1 ) &
                echo $! > "$PID_DIR/cert-renewer.pid"
                echo "  [start] cert-renewer  (pid $!)"
            fi
        fi
    fi
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
            start "asterix-cat${category}-raw" protocols/vendors/asterix/cat.py \
                --category "$category" --zenoh-raw
        fi
        if [[ "$port" ]]; then
            tcp_args=(); [[ "$tcp_var" == "1" ]] && tcp_args=(--tcp)
            start "asterix-cat${category}" protocols/vendors/asterix/cat.py \
                --category "$category" --port "$port" "${tcp_args[@]}"
        elif ! asterix_category_uses_raw "$category"; then
            echo "  [skip] asterix-cat${category} — set CAT${category}_PORT in .env to enable"
        fi
        if [[ ! "$port" ]] && asterix_category_uses_raw "$category"; then
            echo "  [skip] asterix-cat${category} direct listener — set CAT${category}_PORT to enable"
        fi
    done

    host="${CAT62_HOST:-${RADAR_HOST:-}}"
    if asterix_category_uses_raw 62; then
        start asterix-cat62-raw protocols/vendors/asterix/cat.py --category 62 --zenoh-raw
    fi
    if [[ "$host" && "$host" != "127.0.0.1" ]]; then
        start asterix-cat62 protocols/vendors/asterix/cat.py --category 62 --host "$host" \
            --port "${CAT62_PORT:-${RADAR_PORT:-50062}}"
    elif [[ "${CAT62_UDP:-}" == "1" ]]; then
        start asterix-cat62 protocols/vendors/asterix/cat.py --category 62 --udp --port "${CAT62_PORT:-50062}"
    elif ! asterix_category_uses_raw 62; then
        echo "  [skip] asterix-cat62 — set CAT62_HOST or CAT62_UDP=1 in .env to enable"
    fi
}

# ---------------------------------------------------------------------------
# Source bridges (external data → Zenoh)
# ---------------------------------------------------------------------------
start_bridges() {
    echo ""
    echo "=== Source bridges ==="

    start_udp_ingress_bridge

    start dronuradaras bridges/dronuradaras_bridge.py

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
    if [[ "${SAPIENT_RAW_PORT:-}" ]]; then
        start sapient-raw bridges/sapient_flex335_bridge.py --tcp --port "$SAPIENT_RAW_PORT"
    fi
    if [[ "${STANAG4586_RAW_PORT:-}" ]]; then
        start stanag4586-raw bridges/4586_bridge.py --tcp --port "$STANAG4586_RAW_PORT"
    fi
}

# ---------------------------------------------------------------------------
# Reusable input protocols (external wire/API contract → Zenoh)
# ---------------------------------------------------------------------------
start_protocols() {
    echo ""
    echo "=== Input protocols ==="

    start_asterix_protocols

    start cap protocols/random/cap.py
    start geojson protocols/random/geojson_features.py
    start mqtt protocols/random/mqtt_json.py
    start sensorthings protocols/random/sensorthings.py
    start sparkplug protocols/vendors/sparkplug/sparkplug.py
    start spectrum protocols/random/spectrum_observation.py
    start sensor-health protocols/random/sensor_health.py
    start mission-route protocols/random/mission_route.py

    if [[ "${SAPIENT_ZENOH_RAW:-}" == "1" ]]; then
        start sapient protocols/vendors/sapient/flex335.py --zenoh-raw --raw-topic "${SAPIENT_RAW_TOPIC:-}"
    elif [[ "${SAPIENT_LISTEN_PORT:-}" ]]; then
        sapient_args=(--listen "$SAPIENT_LISTEN_PORT" --bind "${SAPIENT_BIND:-127.0.0.1}")
        [[ "${SAPIENT_ALLOW_PEER:-}" ]] && sapient_args+=(--allow-peer "$SAPIENT_ALLOW_PEER")
        start sapient protocols/vendors/sapient/flex335.py "${sapient_args[@]}"
    elif [[ "${SAPIENT_HOST:-}" ]]; then
        start sapient protocols/vendors/sapient/flex335.py --host "$SAPIENT_HOST" --port "${SAPIENT_PORT:-7001}"
    else
        start sapient protocols/vendors/sapient/flex335.py --zenoh-raw --raw-topic "${SAPIENT_RAW_TOPIC:-}"
    fi

    start nffi protocols/random/nffi.py

    if [[ "${STANAG4586_PROFILE:-}" == "legacy_ed3_approx" && "${STANAG4586_ZENOH_RAW:-}" == "1" ]]; then
        start stanag4586 protocols/vendors/stanag/4586.py --zenoh-raw --raw-topic "${STANAG4586_RAW_TOPIC:-}"
    elif [[ "${STANAG4586_PROFILE:-}" == "legacy_ed3_approx" && "${STANAG4586_HOST:-}" ]]; then
        start stanag4586 protocols/vendors/stanag/4586.py \
            --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
    else
        echo "  [skip] stanag4586 — validate the VSM ICD, then set STANAG4586_PROFILE=legacy_ed3_approx and a source"
    fi

    if [[ "${STANAG4609_SRT_URL:-}" ]]; then
        start stanag4609 protocols/vendors/stanag/4609.py --zenoh-raw
    else
        echo "  [skip] stanag4609 — set STANAG4609_SRT_URL in .env to enable"
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
        start cot-bridge layers/cot_layer.py "${_cot_args[@]}"
    else
        echo "  [skip] cot-bridge — TAK Server not reachable at ${TAK_HOST:-127.0.0.1}:${TAK_PORT:-8087}"
    fi

    # SitaWare HQ NVG Import Subscription pulls a complete NVG snapshot from
    # this native HTTP(S) feed. This is separate from the Edge REST adapter.
    if [[ "${SITAWARE_HQ_NVG_ENABLE:-}" == "1" ]]; then
        start sitaware-hq-nvg bridges/nvg_bridge.py
    else
        echo "  [skip] sitaware-hq-nvg — set SITAWARE_HQ_NVG_ENABLE=1 to enable"
    fi

}

# ---------------------------------------------------------------------------
# Giraffe-only subset: ASTERIX inbound bridges, then CoT-UDP out
# ---------------------------------------------------------------------------
start_giraffe_bridges() {
    echo ""
    echo "=== Giraffe sensor bridges ==="

    start_udp_ingress_bridge
    start_asterix_protocols

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
        start cot-bridge layers/cot_layer.py "${_cot_args[@]}"
    else
        echo "  [skip] cot-bridge — TAK Server not reachable at ${TAK_HOST:-127.0.0.1}:${TAK_PORT:-8087}"
    fi

    # Track fusion — always start in giraffe mode (correlates radar + any ADS-B)
    start track-fusion bridges/track_fusion_bridge.py

    # STANAG 4586 UAS interface — only if VSM host is configured
    if [[ "${STANAG4586_PROFILE:-}" == "legacy_ed3_approx" && "${STANAG4586_HOST:-}" ]]; then
        start stanag4586 protocols/vendors/stanag/4586.py --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
    else
        start stanag4586 protocols/vendors/stanag/4586.py --zenoh-raw
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
