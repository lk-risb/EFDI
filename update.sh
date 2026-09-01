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
# shellcheck source=scripts/cleanup_stale_pycache.sh
. "$ROOT/scripts/cleanup_stale_pycache.sh"

[ -f "$ENV_FILE" ] || fail "compose/.env not found — run ./install.sh first"
[ -d "$ROOT/.git" ] || fail "Not a git repo — clone via git, not a manual download"
cd "$ROOT"

# Same check as reinstall.sh: if install.sh never finished, the Zenoh router
# config doesn't exist yet — `docker compose up` doesn't error on a missing
# bind-mount source, Docker silently creates an empty directory there
# instead, and zenohd then crashes with a confusing "Is a directory" instead
# of the real problem. Catch it here with an actionable message.
POD_STATE_DIR="$(grep '^POD_STATE_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
ZENOH_CONFIG="${POD_STATE_DIR}/zenoh/config.json5"
if [ -d "$ZENOH_CONFIG" ]; then
    rmdir "$ZENOH_CONFIG" 2>/dev/null || true
fi
[ -f "$ZENOH_CONFIG" ] || fail "Zenoh config not found at $ZENOH_CONFIG — this deployment was never fully installed. Run ./install.sh and choose Full reconfigure first."

# Same stray-directory bug, same fix, for the two other individually
# bind-mounted state files (see docker-compose.yml) — an install predating
# either file's introduction leaves Docker's empty-directory placeholder
# behind, which then breaks every config/cert apply with "Is a directory".
for state_file in namespace-prefix data-topic-prefix; do
    path="${POD_STATE_DIR}/${state_file}"
    if [ -d "$path" ]; then
        rmdir "$path" 2>/dev/null || true
    fi
    [ -f "$path" ] || printf 'EFDI\n' >"$path"
done

# zenoh-admin bind-mounts BUNDLE_DIR/efdi :rw so the Certificates page can
# write its own uploaded/rotated identity (api/certs_bootstrap.py), but the
# container always runs as the fixed non-root uid/gid 10001 — a directory
# created before this self-heal existed (or by scripts/gen-certs.sh, which
# runs as the host operator) stays owned by the wrong uid with no group-write,
# so every cert upload/rotation fails with a bare "Operation failed" (a 500
# with no detail, since Caddy has nothing useful to relay). Safe to re-run;
# best-effort since a non-root host operator may not be able to chgrp to an
# arbitrary gid — the group must already exist or map via /etc/subgid.
BUNDLE_DIR="$(grep '^BUNDLE_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if [ -n "$BUNDLE_DIR" ] && [ -d "${BUNDLE_DIR}/efdi" ]; then
    chgrp 10001 "${BUNDLE_DIR}/efdi" 2>/dev/null || true
    chmod 775 "${BUNDLE_DIR}/efdi" 2>/dev/null || true
fi

banner "Update"

# Host OS packages — separate from the pinned container/Python/JS dependency
# versions below, and previously never touched by this script at all. Only
# apt-based hosts are supported; anything else is skipped with a warning
# rather than failing the whole update over it.
if command -v apt-get >/dev/null 2>&1; then
    _apt=(apt-get)
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || fail "Host OS update needs root or sudo — install.sh normally runs as root"
        _apt=(sudo apt-get)
    fi
    apt_log="$(mktemp)"
    spin_start "Updating host OS packages (apt)"
    if ! DEBIAN_FRONTEND=noninteractive "${_apt[@]}" update >"$apt_log" 2>&1; then
        spin_stop ""
        cat "$apt_log"; rm -f "$apt_log"
        fail "apt-get update failed — check network and configured repos"
    fi
    if ! DEBIAN_FRONTEND=noninteractive "${_apt[@]}" upgrade -y >>"$apt_log" 2>&1; then
        spin_stop ""
        cat "$apt_log"; rm -f "$apt_log"
        fail "apt-get upgrade failed — see output above"
    fi
    if grep -qE "^0 upgraded, 0 newly installed" "$apt_log"; then
        spin_stop "Host OS packages already up to date"
    else
        spin_stop "Host OS packages updated"
    fi
    rm -f "$apt_log"
    if [ -f /var/run/reboot-required ]; then
        warn "A host package update requires a reboot (see /var/run/reboot-required) — reboot when convenient, this update continues without one."
    fi
else
    warn "apt-get not found — skipping host OS package update (unsupported host OS)"
fi

branch="$(git symbolic-ref --quiet --short HEAD)" \
    || fail "Repo is in detached HEAD state — check out a branch first"
upstream="$(git rev-parse --abbrev-ref "${branch}@{upstream}" 2>/dev/null)" \
    || fail "Branch '$branch' has no upstream configured"
old_head="$(git rev-parse HEAD)"

# This pod's checkout is a deployment target, not a place for hand-edited
# tracked files — the only intended local state is .env / PID / state dirs
# (all untracked). But live troubleshooting sometimes leaves a tracked file
# hand-patched here (e.g. over SSH) ahead of the matching commit landing
# upstream, and previously that made this fast-forward hard-fail with a
# manual-intervention wall of text every single time, on every subsequent
# ./update.sh run, until someone ran `git checkout -- <files>` by hand.
# Stash any tracked modifications out of the way first so the pull always
# goes through; the stash is discarded (not restored) afterward, since a
# hand-patch on this pod is superseded, by definition, the moment the real
# commit is pulled — this pod is never the place such a change should live.
autostash=0
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    warn "Tracked files were locally modified on this pod — stashing them so the pull can proceed (they will be discarded, not restored, since this pod's checkout should always match the repo):"
    git status --porcelain --untracked-files=no
    git stash push -q -m "update.sh-autostash-$(date +%s)"
    autostash=1
fi

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
if [ "$autostash" = "1" ]; then
    warn "Discarding the stashed local modifications (superseded by the pull above)"
    git stash drop -q
fi
spin_stop "Up to date: $(git log -1 --format='%h %s')"

if [ "$old_head" != "$(git rev-parse HEAD)" ]; then
    git --no-pager diff --stat "$old_head" HEAD
    cleanup_stale_pycache "$old_head" HEAD
    cleanup_stale_service_state "$old_head" HEAD "$POD_STATE_DIR"
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
backfill ZENOH_ADMIN_DB_PORT 5433
backfill EFDI_DB_DATA_DIR "${POD_STATE_DIR}/zenoh-admin/postgres"
backfill ZENOH_ADMIN_SECRET_KEY "$(openssl rand -hex 32)"
backfill ZENOH_ADMIN_FIRST_USER admin
backfill EFDI_CONTROL_TOKEN "$(openssl rand -hex 32)"
grep -q '^ZENOH_ADMIN_FIRST_PASS=' "$ENV_FILE" \
    || printf 'ZENOH_ADMIN_FIRST_PASS=\n' >>"$ENV_FILE"

EFDI_DB_DATA_DIR="$(grep '^EFDI_DB_DATA_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
mkdir -p "$EFDI_DB_DATA_DIR"
# Same FUSE guard as install.sh — the database must never end up on the
# JuiceFS-backed POD_STATE_DIR. See docs/04-configuration.md.
_db_data_fstype="$(stat -f -c %T "$EFDI_DB_DATA_DIR" 2>/dev/null || echo unknown)"
case "$_db_data_fstype" in
    fuseblk|fuse*|juicefs)
        fail "EFDI_DB_DATA_DIR ($EFDI_DB_DATA_DIR) is on a FUSE filesystem ($_db_data_fstype). Set it to a local path in compose/.env before continuing."
        ;;
esac

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
    || { dump_service_logs "$COMPOSE_FILE" "$ENV_FILE"; fail "Infrastructure restart failed"; }
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
    EFDI_NONINTERACTIVE=1 bash "$ROOT/health.sh" || fail "Health check failed — see output above"
fi

printf '\n'
printf '  %b┌────────────────────────────────────────────────┐%b\n' "$G" "$NC"
printf '  %b│%b  %bUpdate complete%b                              %b│%b\n' \
    "$G" "$NC" "$W" "$NC" "$G" "$NC"
printf '  %b└────────────────────────────────────────────────┘%b\n\n' "$G" "$NC"
printf '  %bLogs:%b  tail -f "%s"\n\n' \
    "$DIM" "$NC" "\${POD_STATE_DIR}/logs/<service>.log"
