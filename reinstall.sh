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
    || fail "Infrastructure startup failed"

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
