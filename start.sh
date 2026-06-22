#!/usr/bin/env bash
# start.sh — interactive EFDI service launcher
# Usage: ./start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/compose/bridge"
ENV_FILE="$SCRIPT_DIR/compose/.env"
VENV="$BRIDGE_DIR/venv"
LOG_DIR="$SCRIPT_DIR/logs"
PID_DIR="$SCRIPT_DIR/.pids"

mkdir -p "$LOG_DIR" "$PID_DIR"

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
    asterix link16 mavlink vmf cot-rx sitaware
    cot-udp cot-tcp track-fusion
    airplaneslive aisstream aprs fr24 opensky
    openmeteo meteolt yrno osm n2yo purpleair windy here-traffic notam
)

declare -A SVC_CAT=(
    [zenoh]="Infrastructure"
    [asterix]="Sensor bridges"    [link16]="Sensor bridges"
    [mavlink]="Sensor bridges"    [vmf]="Sensor bridges"
    [cot-rx]="Sensor bridges"     [sitaware]="Sensor bridges"
    [cot-udp]="Output layers"     [cot-tcp]="Output layers"
    [track-fusion]="Output layers"
    [airplaneslive]="Open-API data"  [aisstream]="Open-API data"
    [aprs]="Open-API data"           [fr24]="Open-API data"
    [opensky]="Open-API data"        [openmeteo]="Open-API data"
    [meteolt]="Open-API data"        [yrno]="Open-API data"
    [osm]="Open-API data"            [n2yo]="Open-API data"
    [purpleair]="Open-API data"      [windy]="Open-API data"
    [here-traffic]="Open-API data"   [notam]="Open-API data"
)

declare -A SVC_DESC=(
    [zenoh]="Zenoh message router (Docker)"
    [asterix]="ASTERIX CAT-48/34 radar tracks"
    [link16]="Link-16 JREAP-C datalink"
    [mavlink]="MAVLink UAV telemetry"
    [vmf]="VMF MIL-STD-47001C messages"
    [cot-rx]="CoT inbound receiver"
    [sitaware]="SitaWare friendly force tracking"
    [cot-udp]="CoT → ATAK UDP multicast 239.2.3.1:6969"
    [cot-tcp]="CoT → TAK Server TCP"
    [track-fusion]="Radar/ADS-B track correlation"
    [airplaneslive]="ADS-B (Airplanes.Live)"
    [aisstream]="AIS vessel tracking (AISStream)"
    [aprs]="APRS amateur radio positions"
    [fr24]="FlightRadar24 live flights"
    [opensky]="OpenSky Network ADS-B"
    [openmeteo]="Open-Meteo weather forecast"
    [meteolt]="Lithuanian meteo forecast"
    [yrno]="Yr.no weather forecast"
    [osm]="OpenStreetMap overpass POIs"
    [n2yo]="N2YO satellite positions"
    [purpleair]="PurpleAir air quality"
    [windy]="Windy weather forecast"
    [here-traffic]="HERE Maps real-time traffic"
    [notam]="ICAO NOTAM flight advisories"
)

# ── Ready check — 0=can start, 1=missing config ───────────────────────────
svc_ready() {
    case "$1" in
        zenoh|cot-udp|cot-tcp|track-fusion|airplaneslive|aprs|opensky|openmeteo|meteolt|yrno|osm)
            return 0 ;;
        asterix)      [[ "${CAT48_PORT:-}${CAT21_PORT:-}${CAT20_PORT:-}" ]] ;;
        link16)       [[ "${LINK16_PORT:-}" ]] ;;
        mavlink)      [[ "${MAVLINK_PORT:-}" ]] ;;
        vmf)          [[ "${VMF_PORT:-}" ]] ;;
        cot-rx)       [[ "${COT_RX_PORT:-}${COT_RX_HOST:-}" ]] ;;
        sitaware)     [[ "${SITAWARE_URL:-}" ]] ;;
        aisstream)    [[ "${AISSTREAM_KEY:-}" ]] ;;
        fr24)         [[ "${FR24_KEY:-}" ]] ;;
        n2yo)         [[ "${N2YO_KEY:-}" ]] ;;
        purpleair)    [[ "${PURPLEAIR_KEY:-}" ]] ;;
        windy)        [[ "${WINDY_KEY:-}" ]] ;;
        here-traffic) [[ "${HERE_KEY:-}" ]] ;;
        notam)        [[ "${ICAO_NOTAM_KEY:-}" ]] ;;
        *)            return 0 ;;
    esac
}

# Short config note shown in status column when not ready
svc_hint() {
    case "$1" in
        asterix)      echo "CAT48_PORT=${CAT48_PORT:-not set}" ;;
        link16)       echo "LINK16_PORT not set" ;;
        mavlink)      echo "MAVLINK_PORT not set" ;;
        vmf)          echo "VMF_PORT not set" ;;
        cot-rx)       echo "COT_RX_PORT/HOST not set" ;;
        sitaware)     echo "SITAWARE_URL not set" ;;
        aisstream)    echo "AISSTREAM_KEY not set" ;;
        fr24)         echo "FR24_KEY not set" ;;
        n2yo)         echo "N2YO_KEY not set" ;;
        purpleair)    echo "PURPLEAIR_KEY not set" ;;
        windy)        echo "WINDY_KEY not set" ;;
        here-traffic) echo "HERE_KEY not set" ;;
        notam)        echo "ICAO_NOTAM_KEY not set" ;;
        cot-tcp)      echo "${TAK_HOST:-127.0.0.1}:${TAK_PORT:-8087}" ;;
        *)            echo "" ;;
    esac
}

is_running() {
    local f="$PID_DIR/$1.pid"
    [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null
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

        cot-rx)
            if [[ "${COT_RX_PORT:-}" ]]; then
                _start cot-rx layers/cot_receiver_bridge.py --listen "$COT_RX_PORT"
            else
                _start cot-rx layers/cot_receiver_bridge.py --connect "$COT_RX_HOST"
            fi
            ;;

        sitaware)
            local sf=(); [[ "${SITAWARE_DISCOVER:-}" == "1" ]] && sf=(--discover)
            _start sitaware bridges/sitaware_bridge.py "${sf[@]}"
            ;;

        cot-udp)
            _start cot-udp layers/cot_layer.py --udp --host 239.2.3.1 --port 6969
            ;;

        cot-tcp)
            _start cot-tcp layers/cot_layer.py --host "${TAK_HOST:-127.0.0.1}" --port "${TAK_PORT:-8087}"
            ;;

        track-fusion)
            _start track-fusion layers/track_fusion_layer.py
            ;;

        airplaneslive)
            _start airplaneslive bridges/airplaneslive_adsb_bridge.py ;;
        aisstream)
            _start aisstream bridges/aisstream_ws_bridge.py --apikey "$AISSTREAM_KEY" ;;
        aprs)
            _start aprs bridges/aprsis_bridge.py ;;
        fr24)
            _start fr24 bridges/fr24_live_bridge.py --key "$FR24_KEY" ;;
        opensky)
            _start opensky bridges/opensky_states_bridge.py ;;
        openmeteo)
            _start openmeteo bridges/openmeteo_forecast_bridge.py ;;
        meteolt)
            _start meteolt bridges/meteolt_forecast_bridge.py ;;
        yrno)
            _start yrno bridges/yrno_forecast_bridge.py ;;
        osm)
            _start osm bridges/osm_overpass_bridge.py ;;
        n2yo)
            _start n2yo bridges/n2yo_satpos_bridge.py --key "$N2YO_KEY" ;;
        purpleair)
            _start purpleair bridges/purpleair_sensor_bridge.py --key "$PURPLEAIR_KEY" ;;
        windy)
            _start windy bridges/windy_forecast_bridge.py --key "$WINDY_KEY" ;;
        here-traffic)
            _start here-traffic bridges/here_traffic_bridge.py --key "$HERE_KEY" ;;
        notam)
            _start notam bridges/icao_notam_bridge.py --key "$ICAO_NOTAM_KEY" ;;
    esac
}

# ── Interactive menu ───────────────────────────────────────────────────────
declare -A sel

# Default: zenoh + asterix (if ready) + cot-udp
for svc in "${SERVICES[@]}"; do sel[$svc]=0; done
for svc in zenoh cot-udp; do sel[$svc]=1; done
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
