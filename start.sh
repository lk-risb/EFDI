#!/usr/bin/env bash
# start.sh — interactive EFDI service launcher
# Usage: ./start.sh [--restore]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/compose"
ENV_FILE="$SCRIPT_DIR/compose/.env"
VENV="$COMPOSE_DIR/venv"

# ── Load .env (safe — no shell re-parsing of values) ──────────────────────
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

if [[ -z "${EFDI_CONTROL_TOKEN:-}" && -n "${ZENOH_ADMIN_SECRET_KEY:-}" ]]; then
    EFDI_CONTROL_TOKEN="$(printf 'efdi-control-v1:%s' "$ZENOH_ADMIN_SECRET_KEY" | sha256sum | awk '{print $1}')"
    export EFDI_CONTROL_TOKEN
fi

export ZENOH_LOCAL_ENDPOINT="${ZENOH_LOCAL_ENDPOINT:-tcp/127.0.0.1:7448}"
# Exported (not just used to derive EFDI_CERT_DIR) so native Python bridges and
# the containerized admin can share the same certificate location. Defaults
# inside the repo, under compose/certs/ — gitignored, admins drop the router's
# certificates here rather than scattering them somewhere in $HOME.
export BUNDLE_DIR="${BUNDLE_DIR:-$SCRIPT_DIR/compose/certs}"
export EFDI_CERT_DIR="${EFDI_CERT_DIR:-$BUNDLE_DIR/efdi}"

# Runtime state (logs, PID files, Zenoh's own config/certs under
# ${POD_STATE_DIR}/zenoh/...) defaults inside the repo, under compose/state/ —
# gitignored, keeps every path a dev needs to find in one place instead of
# scattered across $HOME. Exported (not just a local default) because
# `docker compose` (invoked later for zenoh-router) needs it in the real
# environment to interpolate ${POD_STATE_DIR} in volume paths — it has no
# access to this script's own defaulting logic.
export POD_STATE_DIR="${POD_STATE_DIR:-$SCRIPT_DIR/compose/state}"
# Host-launched bridges read the same prefix state file the admin writes.
export NAMESPACE_PREFIX_FILE="${POD_STATE_DIR}/namespace-prefix"
export DATA_NAMESPACE_PREFIX_FILE="${POD_STATE_DIR}/data-topic-prefix"
export PYTHONPATH="$COMPOSE_DIR/generated:$COMPOSE_DIR/generated/protocols:$COMPOSE_DIR${PYTHONPATH:+:$PYTHONPATH}"
LOG_DIR="$POD_STATE_DIR/logs"
PID_DIR="$POD_STATE_DIR/.pids"
LAUNCHER_STATE_FILE="$POD_STATE_DIR/launcher-state.env"
mkdir -p "$LOG_DIR" "$PID_DIR"

# ── Ensure venv ────────────────────────────────────────────────────────────
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "Creating venv at $VENV…"
    python3 -m venv "$VENV"
    "$VENV/bin/python3" -m pip install --quiet -r "$COMPOSE_DIR/requirements.txt"
    echo "Venv ready."
fi
PYTHON="$VENV/bin/python3"
if ! "$PYTHON" -c 'import grpc_tools.protoc, google.protobuf, zenoh, defusedxml' 2>/dev/null; then
    "$PYTHON" -m pip install --quiet -r "$COMPOSE_DIR/requirements.txt"
fi
EFDI_PROTOC_PYTHON="$PYTHON" "$SCRIPT_DIR/scripts/generate-protobuf.sh" >/dev/null

# ── ANSI colors (only when stdout is a terminal) ───────────────────────────
if [[ -t 1 ]]; then
    R='\033[0m' BOLD='\033[1m' DIM='\033[2m'
    GREEN='\033[32m' YELLOW='\033[33m' CYAN='\033[36m'
else
    R='' BOLD='' DIM='' GREEN='' YELLOW='' CYAN=''
fi

# ── Service registry ───────────────────────────────────────────────────────
SERVICES=(
    zenoh
    admin-control
    cert-renewer supervisor presence
    meteolt
    sitaware dronuradaras asterix track-fusion
    nffi sapient stanag4586 stanag4609
    sapient-raw stanag4586-raw stanag4609-raw
    mqtt-raw sensorthings-raw
    cap geojson mqtt sensorthings sparkplug spectrum sensor-health mission-route
    cot_layer tak-bridge nvg_bridge nvg_layer
    tak-gateway sitaware-gateway
)

# Restore only non-secret launcher choices. Explicit compose/.env values win;
# remembered addresses are fallbacks for values that were left blank there.
# The file is parsed as data, never sourced, so punctuation in a URL cannot be
# evaluated as shell syntax.
REMEMBERED_SERVICES=""
load_launcher_state() {
    [[ -f "$LAUNCHER_STATE_FILE" ]] || return
    local line key val
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%$'\r'}"
        [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        case "$key" in
            SELECTED_SERVICES)
                REMEMBERED_SERVICES="$val"
                ;;
            TAK_HOST|TAK_HOST_FALLBACK|TAK_TLS_SERVER_NAME|\
            SITAWARE_URL|SITAWARE_URL_FALLBACK|STANAG4609_SRT_URL|STANAG4609_SOURCE|\
            SAPIENT_HOST|STANAG4586_HOST|STANAG4586_PROFILE)
                if [[ -z "${!key:-}" ]]; then
                    printf -v "$key" '%s' "$val"
                    # shellcheck disable=SC2163  # $key names the variable to export.
                    export "$key"
                fi
                ;;
        esac
    done < "$LAUNCHER_STATE_FILE"
}

save_launcher_state() {
    local tmp="${LAUNCHER_STATE_FILE}.tmp.$$" selected="" svc key
    for svc in "${SERVICES[@]}"; do
        [[ "${sel[$svc]:-0}" == "1" ]] || continue
        selected+="${selected:+,}$svc"
    done

    umask 077
    {
        printf '# EFDI launcher memory: selections and endpoint addresses only.\n'
        printf 'SELECTED_SERVICES=%s\n' "$selected"
        for key in TAK_HOST TAK_HOST_FALLBACK TAK_TLS_SERVER_NAME \
                   SITAWARE_URL SITAWARE_URL_FALLBACK STANAG4609_SRT_URL STANAG4609_SOURCE \
                   SAPIENT_HOST STANAG4586_HOST STANAG4586_PROFILE; do
            # A URL with user-info may contain credentials. Use it for this run,
            # but never copy it into persistent launcher memory.
            [[ -n "${!key:-}" && "${!key}" != *://*@* ]] && \
                printf '%s=%s\n' "$key" "${!key}"
        done
    } > "$tmp"
    mv -f "$tmp" "$LAUNCHER_STATE_FILE"
}

load_launcher_state
RESTORE_ONLY=0
if [[ "${1:-}" == "--restore" ]]; then
    RESTORE_ONLY=1
    export EFDI_NONINTERACTIVE=1
fi

declare -A SVC_CAT=(
    [zenoh]="Infrastructure"
    [admin-control]="Infrastructure"
    [cert-renewer]="Infrastructure"
    [supervisor]="Infrastructure"
    [presence]="Infrastructure"
    [meteolt]="Open-data bridges"
    [asterix]="Sensor bridges"
    [mqtt]="Protocols" [sensorthings]="Protocols" [sparkplug]="Protocols"
    [nffi]="Protocols"
    [sitaware]="Sensor bridges" [dronuradaras]="Sensor bridges"
    [sapient]="Protocols" [stanag4586]="Protocols" [stanag4609]="Protocols"
    [tak-bridge]="C2 inputs"
    [mqtt-raw]="Sensor bridges" [sensorthings-raw]="Sensor bridges"
    [sapient-raw]="Sensor bridges"
    [stanag4586-raw]="Sensor bridges" [stanag4609-raw]="Sensor bridges"
    [cap]="Protocols" [geojson]="Protocols"
    [spectrum]="Protocols" [sensor-health]="Protocols" [mission-route]="Protocols"
    [cot_layer]="Output layers"   [nvg_layer]="Output layers"
    [nvg_bridge]="C2 inputs"
    [tak-gateway]="C2 gateways"   [sitaware-gateway]="C2 gateways"
    [track-fusion]="Sensor bridges"
)

declare -A SVC_DESC=(
    [zenoh]="Zenoh message router (Docker)"
    [admin-control]="Web UI host control agent"
    [cert-renewer]="Automatic short-lived transport certificate renewal"
    [supervisor]="Auto-restarts crashed bridges, protocols, and layers"
    [presence]="Liveliness presence tokens (fabric node visibility in panoscope)"
    [meteolt]="meteo.lt weather stations"
    [asterix]="ASTERIX family bundle: UDP ingress + CAT-010/020/021/034/048/062 translators"
    [mqtt]="MQTT sensor JSON on Zenoh → sensor records"
    [sensorthings]="OGC SensorThings observations → sensor records"
    [sparkplug]="Eclipse Sparkplug B (MQTT protobuf) → sensor records"
    [sitaware]="SitaWare HQ friendly force tracking (inbound REST)"
    [nffi]="Raw NFFI XML on Zenoh → normalized friendly-force tracks"
    [dronuradaras]="dronuradaras.lt drone detection network"
    [sapient]="SAPIENT / BSI Flex 335 sensor feed"
    [stanag4586]="STANAG 4586 UAV control (VSM)"
    [stanag4609]="STANAG 4609 KLV decoder (raw → tracks)"
    [mqtt-raw]="MQTT broker → Zenoh raw"
    [sensorthings-raw]="OGC SensorThings REST poll → Zenoh raw"
    [sapient-raw]="SAPIENT/FLEX 335 TCP → Zenoh raw"
    [stanag4586-raw]="STANAG 4586 TCP → Zenoh raw"
    [stanag4609-raw]="STANAG 4609 SRT/KLV → Zenoh raw"
    [cap]="CAP 1.2 XML on Zenoh → alerts"
    [geojson]="GeoJSON/OGC Features on Zenoh → areas"
    [spectrum]="RF spectrum observations on Zenoh"
    [sensor-health]="Sensor health on Zenoh"
    [mission-route]="UAV routes and corridors on Zenoh"
    [cot_layer]="CoT → TAK Server (mTLS)"
    [tak-bridge]="TAK Server CoT ingress"
    [nvg_bridge]="SitaWare NVG export → Zenoh"
    [nvg_layer]="EFDI tracks → SitaWare (NVG feed, SitaWare polls)"
    [tak-gateway]="TAK: CoT ingress + egress in one process (replaces cot_layer + tak-bridge)"
    [sitaware-gateway]="SitaWare: NVG feed + NVG/REST ingress in one process (replaces nvg_layer + nvg_bridge + sitaware)"
    [track-fusion]="Radar/ADS-B track correlation"
)

# ── Ready check — 0=can start, 1=missing config ───────────────────────────
svc_ready() {
    case "$1" in
        zenoh|meteolt|\
        dronuradaras|nffi|cot_layer|track-fusion|\
        cap|geojson|spectrum|sensor-health|mission-route)
            return 0 ;;
        admin-control) [[ -n "${ZENOH_ADMIN_SECRET_KEY:-}" || -n "${EFDI_CONTROL_TOKEN:-}" ]] ;;
        cert-renewer)
            [[ -n "${EFDI_STEP_CA_URL:-}" &&
               "${EFDI_STEP_CA_URL}" == https://* &&
               -f "${EFDI_STEP_RENEW_CERT_PATH:-${EFDI_CERT_DIR}/${PARTNER_NAMESPACE}-cert.pem}" &&
               -f "${EFDI_STEP_RENEW_KEY_PATH:-${EFDI_CERT_DIR}/${PARTNER_NAMESPACE}-key.pem}" ]]
            ;;
        asterix) return 0 ;;
        presence) [[ -n "${PARTNER_NAMESPACE:-}" ]] ;;
        mqtt)         return 0 ;;
        sensorthings) return 0 ;;
        sparkplug)    return 0 ;;
        mqtt-raw)     [[ "${MQTT_HOST:-}" ]] ;;
        sensorthings-raw) [[ "${SENSORTHINGS_URL:-}" ]] ;;
        sapient-raw)  [[ "${SAPIENT_RAW_PORT:-}" ]] ;;
        stanag4586-raw) [[ "${STANAG4586_RAW_PORT:-}" ]] ;;
        stanag4609-raw) [[ "${STANAG4609_SRT_URL:-}" ]] ;;
        sitaware)     return 0 ;;  # always ready; prompts for server IP at launch if unset
        tak-bridge)   [[ "${TAK_HOST:-}" || "${TAK_HOST_FALLBACK:-}" ]] ;;
        sapient) return 0 ;;
        stanag4586) [[ "${STANAG4586_PROFILE:-}" == "legacy_ed3_approx" &&
                       ( -n "${STANAG4586_ZENOH_RAW:-}" || -n "${STANAG4586_HOST:-}" ) ]] ;;
        stanag4609) [[ "${STANAG4609_SRT_URL:-}" ]] ;;
        # nvg_layer serves a feed, so it needs a port to listen on;
        # nvg_bridge reads SitaWare's export, so it needs a URL.
        nvg_layer) [[ -n "${SITAWARE_HQ_NVG_PORT:-}" ]] ;;
        nvg_bridge) [[ -n "${SITAWARE_NVG_IMPORT_URL:-}" || ( -n "${SITAWARE_URL:-}" && -n "${SITAWARE_API_PATH:-}" ) ]] ;;
        tak-gateway) [[ "${TAK_HOST:-}" || "${TAK_HOST_FALLBACK:-}" ]] ;;
        sitaware-gateway)
            [[ "${SITAWARE_HQ_NVG_ENABLE:-0}" == "1" ||
               -n "${SITAWARE_NVG_IMPORT_URL:-}" ||
               ( ( -n "${SITAWARE_URL:-}" || -n "${SITAWARE_URL_FALLBACK:-}" ) &&
                 ( -n "${SITAWARE_API_PATH:-}" || "${SITAWARE_DISCOVER:-0}" == "1" ) ) ]]
            ;;
        *)        return 0 ;;
    esac
}

# Short config note shown in status column when not ready
svc_hint() {
    case "$1" in
        asterix) echo "ASTERIX family bundle" ;;
        mqtt-raw) echo "MQTT_HOST not set" ;;
        sensorthings-raw) echo "SENSORTHINGS_URL not set" ;;
        sapient-raw)
            if [[ "${SAPIENT_RAW_PORT:-}" ]]; then
                echo "TCP port ${SAPIENT_RAW_PORT}"
            else
                echo "SAPIENT_RAW_PORT not set"
            fi
            ;;
        stanag4586-raw)
            if [[ "${STANAG4586_RAW_PORT:-}" ]]; then
                echo "TCP port ${STANAG4586_RAW_PORT}"
            else
                echo "STANAG4586_RAW_PORT not set"
            fi
            ;;
        stanag4609-raw) echo "STANAG4609_SRT_URL not set" ;;
        sapient)
            # This function only DESCRIBES a service — it must never launch one.
            # A stray _start here (copy-pasted from launch()) meant that merely
            # asking sapient for its status hint started it.
            if [[ "${SAPIENT_ZENOH_RAW:-}" == "1" ]]; then
                echo "zenoh raw ${SAPIENT_RAW_TOPIC:-(default topic)}"
            elif [[ "${SAPIENT_LISTEN_PORT:-}" ]]; then
                echo "listen ${SAPIENT_BIND:-127.0.0.1}:${SAPIENT_LISTEN_PORT}"
            elif [[ "${SAPIENT_HOST:-}" ]]; then
                echo "${SAPIENT_HOST}:${SAPIENT_PORT:-7001}"
            else
                echo "will prompt for address"
            fi ;;
        stanag4586) echo "set STANAG4586_PROFILE=legacy_ed3_approx plus a 4586 source" ;;
        stanag4609) echo "STANAG4609_SRT_URL not set (ingest via stanag4609-raw)" ;;
        nvg_layer)
            if [[ -n "${SITAWARE_HQ_NVG_PORT:-}" ]]; then
                echo "serving ${SITAWARE_HQ_NVG_BIND:-127.0.0.1}:${SITAWARE_HQ_NVG_PORT}${SITAWARE_HQ_NVG_PATH:-/nvg}"
            else
                echo "SITAWARE_HQ_NVG_PORT not set"
            fi ;;
        nvg_bridge)
            if [[ -n "${SITAWARE_NVG_IMPORT_URL:-}" ]]; then
                echo "${SITAWARE_NVG_IMPORT_URL}"
            elif [[ -n "${SITAWARE_URL:-}" && -n "${SITAWARE_API_PATH:-}" ]]; then
                echo "${SITAWARE_URL}${SITAWARE_API_PATH}"
            else
                echo "SITAWARE_NVG_IMPORT_URL not set"
            fi ;;
        sitaware)
            if [[ "${SITAWARE_URL:-}" ]]; then
                [[ "${SITAWARE_URL_FALLBACK:-}" ]] && echo "${SITAWARE_URL} (+fallback)" || echo "${SITAWARE_URL}"
            else
                echo "will prompt for address"
            fi ;;
        tak-bridge)
            if [[ "${TAK_HOST:-}" ]]; then
                [[ "${TAK_HOST_FALLBACK:-}" ]] && echo "${TAK_HOST}:${TAK_PORT:-8087} (+fallback)" || echo "${TAK_HOST}:${TAK_PORT:-8087}"
            else
                echo "will prompt for address"
            fi ;;
        cot_layer)
            if [[ "${TAK_HOST:-}" ]]; then
                [[ "${TAK_HOST_FALLBACK:-}" ]] && echo "${TAK_HOST}:${TAK_PORT:-8087} (+fallback)" || echo "${TAK_HOST}:${TAK_PORT:-8087}"
            else
                echo "will prompt for address"
            fi ;;
        tak-gateway) echo "set TAK_HOST (runs ingress + egress)" ;;
        sitaware-gateway)
            echo "enable SITAWARE_HQ_NVG_ENABLE or configure NVG/REST ingress"
            ;;
        *)        echo "" ;;
    esac
}

is_bridge_pid() {
    local pid="$1" expected_script="${2:-}" arg
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && [[ -r "/proc/$pid/cmdline" ]] || return 1
    local efdi_process=1
    while IFS= read -r -d '' arg; do
        if [[ -n "$expected_script" ]]; then
            [[ "$arg" == "$COMPOSE_DIR/$expected_script" || "$arg" == "$SCRIPT_DIR/$expected_script" ]] && return 0
        elif [[ "$arg" == "$COMPOSE_DIR/"* ]]; then
            return 0
        fi
        [[ "$arg" == "$COMPOSE_DIR/"* ]] && efdi_process=0
    done < "/proc/$pid/cmdline"
    # A service's implementation may change during an upgrade (for example, a
    # service's script moving between layers/ and bridges/). Treat
    # the PID-file's still-live EFDI process as running until an explicit stop
    # or restart removes it; otherwise a normal launcher run can duplicate the
    # same Zenoh subscriber.
    [[ "$efdi_process" == "0" ]]
}

# True when some OTHER service's pidfile already claims this PID. Several
# services run the SAME script with different arguments — asterix runs cat.py
# once per --category — so a bare script match is not enough to decide ownership.
pid_claimed_by_other() {
    local candidate="$1" own_file="$2" other other_pid
    for other in "$PID_DIR"/*.pid; do
        [[ -f "$other" && "$other" != "$own_file" ]] || continue
        IFS= read -r other_pid < "$other" || continue
        [[ "$other_pid" == "$candidate" ]] && return 0
    done
    return 1
}

pid_has_args() {
    local pid="$1"
    shift
    local expected arg found
    local -a actual=()
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    while IFS= read -r -d '' arg; do
        actual+=("$arg")
    done < "/proc/$pid/cmdline"
    for expected in "$@"; do
        [[ -n "$expected" ]] || continue
        found=1
        for arg in "${actual[@]}"; do
            if [[ "$arg" == "$expected" ]]; then
                found=0
                break
            fi
        done
        (( found == 0 )) || return 1
    done
}

is_running() {
    local f="$PID_DIR/$1.pid" pid cmd live_pid
    if [[ "$1" == "asterix" && -z "${2:-}" ]]; then
        local child
        for child in asterix-bridge udp-ingress asterix-cat10 asterix-cat20 asterix-cat21 \
                     asterix-cat34 asterix-cat48 asterix-cat62 \
                     asterix-cat10-raw asterix-cat20-raw asterix-cat21-raw \
                     asterix-cat34-raw asterix-cat48-raw asterix-cat62-raw; do
            is_running "$child" && return 0
        done
        return 1
    fi
    if [[ -f "$f" ]]; then
        IFS= read -r pid < "$f"
        # A PID another service already owns is not this service's process; the
        # pidfile is stale from a previous mis-adoption. Fall through to the
        # scan below rather than reporting a sibling's process as ours.
        if ! pid_claimed_by_other "$pid" "$f" &&
           is_bridge_pid "$pid" "${2:-}" &&
           pid_has_args "$pid" "${@:3}"; then
            return 0
        fi
    fi
    cmd="${2:-}"
    [[ -n "$cmd" ]] || return 1
    while IFS= read -r live_pid; do
        # Never adopt a process another service started. Without this, the
        # second service sharing a script is reported "already running" and is
        # silently never launched.
        pid_claimed_by_other "$live_pid" "$f" && continue
        if is_bridge_pid "$live_pid" "$cmd" &&
           pid_has_args "$live_pid" "${@:3}"; then
            printf '%s\n' "$live_pid" > "$f"
            return 0
        fi
    done < <(pgrep -f "$cmd" 2>/dev/null || true)
    return 1
}

# Prompt for a single server address (blank to skip). One flat network routed
# entirely over the VPN mesh here, so there's no separate LAN-vs-NetBird
# address to ask for.
#   _prompt_address <label> <addr_var>
_prompt_address() {
    local label="$1" addr_var="$2"
    if [[ "${EFDI_NONINTERACTIVE:-}" == "1" ]]; then
        printf -v "$addr_var" '%s' ""
        return
    fi
    local addr_in
    read -rp "$(printf "  ${BOLD}${label} IP/URL${R} (blank to skip): ")" addr_in
    printf -v "$addr_var" '%s' "$addr_in"
}

# Prompt for a username and password (password input hidden via read -s).
#   _prompt_credentials <label> <user_var> <pass_var>
_prompt_credentials() {
    local label="$1" user_var="$2" pass_var="$3"
    if [[ "${EFDI_NONINTERACTIVE:-}" == "1" ]]; then
        printf -v "$user_var" '%s' ""
        printf -v "$pass_var" '%s' ""
        return
    fi
    local user_in pass_in
    read -rp "$(printf "  ${BOLD}${label} username${R}: ")" user_in
    read -rsp "$(printf "  ${BOLD}${label} password${R}: ")" pass_in
    echo
    printf -v "$user_var" '%s' "$user_in"
    printf -v "$pass_var" '%s' "$pass_in"
}

_prompt_secret() {
    local label="$1" value_var="$2" value
    if [[ "${EFDI_NONINTERACTIVE:-}" == "1" ]]; then
        printf -v "$value_var" '%s' ""
        return
    fi
    read -rsp "$(printf "  ${BOLD}%s${R} (blank to skip): " "$label")" value
    echo
    printf -v "$value_var" '%s' "$value"
}

# ── Launch helpers ─────────────────────────────────────────────────────────
_start() {   # _start <name> <rel-script-path> [args…]
    local name="$1"; shift
    local script="$1"; shift
    local pid_file="$PID_DIR/$name.pid"
    if is_running "$name" "$script" "$@"; then
        printf "  ${DIM}[skip]${R}  %-16s already running (pid %s)\n" "$name" "$(cat "$pid_file")"
        return
    fi
    rm -f "$pid_file"
    ( exec setsid "$PYTHON" "$COMPOSE_DIR/$script" "$@" >> "$LOG_DIR/$name.log" 2>&1 ) &
    echo $! > "$pid_file"
    printf "  ${GREEN}[start]${R} %-16s pid %s\n" "$name" "$!"
}

asterix_category_uses_raw() {
    local wanted="$1" item
    [[ -n "${UDP_INGRESS_PORT:-${ASTERIX_PORT:-}}" ||
       -n "${ASTERIX_ZENOH_UPSTREAM_ENDPOINT:-}" ]] || return 1
    IFS=',' read -r -a _asterix_categories <<< "${ASTERIX_CATEGORIES:-34,48}"
    for item in "${_asterix_categories[@]}"; do
        item="${item//[[:space:]]/}"
        [[ "$item" == "$wanted" ]] && return 0
    done
    return 1
}

_start_asterix_bundle() {
    local category port tcp_var host
    local -a tcp_args

    if [[ -n "${ASTERIX_ZENOH_UPSTREAM_ENDPOINT:-}" ]]; then
        _start asterix-bridge bridges/asterix_bridge.py
    fi

    if [[ -n "${UDP_INGRESS_PORT:-${ASTERIX_PORT:-}}" ]]; then
        _start udp-ingress bridges/udp_ingress_bridge.py
    else
        printf "  ${YELLOW}[skip]${R}  udp-ingress      set UDP_INGRESS_PORT for generic UDP capture\n"
    fi

    for category in 10 20 21 34 48; do
        case "$category" in
            10) port="${CAT10_PORT:-}"; tcp_var="${CAT10_TCP:-}" ;;
            20) port="${CAT20_PORT:-}"; tcp_var="${CAT20_TCP:-}" ;;
            21) port="${CAT21_PORT:-}"; tcp_var="${CAT21_TCP:-}" ;;
            34) port="${CAT34_PORT:-}"; tcp_var="${CAT34_TCP:-}" ;;
            48) port="${CAT48_PORT:-}"; tcp_var="${CAT48_TCP:-}" ;;
        esac
        if asterix_category_uses_raw "$category"; then
            _start "asterix-cat${category}-raw" protocols/vendors/asterix/cat.py \
                --category "$category" --zenoh-raw
        fi
        if [[ -n "$port" ]]; then
            tcp_args=()
            [[ "$tcp_var" == "1" ]] && tcp_args=(--tcp)
            _start "asterix-cat${category}" protocols/vendors/asterix/cat.py \
                --category "$category" --port "$port" "${tcp_args[@]}"
        elif ! asterix_category_uses_raw "$category"; then
            printf "  ${YELLOW}[skip]${R}  asterix-cat%-2s    set CAT%s_PORT to enable\n" \
                "$category" "$category"
        fi
    done

    host="${CAT62_HOST:-${RADAR_HOST:-}}"
    if asterix_category_uses_raw 62; then
        _start asterix-cat62-raw protocols/vendors/asterix/cat.py --category 62 --zenoh-raw
    fi
    if [[ -n "$host" && "$host" != "127.0.0.1" ]]; then
        _start asterix-cat62 protocols/vendors/asterix/cat.py --category 62 \
            --host "$host" --port "${CAT62_PORT:-${RADAR_PORT:-50062}}"
    elif [[ "${CAT62_UDP:-}" == "1" ]]; then
        _start asterix-cat62 protocols/vendors/asterix/cat.py --category 62 \
            --udp --port "${CAT62_PORT:-50062}"
    elif ! asterix_category_uses_raw 62; then
        printf "  ${YELLOW}[skip]${R}  asterix-cat62    set CAT62_HOST or CAT62_UDP=1 to enable\n"
    fi
}

_reject_c2_conflict() {
    local requested="$1" conflict
    shift
    for conflict in "$@"; do
        if is_running "$conflict"; then
            printf "  ${YELLOW}[skip]${R}  %-16s conflicts with running %s; stop it first\n" \
                "$requested" "$conflict"
            return 1
        fi
    done
}

launch() {
    local name="$1"
    case "$name" in
        tak-gateway)
            _reject_c2_conflict "$name" cot_layer tak-bridge || return 1 ;;
        cot_layer|tak-bridge)
            _reject_c2_conflict "$name" tak-gateway || return 1 ;;
        sitaware-gateway)
            _reject_c2_conflict "$name" nvg_layer nvg_bridge sitaware || return 1 ;;
        nvg_layer|nvg_bridge|sitaware)
            _reject_c2_conflict "$name" sitaware-gateway || return 1 ;;
    esac
    case "$name" in

        zenoh)
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
                printf "  ${DIM}[skip]${R}  zenoh-router already running\n"
            else
                printf "  ${GREEN}[start]${R} zenoh-router (Docker)\n"
                docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" up -d zenoh-router
                printf "  Waiting for zenoh-router"
                for _ in $(seq 1 20); do
                    sleep 1
                    if docker compose -f "$SCRIPT_DIR/compose/docker-compose.yml" ps zenoh-router \
                            --format "{{.Status}}" 2>/dev/null | grep -q "healthy"; then
                        echo " OK"; return
                    fi
                    printf "."
                done
                echo " (timeout — continuing)"
            fi
            ;;

        admin-control)
            if [[ -z "${ZENOH_ADMIN_SECRET_KEY:-}" && -z "${EFDI_CONTROL_TOKEN:-}" ]]; then
                printf "  ${YELLOW}[skip]${R}  admin-control requires ZENOH_ADMIN_SECRET_KEY or EFDI_CONTROL_TOKEN\n"
                return 1
            fi
            if is_running "admin-control" "admin_control.py"; then
                printf "  ${DIM}[skip]${R}  %-16s already running (pid %s)\n" "admin-control" "$(cat "$PID_DIR/admin-control.pid")"
                return
            fi
            rm -f "$PID_DIR/admin-control.pid"
            ( exec setsid "$PYTHON" "$COMPOSE_DIR/admin_control.py" \
                >> "$LOG_DIR/admin-control.log" 2>&1 ) &
            echo $! > "$PID_DIR/admin-control.pid"
            printf "  ${GREEN}[start]${R} %-16s pid %s\n" "admin-control" "$!"
            ;;

        supervisor)
            if is_running "supervisor" "supervisor.py"; then
                printf "  ${DIM}[skip]${R}  %-16s already running (pid %s)\n" "supervisor" "$(cat "$PID_DIR/supervisor.pid")"
                return
            fi
            rm -f "$PID_DIR/supervisor.pid"
            ( exec setsid "$PYTHON" "$COMPOSE_DIR/supervisor.py" \
                >> "$LOG_DIR/supervisor.log" 2>&1 ) &
            echo $! > "$PID_DIR/supervisor.pid"
            printf "  ${GREEN}[start]${R} %-16s pid %s\n" "supervisor" "$!"
            ;;

        presence)
            _start presence presence.py
            ;;

        cert-renewer)
            local renew_cert renew_key renew_root renew_url
            renew_cert="${EFDI_STEP_RENEW_CERT_PATH:-${EFDI_CERT_DIR}/${PARTNER_NAMESPACE}-cert.pem}"
            renew_key="${EFDI_STEP_RENEW_KEY_PATH:-${EFDI_CERT_DIR}/${PARTNER_NAMESPACE}-key.pem}"
            renew_root="${EFDI_STEP_RENEW_ROOT_PATH:-${POD_STATE_DIR}/pki/step-ca/certs/root_ca.crt}"
            renew_url="$EFDI_STEP_CA_URL"
            if [[ -z "$renew_url" || "$renew_url" != https://* ]]; then
                printf "  ${YELLOW}[skip]${R}  cert-renewer requires an https:// EFDI_STEP_CA_URL\n"
                return 1
            fi
            export EFDI_STEP_RENEW_RUNTIME_CERT_PATH="${EFDI_STEP_RENEW_RUNTIME_CERT_PATH:-${POD_STATE_DIR}/zenoh/tls/pod-cert.pem}"
            if is_running "cert-renewer" "scripts/pki/renew-step-identities.sh"; then
                printf "  ${DIM}[skip]${R}  %-16s already running (pid %s)\n" "cert-renewer" "$(cat "$PID_DIR/cert-renewer.pid")"
                return
            fi
            ( exec setsid "$SCRIPT_DIR/scripts/pki/renew-step-identities.sh" --daemon \
                "$renew_url" "$renew_root" "$renew_cert:$renew_key" \
                >> "$LOG_DIR/cert-renewer.log" 2>&1 ) &
            echo $! > "$PID_DIR/cert-renewer.pid"
            printf "  ${GREEN}[start]${R} %-16s pid %s\n" "cert-renewer" "$!"
            ;;

        meteolt)
            _start meteolt bridges/meteolt_forecast_bridge.py
            ;;

        asterix)
            _start_asterix_bundle
            ;;

        mqtt-raw)
            if [[ -z "${MQTT_HOST:-}" ]]; then
                _prompt_address "MQTT broker" MQTT_HOST
                if [[ -z "${MQTT_HOST:-}" ]]; then
                    printf "  ${YELLOW}[skip]${R}  mqtt-raw         no broker address entered\n"
                    return
                fi
                export MQTT_HOST
            fi
            _start mqtt-raw bridges/mqtt_bridge.py
            ;;

        sensorthings-raw)
            if [[ -z "${SENSORTHINGS_URL:-}" ]]; then
                _prompt_address "OGC SensorThings service root (https://host/v1.1)" SENSORTHINGS_URL
                if [[ -z "${SENSORTHINGS_URL:-}" ]]; then
                    printf "  ${YELLOW}[skip]${R}  sensorthings-raw no service root entered\n"
                    return
                fi
                export SENSORTHINGS_URL
            fi
            _start sensorthings-raw bridges/sensorthings_bridge.py
            ;;

        sapient-raw)
            _start sapient-raw bridges/sapient_flex335_bridge.py --tcp --port "${SAPIENT_RAW_PORT:-7001}"
            ;;

        stanag4586-raw)
            _start stanag4586-raw bridges/4586_bridge.py --tcp --port "${STANAG4586_RAW_PORT:-4586}"
            ;;

        stanag4609-raw)
            if [[ -z "${STANAG4609_SRT_URL:-}" ]]; then
                printf "  ${YELLOW}[skip]${R}  stanag4609-raw   set STANAG4609_SRT_URL (srt://host:port)\n"
                return
            fi
            _start stanag4609-raw bridges/4609_bridge.py
            ;;

        cap)
            _start cap protocols/random/cap.py
            ;;

        geojson)
            _start geojson protocols/random/geojson_features.py
            ;;

        mqtt)
            _start mqtt protocols/random/mqtt_json.py
            ;;

        sensorthings)
            _start sensorthings protocols/random/sensorthings.py
            ;;

        sparkplug)
            _start sparkplug protocols/vendors/sparkplug/sparkplug.py
            ;;

        spectrum)
            _start spectrum protocols/random/spectrum_observation.py
            ;;

        sensor-health)
            _start sensor-health protocols/random/sensor_health.py
            ;;

        mission-route)
            _start mission-route protocols/random/mission_route.py
            ;;

        sitaware)
            if [[ -z "${SITAWARE_URL:-}" && -z "${SITAWARE_URL_FALLBACK:-}" ]]; then
                local sw_addr
                _prompt_address "SitaWare Server" sw_addr
                if [[ -z "$sw_addr" ]]; then
                    printf "  ${YELLOW}[skip]${R}  sitaware        no address entered\n"
                    return
                fi
                export SITAWARE_URL="$sw_addr"
            fi
            if [[ -z "${SITAWARE_USER:-}" ]]; then
                local sw_user sw_pass
                _prompt_credentials "SitaWare" sw_user sw_pass
                export SITAWARE_USER="$sw_user"
                export SITAWARE_PASS="$sw_pass"
            fi
            if [[ -z "${SITAWARE_API_PATH:-}" && "${SITAWARE_DISCOVER:-}" != "1" ]]; then
                printf "  ${YELLOW}[skip]${R}  sitaware        set SITAWARE_API_PATH or SITAWARE_DISCOVER=1\n"
                return
            fi
            local sf=(); [[ "${SITAWARE_DISCOVER:-}" == "1" ]] && sf=(--discover)
            _start sitaware bridges/sitaware_bridge.py "${sf[@]}"
            ;;

        nffi)
            _start nffi protocols/random/nffi.py
            ;;

        sapient)
            if [[ "${SAPIENT_LISTEN_PORT:-}" ]]; then
                local sapient_args=(--listen "$SAPIENT_LISTEN_PORT" --bind "${SAPIENT_BIND:-127.0.0.1}")
                [[ "${SAPIENT_ALLOW_PEER:-}" ]] && sapient_args+=(--allow-peer "$SAPIENT_ALLOW_PEER")
                _start sapient protocols/vendors/sapient/flex335.py "${sapient_args[@]}"
                return
            fi
            if [[ -z "${SAPIENT_HOST:-}" ]]; then
                if [[ "${EFDI_NONINTERACTIVE:-}" == "1" ]]; then
                    _start sapient protocols/vendors/sapient/flex335.py --zenoh-raw --raw-topic "${SAPIENT_RAW_TOPIC:-}"
                    return
                fi
                local sapient_host
                _prompt_address "SAPIENT source" sapient_host
                if [[ -z "$sapient_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  sapient           no address entered\n"
                    return
                fi
                export SAPIENT_HOST="$sapient_host"
            fi
            _start sapient protocols/vendors/sapient/flex335.py --host "$SAPIENT_HOST" --port "${SAPIENT_PORT:-7001}"
            ;;

        stanag4586)
            if [[ "${STANAG4586_PROFILE:-}" != "legacy_ed3_approx" ]]; then
                printf "  ${YELLOW}[skip]${R}  stanag4586       validate the VSM ICD, then set STANAG4586_PROFILE=legacy_ed3_approx\n"
                return
            fi
            if [[ "${STANAG4586_ZENOH_RAW:-}" == "1" ]]; then
                stanag_args=(--zenoh-raw)
                [[ "${STANAG4586_RAW_TOPIC:-}" ]] && stanag_args+=(--raw-topic "$STANAG4586_RAW_TOPIC")
                _start stanag4586 protocols/vendors/stanag/4586.py "${stanag_args[@]}"
            elif [[ -n "${STANAG4586_HOST:-}" ]]; then
                _start stanag4586 protocols/vendors/stanag/4586.py \
                    --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
            else
                printf "  ${YELLOW}[skip]${R}  stanag4586       set STANAG4586_HOST or STANAG4586_ZENOH_RAW=1\n"
            fi
            ;;

        stanag4609)
            if [[ -z "${STANAG4609_SRT_URL:-}" ]]; then
                printf "  ${YELLOW}[skip]${R}  stanag4609       set STANAG4609_SRT_URL (ingest runs as stanag4609-raw)\n"
                return
            fi
            _start stanag4609 protocols/vendors/stanag/4609.py --zenoh-raw
            ;;

        dronuradaras)
            _start dronuradaras bridges/dronuradaras_bridge.py
            ;;


        cot_layer)
            local tak_host="${TAK_HOST:-}"
            local tak_host2="${TAK_HOST_FALLBACK:-}"
            if [[ -z "$tak_host" && -z "$tak_host2" ]]; then
                _prompt_address "TAK Server" tak_host
                if [[ -z "$tak_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  cot_layer          no address entered\n"
                    return
                fi
                export TAK_HOST="$tak_host"
            fi
            local tcp_hosts=(); [[ -n "$tak_host"  ]] && tcp_hosts+=(--host "$tak_host")
            [[ -n "$tak_host2" ]] && tcp_hosts+=(--host "$tak_host2")
            local tcp_args=("${tcp_hosts[@]}" --port "${TAK_PORT:-8089}")
            if [[ "${TAK_TLS:-}" == "1" ]]; then
                tcp_args+=(--tls --cert "${TAK_CERT:-}" --key "${TAK_KEY:-}" --ca "${TAK_CA:-}")
            fi
            _start cot_layer layers/cot_layer.py "${tcp_args[@]}"
            ;;

        tak-bridge)
            local tak_ingest_host="${TAK_HOST:-}"
            local tak_ingest_host2="${TAK_HOST_FALLBACK:-}"
            if [[ -z "$tak_ingest_host" && -z "$tak_ingest_host2" ]]; then
                _prompt_address "TAK Server CoT feed" tak_ingest_host
                if [[ -z "$tak_ingest_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  tak-bridge       no address entered\n"
                    return
                fi
                export TAK_HOST="$tak_ingest_host"
            fi
            local tak_ingest_args=()
            [[ -n "$tak_ingest_host"  ]] && tak_ingest_args+=(--host "$tak_ingest_host")
            [[ -n "$tak_ingest_host2" ]] && tak_ingest_args+=(--host "$tak_ingest_host2")
            if [[ "${TAK_TLS:-}" == "1" ]]; then
                tak_ingest_args+=(--tls --cert "${TAK_CERT:-}" --key "${TAK_KEY:-}" --ca "${TAK_CA:-}")
            fi
            _start tak-bridge bridges/tak_bridge.py "${tak_ingest_args[@]}"
            ;;

        nvg_bridge)
            # Reads SitaWare's NVG Export Endpoint into the fabric. Falls back
            # to the same URL the sitaware bridge uses, since both point at the
            # same server — one speaks NVG XML, the other the JSON track API.
            if [[ -z "${SITAWARE_NVG_IMPORT_URL:-}" && ( -z "${SITAWARE_URL:-}" || -z "${SITAWARE_API_PATH:-}" ) ]]; then
                printf "  ${YELLOW}[skip]${R}  nvg_bridge         set SITAWARE_NVG_IMPORT_URL (or SITAWARE_URL + SITAWARE_API_PATH)\n"
                return
            fi
            _start nvg_bridge bridges/nvg_bridge.py
            ;;

        nvg_layer)
            # Serves the NVG feed SitaWare's Import Subscription polls, so it
            # takes a listen address and has nothing to prompt for.
            if [[ -z "${SITAWARE_HQ_NVG_PORT:-}" ]]; then
                printf "  ${YELLOW}[skip]${R}  nvg_layer          set SITAWARE_HQ_NVG_PORT\n"
                return
            fi
            _start nvg_layer layers/nvg_layer.py
            ;;

        tak-gateway)
            # One process = TAK CoT egress + ingress (env-driven, no prompt).
            # Run this INSTEAD of cot_layer + tak-bridge, not alongside them.
            _start tak-gateway c2/tak_gateway.py
            ;;

        sitaware-gateway)
            # One process = SitaWare NVG feed + NVG/REST ingress (env-driven).
            # Run this INSTEAD of nvg_layer + nvg_bridge + sitaware.
            _start sitaware-gateway c2/sitaware_gateway.py
            ;;

        track-fusion)
            _start track-fusion bridges/track_fusion_bridge.py
            ;;

    esac
}

# Prerequisite report for the admin-control agent, one line per service:
#
#   <name><TAB>ready|blocked<TAB><hint>
#
# The web UI needs to know that a service cannot start BEFORE offering a Start
# button, otherwise an unconfigured service spawns, exits immediately and shows
# up as CRASHED with no reason. That answer lives in svc_ready/svc_hint, and
# re-implementing those tables in Python would leave two copies to drift apart,
# so the agent asks this script instead. Reporting only — starts nothing.
if [[ "${1:-}" == "--check-all" ]]; then
    for svc in "${SERVICES[@]}"; do
        if svc_ready "$svc"; then
            printf '%s\tready\t%s\n' "$svc" "$(svc_hint "$svc")"
        else
            printf '%s\tblocked\t%s\n' "$svc" "$(svc_hint "$svc")"
        fi
    done
    exit 0
fi

# Non-interactive entrypoint used by the localhost admin-control agent.  It
# reuses the exact same launch table as the human menu, including environment
# validation and PID handling, without opening a terminal prompt.
if [[ "${1:-}" == "--service" ]]; then
    requested="${2:-}"
    for svc in "${SERVICES[@]}"; do
        if [[ "$svc" == "$requested" ]]; then
            launch "$requested"
            exit $?
        fi
    done
    echo "Unknown service: $requested" >&2
    exit 2
fi

# ── Interactive menu ───────────────────────────────────────────────────────
declare -A sel

normalize_c2_selection() {
    if [[ "${sel[tak-gateway]:-0}" == "1" ]]; then
        sel[cot_layer]=0
        sel[tak-bridge]=0
    fi
    if [[ "${sel[sitaware-gateway]:-0}" == "1" ]]; then
        sel[nvg_layer]=0
        sel[nvg_bridge]=0
        sel[sitaware]=0
    fi
}

select_service() {
    local service="$1"
    sel[$service]=1
    case "$service" in
        tak-gateway)
            sel[cot_layer]=0; sel[tak-bridge]=0 ;;
        cot_layer|tak-bridge)
            sel[tak-gateway]=0 ;;
        sitaware-gateway)
            sel[nvg_layer]=0; sel[nvg_bridge]=0; sel[sitaware]=0 ;;
        nvg_layer|nvg_bridge|sitaware)
            sel[sitaware-gateway]=0 ;;
    esac
}

# Restore the last valid selection. On first use, default to zenoh + the main
# output layers, plus ASTERIX when its input is configured.
for svc in "${SERVICES[@]}"; do sel[$svc]=0; done
restored=0
if [[ -n "$REMEMBERED_SERVICES" ]]; then
    IFS=',' read -r -a remembered_services <<< "$REMEMBERED_SERVICES"
    for remembered in "${remembered_services[@]}"; do
        for svc in "${SERVICES[@]}"; do
            if [[ "$remembered" == "$svc" ]]; then
                sel[$svc]=1
                restored=1
                break
            fi
        done
    done
fi
# A previous `run.sh all` or manual native-process start may have launched
# services that are not yet present in launcher memory. Show those processes as
# selected too, then persist the merged set after this run. This keeps the menu
# faithful to the actual PID-managed runtime instead of merely labeling an
# unchecked item as RUNNING.
for svc in "${SERVICES[@]}"; do
    if is_running "$svc"; then
        sel[$svc]=1
        restored=1
    fi
done
if (( restored == 0 )); then
    for svc in zenoh admin-control cot_layer track-fusion asterix stanag; do
        sel[$svc]=1
    done
fi
# The web UI's native-process control plane is always kept selected so an old
# launcher-state file cannot leave Runtime Control disconnected after upgrade.
sel[admin-control]=1
sel[supervisor]=1
svc_ready cert-renewer && sel[cert-renewer]=1 || true

draw_menu() {
    clear
    printf "${BOLD}╔══════════════════════════════════════════════════════════════════╗${R}\n"
    printf "${BOLD}║           EFDI Bridge Launcher  —  select services to start      ║${R}\n"
    printf "${BOLD}╚══════════════════════════════════════════════════════════════════╝${R}\n"

    local prev_cat="" idx=0
    for svc in "${SERVICES[@]}"; do
        (( idx++ ))
        local cat="${SVC_CAT[$svc]}"

        if [[ "$cat" != "$prev_cat" ]]; then
            printf "\n  ${CYAN}${BOLD}%s${R}\n" "$cat"
            printf "  ${DIM}──────────────────────────────────────────────────────────${R}\n"
            prev_cat="$cat"
        fi

        # Status
        local stat scol
        if is_running "$svc"; then
            stat="RUNNING" scol="$GREEN"
        elif svc_ready "$svc"; then
            stat="ready" scol="$DIM"
        else
            stat="$(svc_hint "$svc")" scol="$YELLOW"
        fi

        # Checkbox
        local chk ccol
        if [[ "${sel[$svc]}" == "1" ]]; then chk="✓" ccol="$GREEN"
        else chk=" " ccol="$DIM"; fi

        printf "  ${DIM}[%2d]${R} ${ccol}[%s]${R} %-16s ${DIM}%-42s${R}  ${scol}%s${R}\n" \
            "$idx" "$chk" "$svc" "${SVC_DESC[$svc]}" "$stat"
    done

    printf "\n  ${DIM}──────────────────────────────────────────────────────────────${R}\n"
    printf "  ${DIM}Toggle: type number(s)   a=select all   n=clear all   q=quit${R}\n"
    printf "  ${DIM}Press Enter with no input to start the selected services.${R}\n\n"
}

change_selection=1
if (( RESTORE_ONLY == 1 )); then
    change_selection=0
fi
if (( restored == 1 )) && [[ -t 0 ]]; then
    draw_menu
    printf "  ${GREEN}Saved selection restored.${R} Auto-starting in 5 seconds.\n"
    printf "  Press ${BOLD}c${R} to change settings, ${BOLD}q${R} to quit, or Enter to start now: "
    saved_action=""
    if read -r -t 5 saved_action; then
        case "$saved_action" in
            c|C) change_selection=1 ;;
            q|Q) exit 0 ;;
            *)   change_selection=0 ;;
        esac
    else
        echo
        change_selection=0
    fi
fi

while (( change_selection == 1 )); do
    draw_menu
    printf "${BOLD}> ${R}"
    read -r input || { echo; exit 0; }

    case "$input" in
        q|Q)
            exit 0
            ;;
        a|A)
            for svc in "${SERVICES[@]}"; do
                svc_ready "$svc" && select_service "$svc" || true
            done
            ;;
        n|N)
            for svc in "${SERVICES[@]}"; do sel[$svc]=0; done
            ;;
        "")
            # Confirm at least one selected
            any=0
            for svc in "${SERVICES[@]}"; do [[ "${sel[$svc]}" == "1" ]] && { any=1; break; }; done
            if (( any == 0 )); then
                printf "\n  ${YELLOW}Nothing selected — pick at least one service.${R}\n"
                sleep 1
                continue
            fi
            break
            ;;
        *)
            # Toggle by number (space-separated)
            for tok in $input; do
                if [[ "$tok" =~ ^[0-9]+$ ]] && (( tok >= 1 && tok <= ${#SERVICES[@]} )); then
                    svc="${SERVICES[$((tok-1))]}"
                    [[ "${sel[$svc]}" == "1" ]] && sel[$svc]=0 || select_service "$svc"
                fi
            done
            ;;
    esac
done

# ── Start ──────────────────────────────────────────────────────────────────
printf "\n${BOLD}Starting selected services…${R}\n\n"

normalize_c2_selection
for svc in "${SERVICES[@]}"; do
    [[ "${sel[$svc]}" == "1" ]] || continue
    launch "$svc"
done

save_launcher_state

printf "\n${BOLD}Done.${R}  Logs → ${LOG_DIR}/   Stop → ./stop.sh\n"
