#!/usr/bin/env bash
# Shared by update.sh (before every rebuild) and health.sh (before its own
# stale-image --no-cache rebuild) — a `docker build` failing mid-way with
# "no space left on device" is the same root cause in both scripts.
#
# How much room a build needs depends on how much of it is cached:
#   incremental (default) — only the changed layers are written, so a fixed small
#       allowance is enough. This is what every normal update does.
#   full (`check_docker_disk_space full`) — a --no-cache rebuild rewrites every layer,
#       and the old image stays until the new containers start, so it needs about
#       twice the largest local image of this project plus 1 GiB.
# EFDI_UPDATE_MIN_FREE_MB overrides either figure. The build cache is never pruned here:
# it is what keeps updates small, and deleting it only makes the next update bigger.
#
# Uses the `fail`/`warn`/`ok` helpers from scripts/_spinner.sh — callers must have
# already sourced that, and set COMPOSE_FILE and ENV_FILE.

# Largest local image (MiB) among the images this compose project builds or uses; 0 if none exist.
_largest_project_image_mb() {
    local image bytes max=0
    while IFS= read -r image; do
        [ -n "$image" ] || continue
        bytes="$(docker image inspect -f '{{.Size}}' "$image" 2>/dev/null || true)"
        [[ "$bytes" =~ ^[0-9]+$ ]] && (( bytes / 1048576 > max )) && max=$(( bytes / 1048576 ))
    done < <(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" config --images 2>/dev/null)
    echo "$max"
}

check_docker_disk_space() {
    local mode="${1:-incremental}" required_mb docker_root free_mb largest_mb
    if [ -n "${EFDI_UPDATE_MIN_FREE_MB:-}" ]; then
        required_mb="$EFDI_UPDATE_MIN_FREE_MB"
        [[ "$required_mb" =~ ^[0-9]+$ ]] || fail "EFDI_UPDATE_MIN_FREE_MB must be a non-negative integer"
    elif [ "$mode" = "full" ]; then
        largest_mb="$(_largest_project_image_mb)"
        required_mb=$(( largest_mb * 2 + 1024 ))
        (( required_mb < 2048 )) && required_mb=2048
    else
        required_mb=1536
    fi
    docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    docker_root="${docker_root:-/var/lib/docker}"
    [ -d "$docker_root" ] || docker_root=/
    free_mb="$(df -Pm "$docker_root" | awk 'NR == 2 {print $4}')"
    [[ "$free_mb" =~ ^[0-9]+$ ]] || fail "Could not determine Docker storage free space"
    if (( free_mb < required_mb )); then
        docker system df 2>/dev/null || true
        warn "Free space deliberately, then retry: docker image prune -af removes images no container uses; docker builder prune -af also removes the build cache, which makes the next update larger."
        fail "Only ${free_mb} MiB free on Docker storage; ${required_mb} MiB needed for a ${mode} build. Set EFDI_UPDATE_MIN_FREE_MB only to override this check on purpose."
    fi
    ok "Docker storage preflight (${mode}): ${free_mb} MiB free, ${required_mb} MiB needed"
}
