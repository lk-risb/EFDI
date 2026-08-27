#!/usr/bin/env bash
# Reset (or create) the WebUI admin/superadmin account's password directly in
# the database. Needed because api/auth.py's _ensure_first_user() bootstrap
# only creates the account when none exists yet (see its own comment) —
# setting ZENOH_ADMIN_FIRST_PASS in .env again is a silent no-op once the
# account already exists, so a real reset has to write a fresh bcrypt hash
# straight into admin_users. Used by reinstall.sh's reset prompt and
# health.sh's troubleshooting menu — one implementation, tested once.
reset_admin_password() {  # reset_admin_password <compose_file> <env_file> <username> <password>
    local compose_file="$1" env_file="$2" username="$3" password="$4"
    local admin_container db_container hash db_root_pass

    admin_container="$(docker compose -f "$compose_file" --env-file "$env_file" ps -q zenoh-admin)"
    if [ -z "$admin_container" ]; then
        echo "  Could not find the running zenoh-admin container to hash the password" >&2
        return 1
    fi
    hash="$(docker exec "$admin_container" python3 -c "
import bcrypt, sys
print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt(rounds=12)).decode())
" "$password" 2>/dev/null)"
    if [ -z "$hash" ]; then
        echo "  Could not hash the new admin password" >&2
        return 1
    fi

    db_root_pass="$(grep '^ZENOH_ADMIN_DB_ROOT_PASSWORD=' "$env_file" | head -1 | cut -d= -f2-)"
    db_container="$(docker compose -f "$compose_file" --env-file "$env_file" ps -q zenoh-admin-db)"
    if [ -z "$db_container" ]; then
        echo "  Could not find the running zenoh-admin-db container" >&2
        return 1
    fi

    # created_at/is_active have no DB-level default (only SQLAlchemy's
    # client-side default=... on the ORM's INSERT path), so a raw INSERT that
    # omits them fails with "doesn't have a default value" — verified against
    # the real schema, not assumed.
    docker exec "$db_container" mariadb -u root -p"$db_root_pass" admin -e "
        INSERT INTO admin_users (id, username, password_hash, role, auth_provider, created_by, created_at, is_active, failed_logins, locked_until)
        VALUES (UUID(), '$username', '$hash', 'superadmin', 'local', 'reset', UTC_TIMESTAMP(), 1, 0, NULL)
        ON DUPLICATE KEY UPDATE password_hash='$hash', failed_logins=0, locked_until=NULL;
    "
}
