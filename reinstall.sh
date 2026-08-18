#!/usr/bin/env bash
# TAK-style reinstall: remove local images/containers, keep certs and data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT/compose/.env"
COMPOSE_FILE="$ROOT/compose/docker-compose.yml"

# shellcheck source=scripts/_spinner.sh
. "$ROOT/scripts/_spinner.sh"
# shellcheck source=scripts/scrub_admin_secret.sh
. "$ROOT/scripts/scrub_admin_secret.sh"

[ -f "$ENV_FILE" ] || fail "compose/.env not found — run ./install.sh first"
cd "$ROOT"
banner "Reinstall"

# reinstall.sh only removes containers/images — it assumes a prior successful
# install.sh run already wrote the Zenoh router config. If that never
# happened (an earlier install attempt was interrupted before reaching it),
# `docker compose up` doesn't error on the missing bind-mount source — Docker
# silently creates an empty DIRECTORY there instead, and zenohd then crashes
# with a confusing "Failed to load config file: Is a directory" instead of
# the real problem. Catch it here with an actionable message.
POD_STATE_DIR="$(grep '^POD_STATE_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
ZENOH_CONFIG="${POD_STATE_DIR}/zenoh/config.json5"
if [ -d "$ZENOH_CONFIG" ]; then
    # Docker's own stray artifact from a prior failed run — clear it so a
    # real install.sh run can write the actual file there.
    rmdir "$ZENOH_CONFIG" 2>/dev/null || true
fi
[ -f "$ZENOH_CONFIG" ] || fail "Zenoh config not found at $ZENOH_CONFIG — this deployment was never fully installed. Run ./install.sh (not reinstall.sh) and choose Full reconfigure first."

# Same stray-directory bug, same fix, for the two other individually
# bind-mounted state files (see docker-compose.yml).
for state_file in namespace-prefix data-topic-prefix; do
    path="${POD_STATE_DIR}/${state_file}"
    if [ -d "$path" ]; then
        rmdir "$path" 2>/dev/null || true
    fi
    [ -f "$path" ] || printf 'EFDI\n' >"$path"
done

# BUNDLE_DIR/efdi must be group-writable by the container's fixed gid 10001
# so the Certificates page can write its own identity (api/certs_bootstrap.py)
# — see update.sh for the full explanation. Best-effort.
BUNDLE_DIR="$(grep '^BUNDLE_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if [ -n "$BUNDLE_DIR" ] && [ -d "${BUNDLE_DIR}/efdi" ]; then
    chgrp 10001 "${BUNDLE_DIR}/efdi" 2>/dev/null || true
    chmod 775 "${BUNDLE_DIR}/efdi" 2>/dev/null || true
fi

info "Stopping PID-managed bridges and layers..."
"$ROOT/stop.sh" native
ok "Native runtime stopped"

run_spin "Removing infrastructure containers and local images" "Containers and local images removed" \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down --remove-orphans --rmi local \
    || fail "Could not remove the existing infrastructure"

GIT_COMMIT="$(git rev-parse HEAD 2>/dev/null || printf unknown)"
export GIT_COMMIT
run_spin "Building EFDI infrastructure" "Infrastructure image built" \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build \
    || fail "Infrastructure build failed"

run_spin "Starting EFDI infrastructure" "Infrastructure started" \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d \
    || { dump_service_logs "$COMPOSE_FILE" "$ENV_FILE"; fail "Infrastructure startup failed"; }

db_container="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps -q zenoh-admin-db)"
for _ in $(seq 1 60); do
    if [ -n "$db_container" ] && docker exec "$db_container" \
        healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
if [ -z "$db_container" ] || ! docker exec "$db_container" \
    healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; then
    fail "MariaDB did not become ready"
fi
ok "MariaDB ready"

scrub_admin_bootstrap_secret "$ENV_FILE" \
    || fail "Admin bootstrap credential could not be removed safely"

EFDI_NONINTERACTIVE=1 "$ROOT/start.sh" --restore
ok "Native runtime restored"

bash "$ROOT/health.sh"
