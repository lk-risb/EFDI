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
# shellcheck source=scripts/_ask.sh
. "$ROOT/scripts/_ask.sh"
# shellcheck source=scripts/reset_admin_password.sh
. "$ROOT/scripts/reset_admin_password.sh"

[ -f "$ENV_FILE" ] || fail "compose/.env not found — run ./install.sh first"
cd "$ROOT"
banner "Reinstall"

# The admin account already exists in the DB (reinstall.sh never wipes it),
# so unlike install.sh's first-boot bootstrap (_ensure_first_user in
# api/auth.py only creates a row when none exists — setting
# ZENOH_ADMIN_FIRST_PASS again here is a silent no-op), resetting it requires
# writing a fresh password hash directly. Ask up front; default is to leave
# it alone, so a routine reinstall never forces an unwanted credential change.
_RESET_ADMIN_CREDS=0
ask_yes_no _RESET_ADMIN_CREDS "Reset the WebUI admin username/password?" n
if [ "$_RESET_ADMIN_CREDS" = "1" ]; then
    _CURRENT_ADMIN_USER="$(env_value ZENOH_ADMIN_FIRST_USER)"
    ask _NEW_ADMIN_USER "Admin username" "${_CURRENT_ADMIN_USER:-admin}"
    while true; do
        ask_secret _NEW_ADMIN_PASS "New admin password (minimum 12 characters)"
        [ "${#_NEW_ADMIN_PASS}" -ge 12 ] && break
        warn "Minimum 12 characters required."
    done
fi

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
# bind-mounted state files (see docker-compose.yml). zenoh-admin runs as
# fixed uid/gid 10001 and rewrites these on every config save; group-only
# read (644) from whoever ran this script leaves the very next Save & Restart
# failing with "[Errno 13] Permission denied" — chgrp/chmod every time,
# not just when the file didn't already exist, since a prior reinstall run
# (before this fix existed) can leave it behind at the wrong perms.
for state_file in namespace-prefix data-topic-prefix; do
    path="${POD_STATE_DIR}/${state_file}"
    if [ -d "$path" ]; then
        rmdir "$path" 2>/dev/null || true
    fi
    [ -f "$path" ] || printf 'EFDI\n' >"$path"
    chgrp 10001 "$path" 2>/dev/null || true
    chmod 664 "$path" 2>/dev/null || true
done

# BUNDLE_DIR/efdi must be group-writable by the container's fixed gid 10001
# so the Certificates page can write its own identity (api/certs_bootstrap.py)
# — see update.sh for the full explanation. Best-effort.
BUNDLE_DIR="$(grep '^BUNDLE_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if [ -n "$BUNDLE_DIR" ] && [ -d "${BUNDLE_DIR}/efdi" ]; then
    chgrp 10001 "${BUNDLE_DIR}/efdi" 2>/dev/null || true
    chmod 775 "${BUNDLE_DIR}/efdi" 2>/dev/null || true
fi

# Same as BUNDLE_DIR/efdi above, for TAK credential uploads
# (api/tak_package.py) — this directory never got the same treatment here
# that install.sh gives it, so a reinstall could silently leave it
# root-owned and every TAK zip/cert upload would fail with a bare 500.
TAK_DIR="${POD_STATE_DIR}/integrations/tak"
mkdir -p "$TAK_DIR"
chgrp 10001 "$TAK_DIR" 2>/dev/null || true
chmod 775 "$TAK_DIR" 2>/dev/null || true

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

ZENOH_ADMIN_DB_USER="$(grep '^ZENOH_ADMIN_DB_USER=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
db_container="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps -q zenoh-admin-db)"
for _ in $(seq 1 60); do
    if [ -n "$db_container" ] && docker exec "$db_container" \
        pg_isready -U "$ZENOH_ADMIN_DB_USER" -d admin >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
if [ -z "$db_container" ] || ! docker exec "$db_container" \
    pg_isready -U "$ZENOH_ADMIN_DB_USER" -d admin >/dev/null 2>&1; then
    fail "PostgreSQL did not become ready"
fi
ok "PostgreSQL ready"

if [ "$_RESET_ADMIN_CREDS" = "1" ]; then
    reset_admin_password "$COMPOSE_FILE" "$ENV_FILE" "$_NEW_ADMIN_USER" "$_NEW_ADMIN_PASS" \
        || fail "Could not reset admin credentials"
    unset _NEW_ADMIN_PASS
    ok "Admin credentials reset for '$_NEW_ADMIN_USER'"
fi

scrub_admin_bootstrap_secret "$ENV_FILE" \
    || fail "Admin bootstrap credential could not be removed safely"

EFDI_NONINTERACTIVE=1 "$ROOT/start.sh" --restore
ok "Native runtime restored"

bash "$ROOT/health.sh"
