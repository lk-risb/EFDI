#!/usr/bin/env bash
# start.sh — interactive EFDI service launcher
# Usage: ./start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/compose/bridge"
ENV_FILE="$SCRIPT_DIR/compose/.env"
VENV="$BRIDGE_DIR/venv"

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
export PYTHONPATH="$BRIDGE_DIR${PYTHONPATH:+:$PYTHONPATH}"
LOG_DIR="$POD_STATE_DIR/logs"
PID_DIR="$POD_STATE_DIR/.pids"
LAUNCHER_STATE_FILE="$POD_STATE_DIR/launcher-state.env"
mkdir -p "$LOG_DIR" "$PID_DIR"

# ── Ensure venv ────────────────────────────────────────────────────────────
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "Creating venv at $VENV…"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet -r "$BRIDGE_DIR/requirements.txt"
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
    airplaneslive aisstream aprs fr24 opensky openmeteo meteolt cmems
    here-traffic notam dronuradaras asterix link16 mavlink vmf sitaware nffi
    cot-rx sitaware-cot-rx sapient stanag4586
    cot-udp cot-udp-tak cot-tcp sitaware-nvg sitaware-hq-nvg track-fusion
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
            SITAWARE_URL|SITAWARE_URL_FALLBACK|SITAWARE_NVG_URL|COT_RX_HOST|\
            SITAWARE_COT_RX_HOST|\
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
                   SITAWARE_URL SITAWARE_URL_FALLBACK SITAWARE_NVG_URL \
                   COT_RX_HOST SITAWARE_COT_RX_HOST \
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
    [airplaneslive]="Open-data bridges" [aisstream]="Open-data bridges"
    [aprs]="Open-data bridges" [fr24]="Open-data bridges"
    [opensky]="Open-data bridges" [openmeteo]="Open-data bridges"
    [meteolt]="Open-data bridges" [cmems]="Open-data bridges"
    [here-traffic]="Open-data bridges" [notam]="Open-data bridges"
    [asterix]="Sensor bridges"  [link16]="Sensor bridges"
    [mavlink]="Sensor bridges"  [vmf]="Sensor bridges"
    [sitaware]="Sensor bridges" [nffi]="Sensor bridges"
    [dronuradaras]="Sensor bridges"
    [cot-rx]="Protocol adapters" [sitaware-cot-rx]="Protocol adapters"
    [sapient]="Protocol adapters"
    [stanag4586]="Protocol adapters"
    [cot-udp]="Output layers"   [cot-udp-tak]="Output layers"
    [cot-tcp]="Output layers"   [sitaware-nvg]="Output layers"
    [sitaware-hq-nvg]="Output layers"
    [track-fusion]="Output layers"
)

declare -A SVC_DESC=(
    [zenoh]="Zenoh message router (Docker)"
    [airplaneslive]="Airplanes.live ADS-B aircraft"
    [aisstream]="AISstream live vessel positions"
    [aprs]="APRS-IS stations, vehicles, and vessels"
    [fr24]="FlightRadar24 aircraft (API key)"
    [opensky]="OpenSky ADS-B aircraft"
    [openmeteo]="Open-Meteo weather stations"
    [meteolt]="meteo.lt weather stations"
    [cmems]="Copernicus Marine conditions"
    [here-traffic]="HERE road traffic flow"
    [notam]="ICAO active NOTAMs"
    [asterix]="ASTERIX CAT-48/34 radar tracks"
    [link16]="Link-16 JREAP-C datalink"
    [mavlink]="MAVLink UAV telemetry"
    [vmf]="VMF MIL-STD-47001C messages"
    [sitaware]="SitaWare HQ friendly force tracking (inbound REST)"
    [nffi]="NATO NFFI friendly force XML feed (inbound)"
    [dronuradaras]="dronuradaras.lt drone detection network"
    [cot-rx]="Inbound CoT / TAK Server user positions"
    [sitaware-cot-rx]="SitaWare Edge/Frontline CoT → TAK"
    [sapient]="SAPIENT sensor feed"
    [stanag4586]="STANAG 4586 UAV feed"
    [cot-udp]="CoT → ATAK UDP multicast 239.2.3.1:6969 (same LAN only)"
    [cot-udp-tak]="CoT → WinTAK/ATAK UDP unicast (crosses LAN/VPN)"
    [cot-tcp]="CoT → TAK Server TCP"
    [sitaware-nvg]="EFDI tracks → SitaWare Edge (outbound NVG)"
    [sitaware-hq-nvg]="EFDI tracks → SitaWare HQ pull feed (outbound NVG)"
    [track-fusion]="Radar/ADS-B track correlation"
)

# ── Ready check — 0=can start, 1=missing config ───────────────────────────
svc_ready() {
    case "$1" in
        zenoh|airplaneslive|aisstream|aprs|fr24|opensky|openmeteo|meteolt|\
        here-traffic|notam|dronuradaras|cot-udp|cot-udp-tak|cot-tcp|track-fusion)
            return 0 ;;
        cmems)
            "$PYTHON" -c 'import copernicusmarine' >/dev/null 2>&1 ;;
        asterix)  [[ "${CAT48_PORT:-}${CAT21_PORT:-}${CAT20_PORT:-}" ]] ;;
        link16)   [[ "${LINK16_PORT:-}" ]] ;;
        mavlink)  [[ "${MAVLINK_PORT:-}" ]] ;;
        vmf)          [[ "${VMF_PORT:-}" ]] ;;
        sitaware)     return 0 ;;  # always ready; prompts for server IP at launch if unset
        nffi)         [[ "${NFFI_HOST:-}" ]] ;;
        cot-rx)       [[ "${COT_RX_PORT:-}${COT_RX_HOST:-}" ]] ;;
        sitaware-cot-rx)
            [[ "${SITAWARE_COT_RX_HOST:-}" || ( "${SITAWARE_COT_RX_PORT:-}" && \
                "${SITAWARE_COT_RX_ALLOW_PEER:-}" ) ]] ;;
        sapient|stanag4586) return 0 ;;
        sitaware-nvg) return 0 ;;  # always ready; prompts for address+login at launch if unset
        sitaware-hq-nvg) [[ "${SITAWARE_HQ_NVG_ENABLE:-}" == "1" ]] ;;
        *)        return 0 ;;
    esac
}

# Short config note shown in status column when not ready
svc_hint() {
    case "$1" in
        asterix)  echo "CAT48_PORT=${CAT48_PORT:-not set}" ;;
        link16)   echo "LINK16_PORT not set" ;;
        mavlink)  echo "MAVLINK_PORT not set" ;;
        vmf)      echo "VMF_PORT not set" ;;
        nffi)     echo "NFFI_HOST not set" ;;
        cot-rx)
            if [[ "${COT_RX_TLS:-}" == "1" ]]; then
                echo "TAK mTLS ${COT_RX_HOST:-host not set}"
            else
                echo "COT_RX_PORT/HOST not set"
            fi ;;
        sitaware-cot-rx) echo "set HOST, or PORT + ALLOW_PEER" ;;
        sapient)
            [[ "${SAPIENT_HOST:-}" ]] && echo "${SAPIENT_HOST}:${SAPIENT_PORT:-7001}" || echo "will prompt for address" ;;
        stanag4586)
            [[ "${STANAG4586_HOST:-}" ]] && echo "${STANAG4586_HOST}:${STANAG4586_PORT:-4586}" || echo "will prompt for address" ;;
        aisstream)
            [[ "${AISSTREAM_KEY:-}" ]] && echo "API key configured" || echo "will prompt for API key" ;;
        fr24)
            [[ "${FR24_KEY:-}" ]] && echo "API key configured" || echo "will prompt for API key" ;;
        cmems)
            if ! "$PYTHON" -c 'import copernicusmarine' >/dev/null 2>&1; then
                echo "optional package missing"
            elif [[ -z "${COPERNICUSMARINE_SERVICE_USERNAME:-}" ]]; then
                echo "will prompt for login"
            else
                echo "credentials configured"
            fi ;;
        here-traffic)
            [[ "${HERE_KEY:-}" ]] && echo "API key configured" || echo "will prompt for API key" ;;
        notam)
            [[ "${ICAO_NOTAM_KEY:-}" ]] && echo "API key configured" || echo "will prompt for API key" ;;
        sitaware-nvg)
            if [[ "${SITAWARE_NVG_URL:-}" ]]; then
                echo "${SITAWARE_NVG_URL}"
            else
                echo "will prompt for address+login"
            fi ;;
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
            [[ "$arg" == "$BRIDGE_DIR/$expected_script" ]] && return 0
        elif [[ "$arg" == "$BRIDGE_DIR/"* ]]; then
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
    local addr_in
    read -rp "$(printf "  ${BOLD}${label} IP/URL${R} (blank to skip): ")" addr_in
    printf -v "$addr_var" '%s' "$addr_in"
}

# Prompt for a username and password (password input hidden via read -s).
#   _prompt_credentials <label> <user_var> <pass_var>
_prompt_credentials() {
    local label="$1" user_var="$2" pass_var="$3"
    local user_in pass_in
    read -rp "$(printf "  ${BOLD}${label} username${R}: ")" user_in
    read -rsp "$(printf "  ${BOLD}${label} password${R}: ")" pass_in
    echo
    printf -v "$user_var" '%s' "$user_in"
    printf -v "$pass_var" '%s' "$pass_in"
}

_prompt_secret() {
    local label="$1" value_var="$2" value
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
    "$PYTHON" "$BRIDGE_DIR/$script" "$@" >> "$LOG_DIR/$name.log" 2>&1 &
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

        airplaneslive)
            _start airplaneslive bridges/airplaneslive_adsb_bridge.py
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

        fr24)
            if [[ -z "${FR24_KEY:-}" ]]; then
                local fr24_key
                _prompt_secret "FlightRadar24 API key" fr24_key
                if [[ -z "$fr24_key" ]]; then
                    printf "  ${YELLOW}[skip]${R}  fr24            no API key entered\n"
                    return
                fi
                export FR24_KEY="$fr24_key"
            fi
            _start fr24 bridges/fr24_live_bridge.py
            ;;

        opensky)
            _start opensky bridges/opensky_states_bridge.py
            ;;

        openmeteo)
            _start openmeteo bridges/openmeteo_forecast_bridge.py
            ;;

        meteolt)
            _start meteolt bridges/meteolt_forecast_bridge.py
            ;;

        cmems)
            if ! "$PYTHON" -c 'import copernicusmarine' >/dev/null 2>&1; then
                printf "  ${YELLOW}[skip]${R}  cmems             optional copernicusmarine package missing\n"
                return
            fi
            if [[ -z "${COPERNICUSMARINE_SERVICE_USERNAME:-}" ]]; then
                local cmems_user cmems_pass
                _prompt_credentials "Copernicus Marine" cmems_user cmems_pass
                if [[ -z "$cmems_user" || -z "$cmems_pass" ]]; then
                    printf "  ${YELLOW}[skip]${R}  cmems             credentials not entered\n"
                    return
                fi
                export COPERNICUSMARINE_SERVICE_USERNAME="$cmems_user"
                export COPERNICUSMARINE_SERVICE_PASSWORD="$cmems_pass"
            fi
            _start cmems bridges/cmems_marine_bridge.py
            ;;

        here-traffic)
            if [[ -z "${HERE_KEY:-}" ]]; then
                local here_key
                _prompt_secret "HERE API key" here_key
                if [[ -z "$here_key" ]]; then
                    printf "  ${YELLOW}[skip]${R}  here-traffic      no API key entered\n"
                    return
                fi
                export HERE_KEY="$here_key"
            fi
            _start here-traffic bridges/here_traffic_bridge.py
            ;;

        notam)
            if [[ -z "${ICAO_NOTAM_KEY:-}" ]]; then
                local notam_key
                _prompt_secret "ICAO NOTAM API key" notam_key
                if [[ -z "$notam_key" ]]; then
                    printf "  ${YELLOW}[skip]${R}  notam             no API key entered\n"
                    return
                fi
                export ICAO_NOTAM_KEY="$notam_key"
            fi
            _start notam bridges/icao_notam_bridge.py
            ;;

        asterix)
            local ax=()
            [[ "${CAT48_PORT:-}"       ]] && ax+=(--cat48-port  "$CAT48_PORT")
            [[ "${CAT48_TCP:-}" == "1" ]] && ax+=(--cat48-tcp)
            [[ "${CAT48_RADAR_LAT:-}"  ]] && ax+=(--radar-lat   "$CAT48_RADAR_LAT")
            [[ "${CAT48_RADAR_LON:-}"  ]] && ax+=(--radar-lon   "$CAT48_RADAR_LON")
            [[ "${CAT48_RADAR_NAME:-}" ]] && ax+=(--radar-name  "$CAT48_RADAR_NAME")
            [[ "${CAT48_RADAR_SAC:-}"  ]] && ax+=(--radar-sac   "$CAT48_RADAR_SAC")
            [[ "${CAT48_RADAR_SIC:-}"  ]] && ax+=(--radar-sic   "$CAT48_RADAR_SIC")
            [[ "${CAT21_PORT:-}"       ]] && ax+=(--cat21-port  "$CAT21_PORT")
            [[ "${CAT21_TCP:-}" == "1" ]] && ax+=(--cat21-tcp)
            [[ "${CAT20_PORT:-}"       ]] && ax+=(--cat20-port  "$CAT20_PORT")
            [[ "${CAT20_TCP:-}" == "1" ]] && ax+=(--cat20-tcp)
            _start asterix bridges/asterix_bridge.py "${ax[@]}"
            ;;

        link16)
            _start link16 bridges/link16_bridge.py --port "$LINK16_PORT"
            ;;

        mavlink)
            local tmav=(); [[ "${MAVLINK_TCP:-}" == "1" ]] && tmav=(--tcp)
            _start mavlink bridges/mavlink_bridge.py --port "$MAVLINK_PORT" "${tmav[@]}"
            ;;

        vmf)
            local tvmf=(); [[ "${VMF_TCP:-}" == "1" ]] && tvmf=(--tcp)
            _start vmf bridges/vmf_bridge.py --port "$VMF_PORT" "${tvmf[@]}"
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
            local sf=(); [[ "${SITAWARE_DISCOVER:-}" == "1" ]] && sf=(--discover)
            _start sitaware bridges/sitaware_bridge.py "${sf[@]}"
            ;;

        nffi)
            local tnffi=(); [[ "${NFFI_FRAMING:-}" == "newline" ]] && tnffi=(--framing newline)
            _start nffi layers/nato_nffi_layer.py "${tnffi[@]}"
            ;;

        cot-rx)
            if [[ "${COT_RX_PORT:-}" ]]; then
                _start cot-rx layers/cot_receiver_bridge.py --listen "$COT_RX_PORT"
            elif [[ "${COT_RX_HOST:-}" ]]; then
                _start cot-rx layers/cot_receiver_bridge.py --connect "$COT_RX_HOST"
            else
                printf "  ${YELLOW}[skip]${R}  cot-rx            set COT_RX_PORT or COT_RX_HOST\n"
            fi
            ;;

        sitaware-cot-rx)
            if [[ "${SITAWARE_COT_RX_PORT:-}" ]]; then
                _start sitaware-cot-rx layers/cot_receiver_bridge.py \
                    --listen "$SITAWARE_COT_RX_PORT" \
                    --bind "${SITAWARE_COT_RX_BIND:-}" \
                    --allow-peer "${SITAWARE_COT_RX_ALLOW_PEER:-}" \
                    --source sitaware_cot_rx --no-tls --no-tak-users-only
            elif [[ "${SITAWARE_COT_RX_HOST:-}" ]]; then
                _start sitaware-cot-rx layers/cot_receiver_bridge.py \
                    --connect "$SITAWARE_COT_RX_HOST" --source sitaware_cot_rx \
                    --no-tls --no-tak-users-only
            else
                printf "  ${YELLOW}[skip]${R}  sitaware-cot-rx   set SITAWARE_COT_RX_PORT or SITAWARE_COT_RX_HOST\n"
            fi
            ;;

        sapient)
            if [[ -z "${SAPIENT_HOST:-}" ]]; then
                local sapient_host
                _prompt_address "SAPIENT source" sapient_host
                if [[ -z "$sapient_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  sapient           no address entered\n"
                    return
                fi
                export SAPIENT_HOST="$sapient_host"
            fi
            _start sapient layers/sapient_layer.py --host "$SAPIENT_HOST" --port "${SAPIENT_PORT:-7001}"
            ;;

        stanag4586)
            if [[ -z "${STANAG4586_HOST:-}" ]]; then
                local stanag_host
                _prompt_address "STANAG 4586 source" stanag_host
                if [[ -z "$stanag_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  stanag4586        no address entered\n"
                    return
                fi
                export STANAG4586_HOST="$stanag_host"
            fi
            _start stanag4586 layers/stanag4586_layer.py \
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

        sitaware-nvg)
            if [[ -z "${SITAWARE_NVG_URL:-}" ]]; then
                local nvg_addr
                _prompt_address "SitaWare Edge (NVG)" nvg_addr
                if [[ -z "$nvg_addr" ]]; then
                    printf "  ${YELLOW}[skip]${R}  sitaware-nvg    no address entered\n"
                    return
                fi
                export SITAWARE_NVG_URL="$nvg_addr"
            fi
            if [[ -z "${SITAWARE_NVG_USER:-}" ]]; then
                local nvg_user nvg_pass
                _prompt_credentials "SitaWare Edge" nvg_user nvg_pass
                export SITAWARE_NVG_USER="$nvg_user"
                export SITAWARE_NVG_PASS="$nvg_pass"
            fi
            _start sitaware-nvg layers/nato_nvg_layer.py
            ;;

        sitaware-hq-nvg)
            if [[ -z "${SITAWARE_HQ_NVG_USER:-}" && \
                  "${SITAWARE_HQ_NVG_ALLOW_ANONYMOUS:-}" != "1" ]]; then
                local hq_nvg_user hq_nvg_pass
                _prompt_credentials "SitaWare HQ NVG feed" hq_nvg_user hq_nvg_pass
                export SITAWARE_HQ_NVG_USER="$hq_nvg_user"
                export SITAWARE_HQ_NVG_PASS="$hq_nvg_pass"
            fi
            _start sitaware-hq-nvg layers/sitaware_hq_nvg_feed.py
            ;;

        track-fusion)
            _start track-fusion layers/track_fusion_layer.py
            ;;

    esac
}

# ── Interactive menu ───────────────────────────────────────────────────────
declare -A sel

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
    for svc in zenoh cot-udp cot-tcp track-fusion; do sel[$svc]=1; done
    svc_ready asterix && sel[asterix]=1 || true
fi

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
