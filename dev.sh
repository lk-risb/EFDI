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
DEV_DB_PORT="${DEV_DB_PORT:-5434}"
DEV_API_PORT="${DEV_API_PORT:-8895}"
DEV_CONTROL_PORT="${DEV_CONTROL_PORT:-8896}"
VENV_UVICORN="$SCRIPT_DIR/compose/zenoh-admin/.venv/bin/uvicorn"
VENV_PYTHON="$SCRIPT_DIR/compose/zenoh-admin/.venv/bin/python"
API_PIDFILE="$SCRIPT_DIR/.dev-api.pid"
API_LOGFILE="$SCRIPT_DIR/.dev-api.log"
UI_PIDFILE="$SCRIPT_DIR/.dev-ui.pid"
UI_LOGFILE="$SCRIPT_DIR/.dev-ui.log"
CONTROL_PIDFILE="$SCRIPT_DIR/.dev-control.pid"
CONTROL_LOGFILE="$SCRIPT_DIR/.dev-control.log"
CONTROL_ENV_FILE="$SCRIPT_DIR/.dev-runtime.env"
CONTROL_STATE_DIR="$SCRIPT_DIR/.dev-state"

cmd_up() {
    if [ -f "$SCRIPT_DIR/dev.env" ]; then
        saved_api_port="$(sed -n 's/^ZENOH_ADMIN_DEV_API_PORT=//p' "$SCRIPT_DIR/dev.env" | head -n 1)"
        [ -n "$saved_api_port" ] && DEV_API_PORT="$saved_api_port"
        saved_control_port="$(sed -n 's/^EFDI_CONTROL_PORT=//p' "$SCRIPT_DIR/dev.env" | head -n 1)"
        [ -n "$saved_control_port" ] && DEV_CONTROL_PORT="$saved_control_port"
    fi
    # A running pod/child may already own the historical 5434 port. Keep the
    # preview disposable and move only its host-side port in that case.
    if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        # Reuse the port Docker actually mapped, even when an older dev.env
        # still contains the historical default and another service owns it.
        mapped_db_port="$(docker port "$CONTAINER" 5432/tcp 2>/dev/null \
            | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1)"
        [ -n "$mapped_db_port" ] && DEV_DB_PORT="$mapped_db_port"
    elif command -v ss >/dev/null \
        && ss -ltn "sport = :$DEV_DB_PORT" 2>/dev/null | grep -q LISTEN; then
        DEV_DB_PORT="${DEV_DB_PORT_FALLBACK:-55434}"
        info "Port 5434 is busy; using disposable dev Postgres port $DEV_DB_PORT"
    fi
    api_pid_valid=0
    if [ -f "$API_PIDFILE" ] && kill -0 "$(cat "$API_PIDFILE")" 2>/dev/null \
        && tr '\0' ' ' < "/proc/$(cat "$API_PIDFILE")/cmdline" | grep -q -- "--port $DEV_API_PORT"; then
        api_pid_valid=1
    fi
    if [ "$api_pid_valid" -eq 0 ]; then
        if command -v ss >/dev/null && ss -ltn "sport = :$DEV_API_PORT" 2>/dev/null | grep -q LISTEN; then
            DEV_API_PORT="${DEV_API_PORT_FALLBACK:-18895}"
        info "Port 8895 is busy; using disposable dev API port $DEV_API_PORT"
        fi
        if [ -f "$API_PIDFILE" ] && kill -0 "$(cat "$API_PIDFILE")" 2>/dev/null; then
            kill "$(cat "$API_PIDFILE")" 2>/dev/null || true
        fi
        rm -f "$API_PIDFILE"
    fi
    control_pid_valid=0
    if [ -f "$CONTROL_PIDFILE" ] && kill -0 "$(cat "$CONTROL_PIDFILE")" 2>/dev/null \
        && tr '\0' ' ' < "/proc/$(cat "$CONTROL_PIDFILE")/cmdline" | grep -q -- 'admin_control.py'; then
        control_pid_valid=1
    fi
    if [ "$control_pid_valid" -eq 0 ]; then
        if command -v ss >/dev/null && ss -ltn "sport = :$DEV_CONTROL_PORT" 2>/dev/null | grep -q LISTEN; then
            DEV_CONTROL_PORT="${DEV_CONTROL_PORT_FALLBACK:-18896}"
            info "Port 8896 is busy; using disposable dev control port $DEV_CONTROL_PORT"
        fi
        if [ -f "$CONTROL_PIDFILE" ] && kill -0 "$(cat "$CONTROL_PIDFILE")" 2>/dev/null; then
            kill "$(cat "$CONTROL_PIDFILE")" 2>/dev/null || true
        fi
        rm -f "$CONTROL_PIDFILE"
    fi
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
            -p "127.0.0.1:$DEV_DB_PORT:5432" \
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
ZENOH_ADMIN_DB_ADDRESS=localhost:$DEV_DB_PORT
ZENOH_ADMIN_DB_USER=$PG_USER
ZENOH_ADMIN_DB_PASSWORD=$PG_PASSWORD
ZENOH_ADMIN_SECRET_KEY=dev-only-not-for-production-use
ZENOH_ADMIN_FIRST_USER=admin
ZENOH_ADMIN_FIRST_PASS=devpass123
ZENOH_ADMIN_DEV_CORS_ORIGIN=http://127.0.0.1:5174
ZENOH_ADMIN_DEV_API_PORT=$DEV_API_PORT
EFDI_CONTROL_URL=http://127.0.0.1:$DEV_CONTROL_PORT
EFDI_CONTROL_PORT=$DEV_CONTROL_PORT
EOF
    ok "Wrote dev.env"

    if [ "$control_pid_valid" -eq 0 ]; then
        : > "$CONTROL_ENV_FILE"
        chmod 600 "$CONTROL_ENV_FILE"
        mkdir -p "$CONTROL_STATE_DIR"
        (
            EFDI_CONTROL_BIND=127.0.0.1 \
            EFDI_CONTROL_PORT="$DEV_CONTROL_PORT" \
            EFDI_ENV_FILE="$CONTROL_ENV_FILE" \
            POD_STATE_DIR="$CONTROL_STATE_DIR" \
            exec setsid python3 "$SCRIPT_DIR/compose/admin_control.py"
        ) > "$CONTROL_LOGFILE" 2>&1 < /dev/null &
        echo "$!" > "$CONTROL_PIDFILE"
        info "Waiting for the dev control agent..."
        for _ in $(seq 1 30); do
            curl -s -o /dev/null "http://127.0.0.1:$DEV_CONTROL_PORT/v1/catalog" && break
            sleep 1
        done
        curl -s -o /dev/null "http://127.0.0.1:$DEV_CONTROL_PORT/v1/catalog" \
            || fail "Dev control agent did not come up within 30s — check: cat $CONTROL_LOGFILE"
        ok "Dev control agent running at http://127.0.0.1:$DEV_CONTROL_PORT (PID $(cat "$CONTROL_PIDFILE"))"
    fi

    if [ ! -x "$VENV_UVICORN" ]; then
        printf "\n"
        printf "  First time only — create a venv with the API's dependencies.\n"
        printf "  Uses uv (https://docs.astral.sh/uv/) to pin Python 3.11, since\n"
        printf "  newer system Pythons can fail to build asyncpg's wheel:\n\n"
        printf "    (cd compose/zenoh-admin && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python3.11 -r requirements.txt)\n\n"
        printf "  Then run ./dev.sh up again to start the API automatically.\n"
        return
    fi
    if ! "$VENV_PYTHON" -c 'import jwt, fastapi, sqlalchemy, zenoh' >/dev/null 2>&1; then
        info "Installing missing admin API dependencies..."
        if command -v uv >/dev/null; then
            uv pip install --python "$VENV_PYTHON" -r "$SCRIPT_DIR/compose/zenoh-admin/requirements.txt" \
                || fail "Could not install admin API dependencies"
        else
            "$VENV_PYTHON" -m pip install -r "$SCRIPT_DIR/compose/zenoh-admin/requirements.txt" \
                || fail "Could not install admin API dependencies"
        fi
    fi

    if [ "$api_pid_valid" -eq 1 ] \
        && tr '\0' '\n' < "/proc/$(cat "$API_PIDFILE")/environ" | grep -Fxq "EFDI_CONTROL_URL=http://127.0.0.1:$DEV_CONTROL_PORT" \
        && curl -s -o /dev/null "http://127.0.0.1:$DEV_API_PORT/api/branding"; then
        info "API already running at http://127.0.0.1:$DEV_API_PORT (PID $(cat "$API_PIDFILE"))"
    else
        rm -f "$API_PIDFILE"
        (
            cd "$SCRIPT_DIR/compose/zenoh-admin"
            set -a
            # shellcheck source=/dev/null
            . "$SCRIPT_DIR/dev.env"
            set +a
            exec setsid "$VENV_UVICORN" api.main:app --reload --host 127.0.0.1 --port "$DEV_API_PORT"
        ) > "$API_LOGFILE" 2>&1 < /dev/null &
        echo "$!" > "$API_PIDFILE"

        info "Waiting for the API to come up..."
        for _ in $(seq 1 30); do
            curl -s -o /dev/null "http://127.0.0.1:$DEV_API_PORT/api/branding" && break
            sleep 1
        done
        curl -s -o /dev/null "http://127.0.0.1:$DEV_API_PORT/api/branding" \
            || fail "API did not come up within 30s — check: cat $API_LOGFILE"
        ok "API running at http://127.0.0.1:$DEV_API_PORT (PID $(cat "$API_PIDFILE"), logs: $API_LOGFILE)"
    fi

    if [ ! -x "$SCRIPT_DIR/compose/zenoh-admin/ui/node_modules/.bin/vite" ]; then
        printf "\n  UI dependencies are missing. Run:\n\n"
        printf "    (cd compose/zenoh-admin/ui && pnpm install)\n\n"
        return
    fi
    if [ -f "$UI_PIDFILE" ] && kill -0 "$(cat "$UI_PIDFILE")" 2>/dev/null; then
        info "Vite UI already running (PID $(cat "$UI_PIDFILE"))"
    else
        rm -f "$UI_PIDFILE"
        (
            cd "$SCRIPT_DIR/compose/zenoh-admin/ui"
            ZENOH_ADMIN_DEV_API_PORT="$DEV_API_PORT" exec setsid pnpm dev --host 127.0.0.1
        ) > "$UI_LOGFILE" 2>&1 < /dev/null &
        echo "$!" > "$UI_PIDFILE"
        info "Waiting for the Vite UI to come up..."
        for _ in $(seq 1 30); do
            curl -s -o /dev/null http://127.0.0.1:5174 && break
            sleep 1
        done
        curl -s -o /dev/null http://127.0.0.1:5174 \
            || fail "Vite UI did not come up within 30s — check: cat $UI_LOGFILE"
        ok "Vite UI running at http://127.0.0.1:5174 (PID $(cat "$UI_PIDFILE"), logs: $UI_LOGFILE)"
    fi

    printf "\n"
    printf "  Open http://127.0.0.1:5174 and log in with: admin / devpass123\n"
}

cmd_down() {
    if [ -f "$UI_PIDFILE" ]; then
        PID="$(cat "$UI_PIDFILE")"
        if kill "$PID" 2>/dev/null; then
            ok "Stopped Vite UI (PID $PID)"
        else
            info "Vite UI was not running"
        fi
        rm -f "$UI_PIDFILE" "$UI_LOGFILE"
    else
        info "Vite UI was not running"
    fi
    if [ -f "$API_PIDFILE" ]; then
        PID="$(cat "$API_PIDFILE")"
        if kill "$PID" 2>/dev/null; then
            ok "Stopped API (PID $PID)"
        else
            info "API was not running"
        fi
        rm -f "$API_PIDFILE" "$API_LOGFILE"
    else
        info "API was not running"
    fi
    if [ -f "$CONTROL_PIDFILE" ]; then
        PID="$(cat "$CONTROL_PIDFILE")"
        if kill "$PID" 2>/dev/null; then
            ok "Stopped dev control agent (PID $PID)"
        else
            info "Dev control agent was not running"
        fi
        rm -f "$CONTROL_PIDFILE" "$CONTROL_LOGFILE"
    else
        info "Dev control agent was not running"
    fi
    rm -f "$CONTROL_ENV_FILE"
    rm -rf "$CONTROL_STATE_DIR"
    if docker rm -f "$CONTAINER" &>/dev/null; then
        ok "Removed $CONTAINER"
    else
        info "$CONTAINER was not running"
    fi
    if docker volume rm "$VOLUME" &>/dev/null; then
        ok "Removed volume $VOLUME"
    else
        info "Volume $VOLUME did not exist"
    fi
}

case "${1:-}" in
    up)   cmd_up ;;
    down) cmd_down ;;
    *)    fail "Usage: ./dev.sh <up|down>" ;;
esac
