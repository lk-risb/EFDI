# shellcheck shell=bash
# Shared live deployment self-test. Source after scripts/_spinner.sh.

efdi_selftest() {
    local dc=(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE")
    local failed=0 container

    info "Self-test: checking EFDI infrastructure..."

    for service in zenoh-router zenoh-admin-db zenoh-admin zenoh-admin-proxy; do
        container="$("${dc[@]}" ps -q "$service" 2>/dev/null)"
        if [ -z "$container" ] || [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
            warn "Self-test FAILED: $service is not running"
            failed=1
        fi
    done

    container="$("${dc[@]}" ps -q zenoh-admin-db 2>/dev/null)"
    local db_user
    db_user="$(grep '^ZENOH_ADMIN_DB_USER=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    if [ -n "$container" ] && ! docker exec "$container" \
        pg_isready -U "$db_user" -d admin >/dev/null 2>&1; then
        warn "Self-test FAILED: PostgreSQL is not ready"
        failed=1
    fi

    local api_ready=0
    for _ in $(seq 1 60); do
        if "$PYTHON" - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8895/openapi.json", timeout=3).read(1)
PY
        then
            api_ready=1
            break
        fi
        sleep 2
    done
    if [ "$api_ready" -ne 1 ]; then
        warn "Self-test FAILED: zenoh-admin API is not reachable on loopback"
        failed=1
    fi

    if [ "$failed" -eq 0 ]; then
        ok "Live EFDI self-test passed"
        return 0
    fi
    return 1
}
