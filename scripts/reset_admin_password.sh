#!/usr/bin/env bash
# Reset (or create) the WebUI admin/superadmin account's password directly in
# the database. Needed because api/auth.py's _ensure_first_user() bootstrap
# only creates the account when none exists yet (see its own comment) —
# setting ZENOH_ADMIN_FIRST_PASS in .env again is a silent no-op once the
# account already exists, so a real reset has to write a fresh password hash
# straight into admin_users. Used by reinstall.sh's reset prompt and
# health.sh's troubleshooting menu — one implementation, tested once.
#
# Runs entirely inside the already-running zenoh-admin container through its
# own SQLAlchemy models (dialect-agnostic — no MariaDB/PostgreSQL-specific SQL
# here), with username/password passed as real Python arguments rather than
# interpolated into a SQL string.
reset_admin_password() {  # reset_admin_password <compose_file> <env_file> <username> <password>
    local compose_file="$1" env_file="$2" username="$3" password="$4"
    local admin_container

    admin_container="$(docker compose -f "$compose_file" --env-file "$env_file" ps -q zenoh-admin)"
    if [ -z "$admin_container" ]; then
        echo "  Could not find the running zenoh-admin container" >&2
        return 1
    fi

    docker exec "$admin_container" python3 -c "
import asyncio, sys
from sqlalchemy import select
from api.db import SessionLocal
from api.deps import pwd_ctx
from api.models import AdminUser


async def main():
    username, password = sys.argv[1], sys.argv[2]
    password_hash = pwd_ctx.hash(password)
    async with SessionLocal() as db:
        result = await db.execute(select(AdminUser).where(AdminUser.username == username))
        user = result.scalar_one_or_none()
        if user is None:
            db.add(AdminUser(
                username=username,
                password_hash=password_hash,
                role='superadmin',
                created_by='reset',
            ))
        else:
            user.password_hash = password_hash
            user.failed_logins = 0
            user.locked_until = None
        await db.commit()


asyncio.run(main())
" "$username" "$password" || {
        echo "  Could not reset the admin password in the database" >&2
        return 1
    }
}
