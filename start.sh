#!/usr/bin/env bash
# start.sh — interactive EFDI service launcher
# Usage: ./start.sh

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

export ZENOH_LOCAL_ENDPOINT="${ZENOH_LOCAL_ENDPOINT:-tcp/127.0.0.1:7448}"
# Exported (not just used to derive EFDI_CERT_DIR) so native Python bridges and
# the containerized admin can share the same certificate location. Defaults
# inside the repo, under compose/certs/ — gitignored, admins drop the router's
# certificates here rather than scattering them somewhere in $HOME.
export BUNDLE_DIR="${BUNDLE_DIR:-$SCRIPT_DIR/compose/certs}"
export EFDI_CERT_DIR="$BUNDLE_DIR"

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
export PYTHONPATH="$COMPOSE_DIR${PYTHONPATH:+:$PYTHONPATH}"
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
    airplaneslive adsblol aisstream aprs openmeteo meteolt
    sitaware dronuradaras dji-cloud utm-ans asterix-udp track-fusion
    asterix-cat10 asterix-cat20 asterix-cat21 asterix-cat34 asterix-cat48 asterix-cat62
    link16 mavlink opendroneid vmf nffi sapient stanag4586
    mavlink-raw link16-raw vmf-raw sapient-raw stanag4586-raw
    cap geojson ais-nmea spectrum sensor-health mission-route
    cot-rx
    cot-udp cot-udp-tak cot-tcp sitaware-hq-nvg
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
            TAK_HOST|TAK_HOST_FALLBACK|TAK_UDP_HOST|TAK_UDP_HOST_FALLBACK|\
            SITAWARE_URL|SITAWARE_URL_FALLBACK|COT_RX_HOST|\
            SAPIENT_HOST|STANAG4586_HOST)
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
        for key in TAK_HOST TAK_HOST_FALLBACK TAK_UDP_HOST TAK_UDP_HOST_FALLBACK \
                   SITAWARE_URL SITAWARE_URL_FALLBACK \
                   COT_RX_HOST \
                   SAPIENT_HOST STANAG4586_HOST; do
            # A URL with user-info may contain credentials. Use it for this run,
            # but never copy it into persistent launcher memory.
            [[ -n "${!key:-}" && "${!key}" != *://*@* ]] && \
                printf '%s=%s\n' "$key" "${!key}"
        done
    } > "$tmp"
    mv -f "$tmp" "$LAUNCHER_STATE_FILE"
}

load_launcher_state

declare -A SVC_CAT=(
    [zenoh]="Infrastructure"
    [admin-control]="Infrastructure"
    [airplaneslive]="Open-data bridges" [adsblol]="Open-data bridges"
    [aisstream]="Open-data bridges" [aprs]="Open-data bridges"
    [openmeteo]="Open-data bridges"
    [meteolt]="Open-data bridges"
    [asterix-udp]="Sensor bridges"
    [asterix-cat10]="Protocols" [asterix-cat20]="Protocols"
    [asterix-cat21]="Protocols" [asterix-cat34]="Protocols"
    [asterix-cat48]="Protocols" [asterix-cat62]="Protocols"
    [link16]="Protocols" [mavlink]="Protocols" [vmf]="Protocols"
    [opendroneid]="Protocols" [nffi]="Protocols"
    [sitaware]="Sensor bridges" [dronuradaras]="Sensor bridges" [dji-cloud]="Sensor bridges"
    [utm-ans]="Open-data bridges"
    [sapient]="Protocols" [stanag4586]="Protocols"
    [mavlink-raw]="Sensor bridges" [link16-raw]="Sensor bridges"
    [vmf-raw]="Sensor bridges" [sapient-raw]="Sensor bridges"
    [stanag4586-raw]="Sensor bridges"
    [cap]="Protocols" [geojson]="Protocols" [ais-nmea]="Protocols"
    [spectrum]="Protocols" [sensor-health]="Protocols" [mission-route]="Protocols"
    [cot-rx]="TAK and SitaWare layers"
    [cot-udp]="Output layers"   [cot-udp-tak]="Output layers"
    [sitaware-hq-nvg]="Output layers"
    [track-fusion]="Sensor bridges"
)

declare -A SVC_DESC=(
    [zenoh]="Zenoh message router (Docker)"
    [admin-control]="Web UI host control agent"
    [airplaneslive]="Airplanes.live ADS-B aircraft"
    [adsblol]="ADSB.lol open-data aircraft"
    [aisstream]="AISstream live vessel positions"
    [aprs]="APRS-IS stations, vehicles, and vessels"
    [openmeteo]="Open-Meteo weather stations"
    [meteolt]="meteo.lt weather stations"
    [asterix-udp]="Mixed ASTERIX UDP → raw category topics"
    [asterix-cat10]="ASTERIX CAT-010 Ed.1.1 airport surface"
    [asterix-cat20]="ASTERIX CAT-020 legacy MLAT profile"
    [asterix-cat21]="ASTERIX CAT-021 legacy ADS-B profile"
    [asterix-cat34]="ASTERIX CAT-034 Ed.1.29 radar service"
    [asterix-cat48]="ASTERIX CAT-048 Ed.1.32 radar targets"
    [asterix-cat62]="ASTERIX CAT-062 legacy system tracks"
    [link16]="Link-16 JREAP-C datalink"
    [mavlink]="MAVLink UAV telemetry"
    [opendroneid]="Raw Open Drone ID on Zenoh → normalized UAV tracks"
    [dji-cloud]="DJI Cloud API MQTT aircraft telemetry"
    [utm-ans]="Lithuanian UTM declared civilian UAV flights"
    [vmf]="VMF MIL-STD-47001C messages"
    [sitaware]="SitaWare HQ friendly force tracking (inbound REST)"
    [nffi]="Raw NFFI XML on Zenoh → normalized friendly-force tracks"
    [dronuradaras]="dronuradaras.lt drone detection network"
    [cot-rx]="TAK Server direct CoT receiver"
    [sapient]="SAPIENT / BSI Flex 335 sensor feed"
    [stanag4586]="STANAG 4586 UAV feed"
    [mavlink-raw]="MAVLink UDP/TCP → Zenoh raw"
    [link16-raw]="Link-16/JREAP-C UDP/TCP → Zenoh raw"
    [vmf-raw]="VMF UDP/TCP → Zenoh raw"
    [sapient-raw]="SAPIENT/FLEX 335 TCP → Zenoh raw"
    [stanag4586-raw]="STANAG 4586 TCP → Zenoh raw"
    [cap]="CAP 1.2 XML on Zenoh → alerts"
    [geojson]="GeoJSON/OGC Features on Zenoh → areas"
    [ais-nmea]="AIS NMEA on Zenoh → vessels"
    [spectrum]="RF spectrum observations on Zenoh"
    [sensor-health]="Sensor health on Zenoh"
    [mission-route]="UAV routes and corridors on Zenoh"
    [cot-udp]="CoT → ATAK UDP multicast 239.2.3.1:6969 (same LAN only)"
    [cot-udp-tak]="CoT → WinTAK/ATAK UDP unicast (crosses LAN/VPN)"
    [cot-tcp]="CoT → TAK Server TCP"
    [sitaware-hq-nvg]="EFDI tracks → SitaWare HQ pull feed (outbound NVG)"
    [track-fusion]="Radar/ADS-B track correlation"
)

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

# ── Ready check — 0=can start, 1=missing config ───────────────────────────
svc_ready() {
    case "$1" in
        zenoh|admin-control|airplaneslive|adsblol|aisstream|aprs|openmeteo|meteolt|\
        dronuradaras|opendroneid|nffi|cot-udp|cot-udp-tak|cot-tcp|track-fusion|\
        cap|geojson|ais-nmea|spectrum|sensor-health|mission-route)
            return 0 ;;
        asterix-udp) [[ "${ASTERIX_PORT:-}" ]] ;;
        asterix-cat10) asterix_category_uses_raw 10 || [[ "${CAT10_PORT:-}" ]] ;;
        asterix-cat20) asterix_category_uses_raw 20 || [[ "${CAT20_PORT:-}" ]] ;;
        asterix-cat21) asterix_category_uses_raw 21 || [[ "${CAT21_PORT:-}" ]] ;;
        asterix-cat34) asterix_category_uses_raw 34 || [[ "${CAT34_PORT:-}" ]] ;;
        asterix-cat48) asterix_category_uses_raw 48 || [[ "${CAT48_PORT:-}" ]] ;;
        asterix-cat62)
            asterix_category_uses_raw 62 ||
                [[ "${CAT62_HOST:-${RADAR_HOST:-}}" || "${CAT62_UDP:-}" == "1" ]] ;;
        link16)   [[ "${LINK16_ZENOH_RAW:-}" == "1" || "${LINK16_PORT:-}" ]] ;;
        mavlink)  [[ "${MAVLINK_ZENOH_RAW:-}" == "1" || "${MAVLINK_PORT:-}" ]] ;;
        dji-cloud) [[ "${DJI_MQTT_HOST:-}" ]] ;;
        utm-ans) [[ "${UTM_ANS_API_URL:-}" ]] ;;
        vmf)          [[ "${VMF_ZENOH_RAW:-}" == "1" || "${VMF_PORT:-}" ]] ;;
        mavlink-raw)  [[ "${MAVLINK_RAW_PORT:-}" ]] ;;
        link16-raw)   [[ "${LINK16_RAW_PORT:-}" ]] ;;
        vmf-raw)      [[ "${VMF_RAW_PORT:-}" ]] ;;
        sapient-raw)  [[ "${SAPIENT_RAW_PORT:-}" ]] ;;
        stanag4586-raw) [[ "${STANAG4586_RAW_PORT:-}" ]] ;;
        sitaware)     return 0 ;;  # always ready; prompts for server IP at launch if unset
        cot-rx)       [[ "${COT_RX_PORT:-}${COT_RX_HOST:-}" ]] ;;
        sapient|stanag4586) return 0 ;;
        sitaware-hq-nvg) [[ "${SITAWARE_HQ_NVG_ENABLE:-}" == "1" ]] ;;
        *)        return 0 ;;
    esac
}

# Short config note shown in status column when not ready
svc_hint() {
    case "$1" in
        asterix-udp) echo "ASTERIX_PORT not set" ;;
        asterix-cat10) echo "CAT10_PORT not set" ;;
        asterix-cat20) echo "CAT20_PORT not set" ;;
        asterix-cat21) echo "CAT21_PORT not set" ;;
        asterix-cat34) echo "CAT34_PORT not set" ;;
        asterix-cat48) echo "CAT48_PORT not set" ;;
        asterix-cat62) echo "CAT62_HOST/UDP not set" ;;
        link16)   echo "set LINK16_PORT or LINK16_ZENOH_RAW=1" ;;
        mavlink)  echo "set MAVLINK_PORT or MAVLINK_ZENOH_RAW=1" ;;
        dji-cloud) echo "DJI_MQTT_HOST not set" ;;
        utm-ans) echo "UTM_ANS_API_URL not set (authorized JSON/GeoJSON feed required)" ;;
        vmf)      echo "set VMF_PORT or VMF_ZENOH_RAW=1" ;;
        mavlink-raw) echo "MAVLINK_RAW_PORT not set" ;;
        link16-raw) echo "LINK16_RAW_PORT not set" ;;
        vmf-raw) echo "VMF_RAW_PORT not set" ;;
        sapient-raw) echo "SAPIENT_RAW_PORT not set" ;;
        stanag4586-raw) echo "STANAG4586_RAW_PORT not set" ;;
        cot-rx)
            if [[ "${COT_RX_TLS:-}" == "1" ]]; then
                echo "TAK mTLS ${COT_RX_HOST:-host not set}"
            else
                echo "COT_RX_PORT/HOST not set"
            fi ;;
        sapient)
            if [[ "${SAPIENT_ZENOH_RAW:-}" == "1" ]]; then
                _start sapient protocols/sapient_flex335.py --zenoh-raw --raw-topic "${SAPIENT_RAW_TOPIC:-}"
                return
            fi
            if [[ "${SAPIENT_LISTEN_PORT:-}" ]]; then
                echo "listen ${SAPIENT_BIND:-127.0.0.1}:${SAPIENT_LISTEN_PORT}"
            elif [[ "${SAPIENT_HOST:-}" ]]; then
                echo "${SAPIENT_HOST}:${SAPIENT_PORT:-7001}"
            else
                echo "will prompt for address"
            fi ;;
        stanag4586)
            [[ "${STANAG4586_HOST:-}" ]] && echo "${STANAG4586_HOST}:${STANAG4586_PORT:-4586}" || echo "will prompt for address" ;;
        aisstream)
            [[ "${AISSTREAM_KEY:-}" ]] && echo "API key configured" || echo "will prompt for API key" ;;
        sitaware-hq-nvg)
            if [[ "${SITAWARE_HQ_NVG_ENABLE:-}" == "1" ]]; then
                echo "${SITAWARE_HQ_NVG_BIND:-127.0.0.1}:${SITAWARE_HQ_NVG_PORT:-8088}${SITAWARE_HQ_NVG_PATH:-/nvg}"
            else
                echo "SITAWARE_HQ_NVG_ENABLE=0"
            fi ;;
        sitaware)
            if [[ "${SITAWARE_URL:-}" ]]; then
                [[ "${SITAWARE_URL_FALLBACK:-}" ]] && echo "${SITAWARE_URL} (+fallback)" || echo "${SITAWARE_URL}"
            else
                echo "will prompt for address"
            fi ;;
        cot-tcp)
            if [[ "${TAK_HOST:-}" ]]; then
                [[ "${TAK_HOST_FALLBACK:-}" ]] && echo "${TAK_HOST}:${TAK_PORT:-8087} (+fallback)" || echo "${TAK_HOST}:${TAK_PORT:-8087}"
            else
                echo "will prompt for address"
            fi ;;
        cot-udp-tak)
            if [[ "${TAK_UDP_HOST:-}" ]]; then
                [[ "${TAK_UDP_HOST_FALLBACK:-}" ]] && echo "${TAK_UDP_HOST}:${TAK_UDP_PORT:-8087} (+fallback)" || echo "${TAK_UDP_HOST}:${TAK_UDP_PORT:-8087}"
            else
                echo "will prompt for address"
            fi ;;
        *)        echo "" ;;
    esac
}

is_bridge_pid() {
    local pid="$1" expected_script="${2:-}" arg
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && [[ -r "/proc/$pid/cmdline" ]] || return 1
    while IFS= read -r -d '' arg; do
        if [[ -n "$expected_script" ]]; then
            [[ "$arg" == "$COMPOSE_DIR/$expected_script" ]] && return 0
        elif [[ "$arg" == "$COMPOSE_DIR/"* ]]; then
            return 0
        fi
    done < "/proc/$pid/cmdline"
    return 1
}

is_running() {
    local f="$PID_DIR/$1.pid" pid
    [[ -f "$f" ]] || return 1
    IFS= read -r pid < "$f"
    is_bridge_pid "$pid" "${2:-}"
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
    if is_running "$name" "$script"; then
        printf "  ${DIM}[skip]${R}  %-16s already running (pid %s)\n" "$name" "$(cat "$pid_file")"
        return
    fi
    rm -f "$pid_file"
    "$PYTHON" "$COMPOSE_DIR/$script" "$@" >> "$LOG_DIR/$name.log" 2>&1 &
    echo $! > "$pid_file"
    printf "  ${GREEN}[start]${R} %-16s pid %s\n" "$name" "$!"
}

launch() {
    local name="$1"
    case "$name" in

        zenoh)
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

        airplaneslive)
            _start airplaneslive bridges/airplaneslive_adsb_bridge.py
            ;;

        adsblol)
            _start adsblol bridges/adsblol_bridge.py
            ;;

        aisstream)
            if [[ -z "${AISSTREAM_KEY:-}" ]]; then
                local ais_key
                _prompt_secret "AISstream API key" ais_key
                if [[ -z "$ais_key" ]]; then
                    printf "  ${YELLOW}[skip]${R}  aisstream        no API key entered\n"
                    return
                fi
                export AISSTREAM_KEY="$ais_key"
            fi
            _start aisstream bridges/aisstream_ws_bridge.py
            ;;

        aprs)
            _start aprs bridges/aprsis_bridge.py
            ;;

        openmeteo)
            _start openmeteo bridges/openmeteo_forecast_bridge.py
            ;;

        meteolt)
            _start meteolt bridges/meteolt_forecast_bridge.py
            ;;

        asterix-udp)
            _start asterix-udp bridges/asterix_udp_bridge.py
            ;;

        asterix-cat10|asterix-cat20|asterix-cat21|asterix-cat34|asterix-cat48)
            local cat_num="${name#asterix-cat}"
            local port_var="CAT${cat_num}_PORT" tcp_var="CAT${cat_num}_TCP"
            local ast_args=()
            if asterix_category_uses_raw "$cat_num"; then
                ast_args=(--zenoh-raw)
            else
                ast_args=(--port "${!port_var}")
                [[ "${!tcp_var:-}" == "1" ]] && ast_args+=(--tcp)
            fi
            _start "$name" "protocols/asterix_cat${cat_num}.py" "${ast_args[@]}"
            ;;

        asterix-cat62)
            if asterix_category_uses_raw 62; then
                _start asterix-cat62 protocols/asterix_cat62.py --zenoh-raw
            elif [[ "${CAT62_UDP:-}" == "1" ]]; then
                _start asterix-cat62 protocols/asterix_cat62.py --udp --port "${CAT62_PORT:-50062}"
            elif [[ "${CAT62_HOST:-${RADAR_HOST:-}}" ]]; then
                _start asterix-cat62 protocols/asterix_cat62.py \
                    --host "${CAT62_HOST:-${RADAR_HOST:-}}" \
                    --port "${CAT62_PORT:-${RADAR_PORT:-50062}}"
            else
                _start asterix-cat62 protocols/asterix_cat62.py --zenoh-raw
            fi
            ;;

        link16)
            if [[ "${LINK16_ZENOH_RAW:-}" == "1" ]]; then
                _start link16 protocols/link16.py --zenoh-raw --raw-topic "${LINK16_RAW_TOPIC:-}"
            else
                _start link16 protocols/link16.py --port "$LINK16_PORT"
            fi
            ;;

        mavlink)
            if [[ "${MAVLINK_ZENOH_RAW:-}" == "1" ]]; then
                _start mavlink protocols/mavlink.py --zenoh-raw --raw-topic "${MAVLINK_RAW_TOPIC:-}"
            else
                local tmav=(); [[ "${MAVLINK_TCP:-}" == "1" ]] && tmav=(--tcp)
                _start mavlink protocols/mavlink.py --port "$MAVLINK_PORT" "${tmav[@]}"
            fi
            ;;

        opendroneid)
            _start opendroneid protocols/opendroneid.py
            ;;

        dji-cloud)
            _start dji-cloud bridges/dji_cloud_api_bridge.py
            ;;

        utm-ans)
            if [[ "${UTM_ANS_API_URL:-}" ]]; then
                _start utm-ans bridges/utm_ans_bridge.py
            else
                printf "  ${YELLOW}[skip]${R}  utm-ans         set UTM_ANS_API_URL to an authorized JSON/GeoJSON feed\n"
            fi
            ;;

        vmf)
            if [[ "${VMF_ZENOH_RAW:-}" == "1" ]]; then
                _start vmf protocols/vmf.py --zenoh-raw --raw-topic "${VMF_RAW_TOPIC:-}"
            else
                local tvmf=(); [[ "${VMF_TCP:-}" == "1" ]] && tvmf=(--tcp)
                _start vmf protocols/vmf.py --port "$VMF_PORT" "${tvmf[@]}"
            fi
            ;;

        mavlink-raw)
            _start mavlink-raw bridges/mavlink_raw_bridge.py --port "$MAVLINK_RAW_PORT"
            ;;

        link16-raw)
            _start link16-raw bridges/link16_jreap_bridge.py --port "$LINK16_RAW_PORT"
            ;;

        vmf-raw)
            _start vmf-raw bridges/vmf_bridge.py --port "$VMF_RAW_PORT"
            ;;

        sapient-raw)
            _start sapient-raw bridges/sapient_flex335_bridge.py --tcp --port "${SAPIENT_RAW_PORT:-7001}"
            ;;

        stanag4586-raw)
            _start stanag4586-raw bridges/stanag4586_bridge.py --tcp --port "${STANAG4586_RAW_PORT:-4586}"
            ;;

        cap)
            _start cap protocols/cap.py
            ;;

        geojson)
            _start geojson protocols/geojson_features.py
            ;;

        ais-nmea)
            _start ais-nmea protocols/ais_nmea.py
            ;;

        spectrum)
            _start spectrum protocols/spectrum_observation.py
            ;;

        sensor-health)
            _start sensor-health protocols/sensor_health.py
            ;;

        mission-route)
            _start mission-route protocols/mission_route.py
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
            _start nffi protocols/nffi.py
            ;;

        cot-rx)
            if [[ "${COT_RX_PORT:-}" ]]; then
                _start cot-rx bridges/tak_bridge.py --listen "$COT_RX_PORT" --source tak_rx
            elif [[ "${COT_RX_HOST:-}" ]]; then
                _start cot-rx bridges/tak_bridge.py --connect "$COT_RX_HOST" --source tak_rx
            else
                printf "  ${YELLOW}[skip]${R}  cot-rx            set COT_RX_PORT or COT_RX_HOST\n"
            fi
            ;;

        sapient)
            if [[ "${SAPIENT_LISTEN_PORT:-}" ]]; then
                local sapient_args=(--listen "$SAPIENT_LISTEN_PORT" --bind "${SAPIENT_BIND:-127.0.0.1}")
                [[ "${SAPIENT_ALLOW_PEER:-}" ]] && sapient_args+=(--allow-peer "$SAPIENT_ALLOW_PEER")
                _start sapient protocols/sapient_flex335.py "${sapient_args[@]}"
                return
            fi
            if [[ -z "${SAPIENT_HOST:-}" ]]; then
                if [[ "${EFDI_NONINTERACTIVE:-}" == "1" ]]; then
                    _start sapient protocols/sapient_flex335.py --zenoh-raw --raw-topic "${SAPIENT_RAW_TOPIC:-}"
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
            _start sapient protocols/sapient_flex335.py --host "$SAPIENT_HOST" --port "${SAPIENT_PORT:-7001}"
            ;;

        stanag4586)
            if [[ "${STANAG4586_ZENOH_RAW:-}" == "1" ]]; then
                _start stanag4586 protocols/stanag4586.py --zenoh-raw --raw-topic "${STANAG4586_RAW_TOPIC:-}"
                return
            fi
            if [[ -z "${STANAG4586_HOST:-}" ]]; then
                if [[ "${EFDI_NONINTERACTIVE:-}" == "1" ]]; then
                    _start stanag4586 protocols/stanag4586.py --zenoh-raw --raw-topic "${STANAG4586_RAW_TOPIC:-}"
                    return
                fi
                local stanag_host
                _prompt_address "STANAG 4586 source" stanag_host
                if [[ -z "$stanag_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  stanag4586        no address entered\n"
                    return
                fi
                export STANAG4586_HOST="$stanag_host"
            fi
            _start stanag4586 protocols/stanag4586.py \
                --host "$STANAG4586_HOST" --port "${STANAG4586_PORT:-4586}"
            ;;

        dronuradaras)
            _start dronuradaras bridges/dronuradaras_bridge.py
            ;;

        cot-udp)
            _start cot-udp layers/cot_layer.py --udp --host 239.2.3.1 --port 6969
            ;;

        cot-udp-tak)
            local tak_udp_host="${TAK_UDP_HOST:-}"
            local tak_udp_host2="${TAK_UDP_HOST_FALLBACK:-}"
            local tak_udp_port="${TAK_UDP_PORT:-8087}"
            if [[ -z "$tak_udp_host" && -z "$tak_udp_host2" ]]; then
                _prompt_address "WinTAK/ATAK client" tak_udp_host
                if [[ -z "$tak_udp_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  cot-udp-tak     no address entered\n"
                    return
                fi
                export TAK_UDP_HOST="$tak_udp_host"
            fi
            local udp_hosts=(); [[ -n "$tak_udp_host"  ]] && udp_hosts+=(--host "$tak_udp_host")
            [[ -n "$tak_udp_host2" ]] && udp_hosts+=(--host "$tak_udp_host2")
            _start cot-udp-tak layers/cot_layer.py --udp "${udp_hosts[@]}" --port "$tak_udp_port"
            ;;

        cot-tcp)
            local tak_host="${TAK_HOST:-}"
            local tak_host2="${TAK_HOST_FALLBACK:-}"
            if [[ -z "$tak_host" && -z "$tak_host2" ]]; then
                _prompt_address "TAK Server" tak_host
                if [[ -z "$tak_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  cot-tcp         no address entered\n"
                    return
                fi
                export TAK_HOST="$tak_host"
            fi
            local tcp_hosts=(); [[ -n "$tak_host"  ]] && tcp_hosts+=(--host "$tak_host")
            [[ -n "$tak_host2" ]] && tcp_hosts+=(--host "$tak_host2")
            local tcp_args=("${tcp_hosts[@]}" --port "${TAK_PORT:-8087}")
            if [[ "${TAK_TLS:-}" == "1" ]]; then
                tcp_args+=(--tls --cert "${TAK_CERT:-}" --key "${TAK_KEY:-}" --ca "${TAK_CA:-}")
            fi
            _start cot-tcp layers/cot_layer.py "${tcp_args[@]}"
            ;;

        sitaware-hq-nvg)
            if [[ -z "${SITAWARE_HQ_NVG_USER:-}" && \
                  "${SITAWARE_HQ_NVG_ALLOW_ANONYMOUS:-}" != "1" ]]; then
                local hq_nvg_user hq_nvg_pass
                _prompt_credentials "SitaWare HQ NVG feed" hq_nvg_user hq_nvg_pass
                export SITAWARE_HQ_NVG_USER="$hq_nvg_user"
                export SITAWARE_HQ_NVG_PASS="$hq_nvg_pass"
            fi
            _start sitaware-hq-nvg bridges/nvg_bridge.py
            ;;

        track-fusion)
            _start track-fusion bridges/track_fusion_bridge.py
            ;;

    esac
}

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

# Restore the last valid selection. On first use, default to zenoh + the main
# output layers, plus ASTERIX when its input is configured.
for svc in "${SERVICES[@]}"; do sel[$svc]=0; done
restored=0
if [[ -n "$REMEMBERED_SERVICES" ]]; then
    IFS=',' read -r -a remembered_services <<< "$REMEMBERED_SERVICES"
    for remembered in "${remembered_services[@]}"; do
        # Migrate the former router-host radio service selection to the raw
        # Zenoh translator; the next save drops the obsolete name.
        [[ "$remembered" == "remote-id" ]] && remembered="opendroneid"
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
    for svc in zenoh admin-control cot-udp cot-tcp track-fusion; do sel[$svc]=1; done
    svc_ready asterix-udp && sel[asterix-udp]=1 || true
    for svc in asterix-cat10 asterix-cat20 asterix-cat21 \
               asterix-cat34 asterix-cat48 asterix-cat62; do
        svc_ready "$svc" && sel[$svc]=1 || true
    done
fi
# The web UI's native-process control plane is always kept selected so an old
# launcher-state file cannot leave Runtime Control disconnected after upgrade.
sel[admin-control]=1

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
                svc_ready "$svc" && sel[$svc]=1 || true
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
                    [[ "${sel[$svc]}" == "1" ]] && sel[$svc]=0 || sel[$svc]=1
                fi
            done
            ;;
    esac
done

# ── Start ──────────────────────────────────────────────────────────────────
printf "\n${BOLD}Starting selected services…${R}\n\n"

for svc in "${SERVICES[@]}"; do
    [[ "${sel[$svc]}" == "1" ]] || continue
    launch "$svc"
done

save_launcher_state

printf "\n${BOLD}Done.${R}  Logs → ${LOG_DIR}/   Stop → ./stop.sh\n"
