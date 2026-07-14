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
    asterix link16 mavlink vmf sitaware nffi dronuradaras
    cot-udp cot-udp-tak cot-tcp sitaware-nvg track-fusion
)

declare -A SVC_CAT=(
    [zenoh]="Infrastructure"
    [asterix]="Sensor bridges"  [link16]="Sensor bridges"
    [mavlink]="Sensor bridges"  [vmf]="Sensor bridges"
    [sitaware]="Sensor bridges" [nffi]="Sensor bridges"
    [dronuradaras]="Sensor bridges"
    [cot-udp]="Output layers"   [cot-udp-tak]="Output layers"
    [cot-tcp]="Output layers"   [sitaware-nvg]="Output layers"
    [track-fusion]="Output layers"
)

declare -A SVC_DESC=(
    [zenoh]="Zenoh message router (Docker)"
    [asterix]="ASTERIX CAT-48/34 radar tracks"
    [link16]="Link-16 JREAP-C datalink"
    [mavlink]="MAVLink UAV telemetry"
    [vmf]="VMF MIL-STD-47001C messages"
    [sitaware]="SitaWare HQ friendly force tracking (inbound REST)"
    [nffi]="NATO NFFI friendly force XML feed (inbound)"
    [dronuradaras]="dronuradaras.lt drone detection network"
    [cot-udp]="CoT → ATAK UDP multicast 239.2.3.1:6969 (same LAN only)"
    [cot-udp-tak]="CoT → UDP unicast direct to TAK Server (crosses NetBird/VPN)"
    [cot-tcp]="CoT → TAK Server TCP"
    [sitaware-nvg]="EFDI tracks → SitaWare Edge (outbound NVG)"
    [track-fusion]="Radar/ADS-B track correlation"
)

# ── Ready check — 0=can start, 1=missing config ───────────────────────────
svc_ready() {
    case "$1" in
        zenoh|cot-udp|cot-udp-tak|cot-tcp|track-fusion)
            return 0 ;;
        asterix)  [[ "${CAT48_PORT:-}${CAT21_PORT:-}${CAT20_PORT:-}" ]] ;;
        link16)   [[ "${LINK16_PORT:-}" ]] ;;
        mavlink)  [[ "${MAVLINK_PORT:-}" ]] ;;
        vmf)          [[ "${VMF_PORT:-}" ]] ;;
        sitaware)     return 0 ;;  # always ready; prompts for server IP at launch if unset
        nffi)         [[ "${NFFI_HOST:-}" ]] ;;
        dronuradaras) return 0 ;;
        sitaware-nvg) return 0 ;;  # always ready; prompts for address+login at launch if unset
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
        sitaware-nvg)
            if [[ "${SITAWARE_NVG_URL:-}" ]]; then
                echo "${SITAWARE_NVG_URL}"
            else
                echo "will prompt for address+login"
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
                _prompt_address "TAK Server" tak_udp_host
                if [[ -z "$tak_udp_host" ]]; then
                    printf "  ${YELLOW}[skip]${R}  cot-udp-tak     no address entered\n"
                    return
                fi
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
            fi
            local tcp_hosts=(); [[ -n "$tak_host"  ]] && tcp_hosts+=(--host "$tak_host")
            [[ -n "$tak_host2" ]] && tcp_hosts+=(--host "$tak_host2")
            _start cot-tcp layers/cot_layer.py "${tcp_hosts[@]}" --port "${TAK_PORT:-8087}"
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

        track-fusion)
            _start track-fusion layers/track_fusion_layer.py
            ;;

    esac
}

# ── Interactive menu ───────────────────────────────────────────────────────
declare -A sel

# Default: zenoh + asterix (if ready) + cot-udp
for svc in "${SERVICES[@]}"; do sel[$svc]=0; done
for svc in zenoh cot-udp cot-tcp track-fusion; do sel[$svc]=1; done
svc_ready asterix && sel[asterix]=1 || true

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

while true; do
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

printf "\n${BOLD}Done.${R}  Logs → ${LOG_DIR}/   Stop → ./stop.sh\n"
