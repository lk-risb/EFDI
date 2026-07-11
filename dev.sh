#!/usr/bin/env bash
# Disposable local Postgres + env file for previewing zenoh-admin panel changes
# without touching the real pod. Admin panel only — no zenoh-router, no certs,
# no fabric connection.
#
# Usage:
#   ./dev.sh up     start the dev Postgres container, write dev.env, and
#                   (once the venv below exists) start the API in the background
#   ./dev.sh down   stop and remove the dev Postgres container, its volume,
#                   and the background API process
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/_spinner.sh
. "$SCRIPT_DIR/scripts/_spinner.sh"

CONTAINER=zenoh-admin-dev-db
VOLUME=zenoh-admin-dev-db-data
PG_USER=devuser
PG_PASSWORD=devpass
PG_DB="admin"
VENV_UVICORN="$SCRIPT_DIR/compose/zenoh-admin/.venv/bin/uvicorn"
API_PIDFILE="$SCRIPT_DIR/.dev-api.pid"
API_LOGFILE="$SCRIPT_DIR/.dev-api.log"

cmd_up() {
    if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        info "$CONTAINER already running"
    else
        docker rm -f "$CONTAINER" &>/dev/null || true
        run_spin "Starting dev Postgres" "dev Postgres started" docker run -d \
            --name "$CONTAINER" \
            --restart unless-stopped \
            -e POSTGRES_USER="$PG_USER" \
            -e POSTGRES_PASSWORD="$PG_PASSWORD" \
            -e POSTGRES_DB="$PG_DB" \
            -p 127.0.0.1:5434:5432 \
            -v "$VOLUME":/var/lib/postgresql/data \
            postgres:16-alpine \
            || fail "Could not start $CONTAINER (see output above)."

        info "Waiting for Postgres to accept connections..."
        for _ in $(seq 1 30); do
            docker exec "$CONTAINER" pg_isready -U "$PG_USER" &>/dev/null && break
            sleep 1
        done
        docker exec "$CONTAINER" pg_isready -U "$PG_USER" &>/dev/null \
            || fail "Postgres did not become ready within 30s — check: docker logs $CONTAINER"
        ok "Postgres is ready"
    fi

    cat > "$SCRIPT_DIR/dev.env" <<EOF
ZENOH_ADMIN_DB_ADDRESS=localhost:5434
ZENOH_ADMIN_DB_USER=$PG_USER
ZENOH_ADMIN_DB_PASSWORD=$PG_PASSWORD
ZENOH_ADMIN_SECRET_KEY=dev-only-not-for-production-use
ZENOH_ADMIN_FIRST_USER=admin
ZENOH_ADMIN_FIRST_PASS=devpass123
EOF
    ok "Wrote dev.env"

    if [ ! -x "$VENV_UVICORN" ]; then
        printf "\n"
        printf "  First time only — create a venv with the API's dependencies.\n"
        printf "  Uses uv (https://docs.astral.sh/uv/) to pin Python 3.11, since\n"
        printf "  newer system Pythons can fail to build asyncpg's wheel:\n\n"
        printf "    (cd compose/zenoh-admin && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python3.11 -r requirements.txt)\n\n"
        printf "  Then run ./dev.sh up again to start the API automatically.\n"
        return
    fi

    if [ -f "$API_PIDFILE" ] && kill -0 "$(cat "$API_PIDFILE")" 2>/dev/null; then
        info "API already running (PID $(cat "$API_PIDFILE"))"
    else
        rm -f "$API_PIDFILE"
        (
            cd "$SCRIPT_DIR/compose/zenoh-admin"
            set -a
            # shellcheck source=/dev/null
            . "$SCRIPT_DIR/dev.env"
            set +a
            exec "$VENV_UVICORN" api.main:app --reload --port 8890
        ) > "$API_LOGFILE" 2>&1 &
        echo "$!" > "$API_PIDFILE"

        info "Waiting for the API to come up..."
        for _ in $(seq 1 30); do
            curl -s -o /dev/null http://localhost:8890/api/branding && break
            sleep 1
        done
        curl -s -o /dev/null http://localhost:8890/api/branding \
            || fail "API did not come up within 30s — check: cat $API_LOGFILE"
        ok "API running at http://localhost:8890 (PID $(cat "$API_PIDFILE"), logs: $API_LOGFILE)"
    fi

    printf "\n"
    printf "  Run this in one more terminal:\n\n"
    printf "    (cd compose/zenoh-admin/ui && pnpm dev)\n\n"
    printf "  Then log in at the printed Vite URL (http://127.0.0.1:5174) with: admin / devpass123\n"
}

cmd_down() {
    if [ -f "$API_PIDFILE" ]; then
        PID="$(cat "$API_PIDFILE")"
        kill "$PID" 2>/dev/null && ok "Stopped API (PID $PID)" || info "API was not running"
        rm -f "$API_PIDFILE" "$API_LOGFILE"
    else
        info "API was not running"
    fi
    docker rm -f "$CONTAINER" &>/dev/null && ok "Removed $CONTAINER" || info "$CONTAINER was not running"
    docker volume rm "$VOLUME" &>/dev/null && ok "Removed volume $VOLUME" || info "Volume $VOLUME did not exist"
}

case "${1:-}" in
    up)   cmd_up ;;
    down) cmd_down ;;
    *)    fail "Usage: ./dev.sh <up|down>" ;;
esac
