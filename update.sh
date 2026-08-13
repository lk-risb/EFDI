#!/usr/bin/env bash
# Fast-forward update with TAK-style cache verification and automatic recovery.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT/compose/.env"
COMPOSE_FILE="$ROOT/compose/docker-compose.yml"
PYTHON="$ROOT/compose/venv/bin/python3"

# shellcheck source=scripts/_spinner.sh
. "$ROOT/scripts/_spinner.sh"
# shellcheck source=scripts/_selftest.sh
. "$ROOT/scripts/_selftest.sh"
# shellcheck source=scripts/scrub_admin_secret.sh
. "$ROOT/scripts/scrub_admin_secret.sh"

[ -f "$ENV_FILE" ] || fail "compose/.env not found — run ./install.sh first"
[ -d "$ROOT/.git" ] || fail "Not a git repo — clone via git, not a manual download"
cd "$ROOT"

banner "Update"

branch="$(git symbolic-ref --quiet --short HEAD)" \
    || fail "Repo is in detached HEAD state — check out a branch first"
upstream="$(git rev-parse --abbrev-ref "${branch}@{upstream}" 2>/dev/null)" \
    || fail "Branch '$branch' has no upstream configured"
old_head="$(git rev-parse HEAD)"

pull_log="$(mktemp)"
spin_start "Fetching latest changes ($upstream)"
if ! git fetch --prune >"$pull_log" 2>&1; then
    spin_stop ""
    cat "$pull_log"
    rm -f "$pull_log"
    fail "git fetch failed — check network and the configured remote"
fi
if ! git merge --ff-only "$upstream" >>"$pull_log" 2>&1; then
    spin_stop ""
    cat "$pull_log"
    rm -f "$pull_log"
    fail "Fast-forward failed; preserve or move conflicting local changes and retry"
fi
rm -f "$pull_log"
spin_stop "Up to date: $(git log -1 --format='%h %s')"

if [ "$old_head" != "$(git rev-parse HEAD)" ]; then
    git --no-pager diff --stat "$old_head" HEAD
    if ! git diff --quiet "$old_head" HEAD -- update.sh; then
        info "update.sh changed — restarting from the updated version"
        exec bash "$ROOT/update.sh" "$@"
    fi
else
    dim "No changes — already up to date."
fi

chmod 600 "$ENV_FILE"
backfill() {
    local key="$1" value="$2"
    if ! grep -q "^${key}=." "$ENV_FILE"; then
        sed -i "/^${key}=/d" "$ENV_FILE"
        printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
        ok "Added $key"
    fi
}
backfill ZENOH_ADMIN_DB_USER zenoh_admin
backfill ZENOH_ADMIN_DB_PASSWORD "$(openssl rand -hex 24)"
backfill ZENOH_ADMIN_DB_ROOT_PASSWORD "$(openssl rand -hex 24)"
backfill ZENOH_ADMIN_SECRET_KEY "$(openssl rand -hex 32)"
backfill ZENOH_ADMIN_FIRST_USER admin
backfill EFDI_CONTROL_TOKEN "$(openssl rand -hex 32)"
grep -q '^ZENOH_ADMIN_FIRST_PASS=' "$ENV_FILE" \
    || printf 'ZENOH_ADMIN_FIRST_PASS=\n' >>"$ENV_FILE"

min_free_mb="${EFDI_UPDATE_MIN_FREE_MB:-2048}"
[[ "$min_free_mb" =~ ^[0-9]+$ ]] || fail "EFDI_UPDATE_MIN_FREE_MB must be a non-negative integer"
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
docker_root="${docker_root:-/var/lib/docker}"
[ -d "$docker_root" ] || docker_root=/
free_mb="$(df -Pm "$docker_root" | awk 'NR == 2 {print $4}')"
[[ "$free_mb" =~ ^[0-9]+$ ]] || fail "Could not determine Docker storage free space"
if (( free_mb < min_free_mb )); then
    docker system df 2>/dev/null || true
    fail "Only ${free_mb} MiB free on Docker storage; ${min_free_mb} MiB required"
fi
ok "Docker storage preflight: ${free_mb} MiB free"

if [ ! -x "$PYTHON" ]; then
    python3 -m venv "$ROOT/compose/venv"
fi
run_spin "Synchronizing Python dependencies" "Python dependencies synchronized" \
    "$PYTHON" -m pip install --disable-pip-version-check \
        -r "$ROOT/compose/requirements.txt" \
        -r "$ROOT/compose/zenoh-admin/requirements.txt" \
    || fail "Python dependency installation failed"

GIT_COMMIT="$(git rev-parse HEAD)"
export GIT_COMMIT
run_spin "Building updated infrastructure" "Infrastructure image built" \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build \
    || fail "Infrastructure build failed"

info "Recreating changed infrastructure without a full shutdown..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --remove-orphans \
    || fail "Infrastructure restart failed"
ok "Infrastructure restarted"

info "Restarting native bridges and layers from the saved selection..."
"$ROOT/stop.sh" native
EFDI_NONINTERACTIVE=1 "$ROOT/start.sh" --restore
ok "Native runtime restored"

scrub_admin_bootstrap_secret "$ENV_FILE" \
    || fail "Admin bootstrap credential could not be removed safely"

info "Restarting zenoh-admin-proxy to refresh the backend connection..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" restart zenoh-admin-proxy \
    || fail "zenoh-admin-proxy restart failed"
ok "zenoh-admin-proxy restarted"

printf '\n'
if ! efdi_selftest; then
    warn "Self-test failed — escalating to health.sh for automatic recovery"
    bash "$ROOT/health.sh" || fail "Health check failed — see output above"
fi

printf '\n'
printf '  %b┌────────────────────────────────────────────────┐%b\n' "$G" "$NC"
printf '  %b│%b  %bUpdate complete%b                              %b│%b\n' \
    "$G" "$NC" "$W" "$NC" "$G" "$NC"
printf '  %b└────────────────────────────────────────────────┘%b\n\n' "$G" "$NC"
printf '  %bLogs:%b  tail -f "%s"\n\n' \
    "$DIM" "$NC" "\${POD_STATE_DIR}/logs/<service>.log"
