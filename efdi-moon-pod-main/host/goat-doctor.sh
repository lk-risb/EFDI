#!/usr/bin/env bash
# moon-pod — periodic health probe (host systemd timer, not a container).
#
# goat-cli is no-daemon by design (ADR 1019) and has no published container image, so the
# pod's health check runs `goat doctor --json` on the host on a timer and writes a status blob
# the operator (and the future web UI) can read. Installed + enabled by first-boot.sh.
set -euo pipefail

PROFILE_DIR="${GOAT_PROFILE_DIR:-/var/lib/goat-moon/goat-cli}"
OUT="${DOCTOR_STATUS_PATH:-/var/lib/goat-moon/doctor/status.json}"
mkdir -p "$(dirname "$OUT")"

# --json emits one NDJSON line per step; --all runs all steps regardless of earlier failures.
if GOAT_PROFILE_DIR="$PROFILE_DIR" goat doctor --all --json > "${OUT}.tmp" 2>/dev/null; then
  mv "${OUT}.tmp" "$OUT"
  echo "goat-doctor: wrote $OUT"
else
  # Keep the last good status; record the failure alongside.
  mv "${OUT}.tmp" "${OUT}.last-fail" 2>/dev/null || true
  echo "goat-doctor: probe failed (see ${OUT}.last-fail)"
fi
