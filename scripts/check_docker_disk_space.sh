#!/usr/bin/env bash
# Shared by update.sh (before every rebuild) and health.sh (before its own
# stale-image --no-cache rebuild) — a `docker build` failing mid-way with
# "no space left on device" is the same root cause in both scripts, but
# health.sh's --no-cache rebuild is actually the *more* likely place to hit
# it: no-cache discards every reusable layer, so it needs the most free
# space of any build path in either script. Originally only update.sh had
# this guard.

# Fails with a clear message if Docker's storage is too full to build,
# reclaiming unused build cache (never data) first if that's enough on its
# own. Uses the same `fail`/`warn`/`ok` helpers from scripts/_spinner.sh —
# callers must have already sourced that.
check_docker_disk_space() {
    local min_free_mb docker_root free_mb
    min_free_mb="${EFDI_UPDATE_MIN_FREE_MB:-2048}"
    [[ "$min_free_mb" =~ ^[0-9]+$ ]] || fail "EFDI_UPDATE_MIN_FREE_MB must be a non-negative integer"
    docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    docker_root="${docker_root:-/var/lib/docker}"
    [ -d "$docker_root" ] || docker_root=/
    free_mb="$(df -Pm "$docker_root" | awk 'NR == 2 {print $4}')"
    [[ "$free_mb" =~ ^[0-9]+$ ]] || fail "Could not determine Docker storage free space"
    if (( free_mb < min_free_mb )); then
        # Build cache is pure rebuild-time savings, never data — safe to reclaim
        # automatically instead of failing every build once it piles up.
        warn "Only ${free_mb} MiB free on Docker storage; ${min_free_mb} MiB required. Reclaiming unused build cache..."
        docker builder prune -af >/dev/null 2>&1 || true
        free_mb="$(df -Pm "$docker_root" | awk 'NR == 2 {print $4}')"
    fi
    if (( free_mb < min_free_mb )); then
        docker system df 2>/dev/null || true
        warn "Reclaim images unused by containers: docker image prune -af"
        fail "Only ${free_mb} MiB free on Docker storage even after reclaiming build cache; ${min_free_mb} MiB required"
    fi
    ok "Docker storage preflight: ${free_mb} MiB free"
}
