#!/usr/bin/env bash
set -euo pipefail

INTERFACE="${1:-any}"
OUTPUT="${2:-}"
FILTER='udp dst port 50000'

command -v tcpdump >/dev/null 2>&1 || {
    echo "tcpdump is required" >&2
    exit 1
}

if [[ -n "$OUTPUT" ]]; then
    echo "Capturing UDP 50000 on ${INTERFACE} to ${OUTPUT} (Ctrl-C to stop)"
    exec sudo tcpdump -ni "$INTERFACE" -s 0 -U -w "$OUTPUT" "$FILTER"
fi

echo "Displaying UDP 50000 on ${INTERFACE} (Ctrl-C to stop)"
exec sudo tcpdump -ni "$INTERFACE" -s 0 -vv -X "$FILTER"
