#!/usr/bin/env bash
# c2_preflight.sh — one-glance readiness of the three C2 legs before a demo.
#
#   tak_layer        EFDI -> TAK Server   (egress)   process + ESTAB to TAK_PORT
#   tak-bridge       TAK  -> EFDI         (ingress)  process
#   sitaware_layer   EFDI -> SitaWare HQ  (egress)   process + LISTEN on NVG port
#
# Exits non-zero if any C2 process is down (the actionable blocker). A live
# process whose link is not yet established is reported as a warning, not a
# failure — TAK/HQ may simply not have connected yet.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/compose/.env"

envval() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1; }
TAK_PORT="$(envval TAK_PORT)"; TAK_PORT="${TAK_PORT:-8089}"
NVG_PORT="$(envval SITAWARE_HQ_NVG_PORT)"; NVG_PORT="${NVG_PORT:-8088}"

# POD_STATE_DIR defaults to compose/state (see start.sh), but a deployment
# can point it anywhere via .env or an already-exported env var — hardcoding
# compose/state here meant this script could never find a single pidfile on
# a deployment using a non-default POD_STATE_DIR, reporting every C2 leg as
# permanently DOWN regardless of whether it was actually running.
POD_STATE_DIR="${POD_STATE_DIR:-$(envval POD_STATE_DIR)}"
POD_STATE_DIR="${POD_STATE_DIR:-$ROOT/compose/state}"
PID_DIR="$POD_STATE_DIR/.pids"

GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
rc=0

alive() {  # pidfile -> 0 if the recorded PID is running
    local f="$PID_DIR/$1.pid" pid
    [ -f "$f" ] || return 1
    pid="$(cat "$f" 2>/dev/null)"
    [ -n "$pid" ] && [ -d "/proc/$pid" ]
}

sock() { ss -tnH "$@" 2>/dev/null; }

row() {  # name  direction  up(0/1)  link-note
    local name="$1" dir="$2" up="$3" note="$4"
    if [ "$up" = 1 ]; then
        printf "  ${GREEN}●${RST} %-11s ${DIM}%-22s${RST} %s\n" "$name" "$dir" "$note"
    else
        printf "  ${RED}○${RST} %-11s ${DIM}%-22s${RST} ${RED}DOWN${RST} %s\n" "$name" "$dir" "$note"
        rc=1
    fi
}

echo "C2 preflight — $(date '+%Y-%m-%d %H:%M:%S')"

# tak_layer: process + an ESTABlished connection to the TAK port
if alive tak_layer; then
    if sock state established "( dport = :$TAK_PORT )" | grep -q .; then
        note="${GREEN}ESTAB :$TAK_PORT${RST}"
    else
        note="${YEL}no ESTAB :$TAK_PORT yet${RST}"
    fi
    row tak_layer "EFDI -> TAK" 1 "$note"
else
    row tak_layer "EFDI -> TAK" 0 ""
fi

row tak-bridge "TAK -> EFDI" "$(alive tak-bridge && echo 1 || echo 0)" ""

# sitaware_layer: process + a LISTEN socket on the NVG feed port HQ polls
if alive sitaware_layer; then
    if sock state listening "( sport = :$NVG_PORT )" | grep -q .; then
        note="${GREEN}LISTEN :$NVG_PORT${RST}"
    else
        note="${YEL}not listening :$NVG_PORT${RST}"
    fi
    row sitaware_layer "EFDI -> SitaWare" 1 "$note"
else
    row sitaware_layer "EFDI -> SitaWare" 0 ""
fi

[ "$rc" = 0 ] && echo "${GREEN}All C2 processes up.${RST}" || echo "${RED}One or more C2 legs are down.${RST}"
exit "$rc"
