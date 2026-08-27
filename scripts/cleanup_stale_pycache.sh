#!/usr/bin/env bash
# `git merge --ff-only` (update.sh) removes tracked files a commit deleted,
# but it can't touch Python's own __pycache__/*.pyc — those were never
# tracked by git, so a .py file deleted upstream leaves its compiled
# bytecode sitting on disk indefinitely. Same for compose/state/.pids and
# compose/state/logs entries for a service whose script no longer exists.
# Harmless on its own, but confusing when investigating "is this still
# running" (a stale .pid/.log can look like evidence of a live process).

# Remove __pycache__ entries for every .py file a commit range deleted.
cleanup_stale_pycache() {
    local old_ref="$1" new_ref="$2"
    local deleted removed=0
    deleted="$(git diff --no-renames --name-only --diff-filter=D "$old_ref" "$new_ref" -- '*.py' 2>/dev/null || true)"
    [ -z "$deleted" ] && return 0

    while IFS= read -r rel_path; do
        [ -z "$rel_path" ] && continue
        local dir base stem cache_dir
        dir="$(dirname "$rel_path")"
        base="$(basename "$rel_path")"
        stem="${base%.py}"
        cache_dir="$ROOT/$dir/__pycache__"
        [ -d "$cache_dir" ] || continue
        while IFS= read -r -d '' stale; do
            rm -f -- "$stale"
            removed=$((removed + 1))
        done < <(find "$cache_dir" -maxdepth 1 -name "${stem}.cpython-*.pyc" -print0 2>/dev/null)
    done <<< "$deleted"

    [ "$removed" -gt 0 ] && info "Removed $removed stale __pycache__ file(s) for modules deleted upstream"
    return 0
}

# Remove compose/state/.pids/<name>.pid and compose/state/logs/<name>.log for
# a bridge/layer script deleted upstream — these are only ever meaningful
# while that script still exists to be started/stopped by start.sh/stop.sh.
cleanup_stale_service_state() {
    local old_ref="$1" new_ref="$2"
    local pod_state_dir="$3"
    local deleted removed=0
    [ -n "$pod_state_dir" ] && [ -d "$pod_state_dir" ] || return 0

    deleted="$(git diff --no-renames --name-only --diff-filter=D "$old_ref" "$new_ref" \
        -- 'compose/bridges/*.py' 'compose/layers/*.py' 2>/dev/null || true)"
    [ -z "$deleted" ] && return 0

    while IFS= read -r rel_path; do
        [ -z "$rel_path" ] && continue
        local stem
        stem="$(basename "$rel_path" .py)"
        for stale in "$pod_state_dir/.pids/${stem}.pid" "$pod_state_dir/logs/${stem}.log"; do
            [ -f "$stale" ] || continue
            rm -f -- "$stale"
            removed=$((removed + 1))
        done
    done <<< "$deleted"

    [ "$removed" -gt 0 ] && info "Removed $removed stale .pid/.log file(s) for scripts deleted upstream"
    return 0
}
