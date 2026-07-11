# shellcheck shell=bash
# Shared colored spinner helpers — source this from non-interactive scripts.
# No whiptail dependency. Requires: set -euo pipefail already active in the caller.

R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m' C='\033[0;36m'
W='\033[1;37m' DIM='\033[2m' NC='\033[0m'
ok()   { printf "${G}  ✓${NC}  %s\n" "$*"; }
fail() { printf "${R}  ✗${NC}  %s\n" "$*"; exit 1; }
warn() { printf "${Y}  !${NC}  %s\n" "$*"; }
info() { printf "${C}  →${NC}  %s\n" "$*"; }
dim()  { printf "${DIM}     %s${NC}\n" "$*"; }

_SP_PID=""; _SP_START=0
_SP_FRAMES=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
spin_start() {
    _SP_START=$(date +%s)
    ( local i=0
      while true; do
          printf "\r  ${C}%s${NC}  %s  ${DIM}%ds${NC}" \
              "${_SP_FRAMES[$((i % ${#_SP_FRAMES[@]}))]}" "$1" "$(( $(date +%s) - _SP_START ))"
          sleep 0.1; i=$(( i + 1 ))
      done ) &
    _SP_PID=$!
}
spin_stop() {
    [ -n "$_SP_PID" ] && { kill "$_SP_PID" 2>/dev/null; wait "$_SP_PID" 2>/dev/null || true; _SP_PID=""; }
    printf "\r\033[K"
    printf "${G}  ✓${NC}  %s  ${DIM}(%ds)${NC}\n" "$1" "$(( $(date +%s) - _SP_START ))"
}
trap '[ -n "$_SP_PID" ] && kill "$_SP_PID" 2>/dev/null; printf "\r\033[K"' EXIT

banner() {
    printf "\n  ${W}EFDI${NC} ${DIM}— %s${NC}\n" "$*"
    printf "  %s\n\n" "$(printf '─%.0s' {1..48})"
}

# Run a command with a spinner; on failure, dump full output to the terminal
# (never hide errors behind a spinner, unlike a plain --quiet build).
run_spin() {
    local msg="$1" done_msg="${2:-$1}"; shift 2
    local logfile; logfile=$(mktemp)
    spin_start "$msg"
    if ! "$@" > "$logfile" 2>&1; then
        spin_stop ""
        printf "${R}  ✗  %s failed — full output:${NC}\n\n" "$msg"
        cat "$logfile"
        rm -f "$logfile"
        return 1
    fi
    spin_stop "$done_msg"
    rm -f "$logfile"
    return 0
}
