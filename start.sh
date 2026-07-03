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
        export "$key"
    done < "$ENV_FILE"
fi

export ZENOH_LOCAL_ENDPOINT="${ZENOH_LOCAL_ENDPOINT:-tcp/127.0.0.1:7448}"
export GOAT_CERT_DIR="${BUNDLE_DIR:-$HOME/goat-bundle}"

# Runtime state (logs, PID files) lives outside the repo — same convention as
# Zenoh's own config/certs (${POD_STATE_DIR}/zenoh/...). Keeps the working
# tree free of unversioned junk.
LOG_DIR="${POD_STATE_DIR:-$HOME/goat-moon}/logs"
PID_DIR="${POD_STATE_DIR:-$HOME/goat-moon}/.pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# ── Ensure venv ────────────────────────────────────────────────────────────
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "Creating venv at $VENV…"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet eclipse-zenoh==1.9.0
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
    asterix link16 mavlink vmf sitaware dronuradaras gps-ew
    cot-udp cot-udp-tak cot-tcp track-fusion
)

declare -A SVC_CAT=(
    [zenoh]="Infrastructure"
    [asterix]="Sensor bridges"  [link16]="Sensor bridges"
    [mavlink]="Sensor bridges"  [vmf]="Sensor bridges"
    [sitaware]="Sensor bridges" [dronuradaras]="Sensor bridges"
    [gps-ew]="Sensor bridges"
    [cot-udp]="Output layers"   [cot-udp-tak]="Output layers"
    [cot-tcp]="Output layers"   [track-fusion]="Output layers"
)

declare -A SVC_DESC=(
    [zenoh]="Zenoh message router (Docker)"
    [asterix]="ASTERIX CAT-48/34 radar tracks"
    [link16]="Link-16 JREAP-C datalink"
    [mavlink]="MAVLink UAV telemetry"
    [vmf]="VMF MIL-STD-47001C messages"
    [sitaware]="SitaWare friendly force tracking"
    [dronuradaras]="dronuradaras.lt drone detection network"
    [gps-ew]="GPS jamming/spoofing threat feed (GPSJam + custom)"
    [cot-udp]="CoT → ATAK UDP multicast 239.2.3.1:6969 (same LAN only)"
    [cot-udp-tak]="CoT → UDP unicast direct to TAK Server (crosses NetBird/VPN)"
    [cot-tcp]="CoT → TAK Server TCP"
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
        dronuradaras) return 0 ;;
        gps-ew)       return 0 ;;  # always ready; sources enabled via env vars
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

is_running() {
    local f="$PID_DIR/$1.pid"
    [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null
}

# Prompt for BOTH a LAN address and a NetBird mesh address for the same server.
# Either may be left blank. Sets two caller-provided variable names (nameref-style
# via printf -v) so both candidates can be kept as fallbacks — whichever network
# path is actually up gets used.
#   _prompt_dual_address <label> <lan_var> <netbird_var>
_prompt_dual_address() {
    local label="$1" lan_var="$2" nb_var="$3"
    local lan_in nb_in
    read -rp "$(printf "  ${BOLD}${label} LAN IP/URL${R} (same network, blank to skip): ")" lan_in
    read -rp "$(printf "  ${BOLD}${label} NetBird IP/URL${R} (mesh VPN, blank to skip): ")" nb_in
    printf -v "$lan_var" '%s' "$lan_in"
    printf -v "$nb_var" '%s' "$nb_in"
}

# ── Launch helpers ─────────────────────────────────────────────────────────
_start() {   # _start <name> <rel-script-path> [args…]
    local name="$1"; shift
    local script="$1"; shift
    local pid_file="$PID_DIR/$name.pid"
    if is_running "$name"; then
        printf "  ${DIM}[skip]${R}  %-16s already running (pid %s)\n" "$name" "$(cat "$pid_file")"
        return
    fi
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
            local t16=(); [[ "${LINK16_TCP:-}" == "1" ]] && t16=(--tcp)
            _start link16 bridges/link16_bridge.py --port "$LINK16_PORT" "${t16[@]}"
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
                local sw_lan sw_nb
                _prompt_dual_address "SitaWare Server" sw_lan sw_nb
                if [[ -z "$sw_lan" && -z "$sw_nb" ]]; then
                    printf "  ${YELLOW}[skip]${R}  sitaware        no address entered\n"
                    return
                fi
                [[ -n "$sw_lan" ]] && export SITAWARE_URL="$sw_lan"
                [[ -n "$sw_nb"  ]] && export SITAWARE_URL_FALLBACK="$sw_nb"
                # Only a NetBird address was given — it becomes primary.
                [[ -z "${SITAWARE_URL:-}" ]] && export SITAWARE_URL="$sw_nb" && unset SITAWARE_URL_FALLBACK
            fi
            local sf=(); [[ "${SITAWARE_DISCOVER:-}" == "1" ]] && sf=(--discover)
            _start sitaware bridges/sitaware_bridge.py "${sf[@]}"
            ;;

        dronuradaras)
            _start dronuradaras bridges/dronuradaras_bridge.py
            ;;

        gps-ew)
            _start gps-ew bridges/gps_ew_bridge.py
            ;;

        cot-udp)
            _start cot-udp layers/cot_layer.py --udp --host 239.2.3.1 --port 6969
            ;;

        cot-udp-tak)
            local tak_udp_host="${TAK_UDP_HOST:-}"
            local tak_udp_host2="${TAK_UDP_HOST_FALLBACK:-}"
            local tak_udp_port="${TAK_UDP_PORT:-8087}"
            if [[ -z "$tak_udp_host" && -z "$tak_udp_host2" ]]; then
                _prompt_dual_address "TAK Server" tak_udp_host tak_udp_host2
                if [[ -z "$tak_udp_host" && -z "$tak_udp_host2" ]]; then
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
                _prompt_dual_address "TAK Server" tak_host tak_host2
                if [[ -z "$tak_host" && -z "$tak_host2" ]]; then
                    printf "  ${YELLOW}[skip]${R}  cot-tcp         no address entered\n"
                    return
                fi
            fi
            local tcp_hosts=(); [[ -n "$tak_host"  ]] && tcp_hosts+=(--host "$tak_host")
            [[ -n "$tak_host2" ]] && tcp_hosts+=(--host "$tak_host2")
            _start cot-tcp layers/cot_layer.py "${tcp_hosts[@]}" --port "${TAK_PORT:-8087}"
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
