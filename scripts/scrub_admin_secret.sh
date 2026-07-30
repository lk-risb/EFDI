#!/usr/bin/env bash
# Remove the one-time admin bootstrap password after the API has created the
# first account. Recreate zenoh-admin so the password also leaves container
# metadata.

scrub_admin_bootstrap_secret() {
    local env_file="$1" ready=0
    local dc=(docker compose -f "$COMPOSE_FILE" --env-file "$env_file")

    grep -q '^ZENOH_ADMIN_FIRST_PASS=.' "$env_file" || return 0

    for _ in $(seq 1 90); do
        if "${dc[@]}" exec -T zenoh-admin python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8895/openapi.json", timeout=2).read(1)
PY
        then
            ready=1
            break
        fi
        sleep 2
    done

    if [ "$ready" -ne 1 ]; then
        echo "zenoh-admin did not become ready; refusing to erase the bootstrap password." >&2
        return 1
    fi

    sed -i 's/^ZENOH_ADMIN_FIRST_PASS=.*/ZENOH_ADMIN_FIRST_PASS=/' "$env_file"
    chmod 600 "$env_file"
    "${dc[@]}" up -d --no-deps --force-recreate zenoh-admin >/dev/null
    "${dc[@]}" restart zenoh-admin-proxy >/dev/null
}
