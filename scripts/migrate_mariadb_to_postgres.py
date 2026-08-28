"""One-time data transfer from the old MariaDB zenoh-admin database to the new
PostgreSQL one, for the cutover described in docs/12-zenoh-admin-gui.md.

IMPORTANT — the old MariaDB container cannot still be running under its
compose name by the time you run this. `docker-compose.yml`'s `zenoh-admin-db`
service (and its fixed `container_name`) now describes the Postgres image, so
the first `docker compose up` after checking out this change stops and
replaces whatever container currently holds that name — it does not leave
the old MariaDB reachable alongside the new Postgres. Bind-mounted data is
untouched by that, though: before updating, stop the pod
(`docker compose stop zenoh-admin zenoh-admin-db`) and start a throwaway
MariaDB container of your own against the SAME preserved datadir path
(`${POD_STATE_DIR}/zenoh-admin/mariadb`), on a port/name of your choosing:

    docker run -d --name efdi-mariadb-migration-source \\
        -e MARIADB_ROOT_PASSWORD=<old-root-password> \\
        -v "${POD_STATE_DIR}/zenoh-admin/mariadb":/var/lib/mysql \\
        -p 127.0.0.1:3399:3306 \\
        mariadb:11.4.12-noble@sha256:a794d9eb009e20de605858a11f32f63b4075cbd197c650436f0e3b457e4caed7

Only then bring up the new Postgres (`docker compose up -d zenoh-admin-db`),
let it build the empty schema by starting `zenoh-admin` once, and run this
script — from inside the `zenoh-admin` container, which has both aiomysql and
asyncpg plus the api package:

    docker cp scripts/migrate_mariadb_to_postgres.py \\
        <zenoh-admin container>:/app/migrate_mariadb_to_postgres.py
    docker exec -e SRC_MARIADB_HOST=127.0.0.1 -e SRC_MARIADB_PORT=3399 \\
        -e SRC_MARIADB_USER=root -e SRC_MARIADB_PASSWORD=<old-root-password> \\
        <zenoh-admin container> python3 migrate_mariadb_to_postgres.py [--dry-run]

Then remove the throwaway `efdi-mariadb-migration-source` container — the
real datadir it pointed at is untouched and can be deleted by hand once the
migration is verified.

The target connection is read from the environment exactly as the running
app reads it (ZENOH_ADMIN_DB_USER/PASSWORD/ADDRESS) via api.db.DATABASE_URL,
so this always writes to whatever database the container is actually
configured for.

The source schema is reflected directly off the live MariaDB database rather
than imported from api.models, so this script can never be affected by the
models module now describing a Postgres-only schema (e.g. COLLATE "C").
"""

import argparse
import asyncio
import os
import sys
from datetime import timezone

from sqlalchemy import MetaData, insert, select, update, func
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from api.db import DATABASE_URL as TARGET_URL  # noqa: E402
from api.models import Base  # noqa: E402


def _source_url() -> URL:
    return URL.create(
        "mysql+aiomysql",
        username=os.environ["SRC_MARIADB_USER"],
        password=os.environ["SRC_MARIADB_PASSWORD"],
        host=os.environ.get("SRC_MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("SRC_MARIADB_PORT", "3307")),
        database=os.environ.get("SRC_MARIADB_DATABASE", "admin"),
    )


def _self_referential_columns(table):
    """Columns whose FK points back at the same table (trust_authorities.parent_id,
    issued_identities.replaced_by_id). A straight sorted_tables insert can hit a
    forward reference here — e.g. replaced_by_id names a row created later — so
    these are nulled on insert and backfilled once every row in the table exists."""
    return [c for c in table.columns if any(fk.column.table is table for fk in c.foreign_keys)]


def _normalize(value, target_column):
    """Re-attach UTC to naive datetimes (MariaDB stored them naive-UTC; the
    target's UTCDateTime type requires tz-aware input on bind and re-strips
    it), and coerce MariaDB's 0/1 ints to real booleans."""
    if value is None:
        return None
    if hasattr(value, "tzinfo") and value.tzinfo is None and hasattr(value, "astimezone"):
        return value.replace(tzinfo=timezone.utc)
    if target_column.type.python_type is bool and not isinstance(value, bool):
        return bool(value)
    return value


async def migrate(dry_run: bool) -> int:
    source_engine = create_async_engine(_source_url())
    target_engine = create_async_engine(TARGET_URL)

    source_metadata = MetaData()
    async with source_engine.connect() as conn:
        await conn.run_sync(source_metadata.reflect)

    async with target_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    mismatches = []
    print(f"{'table':<28}{'source':>10}{'target':>10}")
    for target_table in Base.metadata.sorted_tables:
        name = target_table.name
        source_table = source_metadata.tables.get(name)
        if source_table is None:
            print(f"  ! {name}: not found in source database, skipping")
            continue

        async with source_engine.connect() as conn:
            rows = (await conn.execute(select(source_table))).mappings().all()

        if not dry_run and rows:
            columns_by_name = {c.name: c for c in target_table.columns}
            self_ref_cols = _self_referential_columns(target_table)
            pk_col = list(target_table.primary_key.columns)[0]

            payload = []
            deferred = []  # (pk value, {col name: original value}) for self-referential FKs
            for row in rows:
                row_dict = dict(row)
                deferred_values = {}
                for col in self_ref_cols:
                    if row_dict.get(col.name) is not None:
                        deferred_values[col.name] = row_dict[col.name]
                        row_dict[col.name] = None
                if deferred_values:
                    deferred.append((row_dict[pk_col.name], deferred_values))
                payload.append({k: _normalize(v, columns_by_name[k]) for k, v in row_dict.items()})

            async with target_engine.begin() as conn:
                await conn.execute(insert(target_table), payload)
            if deferred:
                async with target_engine.begin() as conn:
                    for pk_value, deferred_values in deferred:
                        await conn.execute(
                            update(target_table)
                            .where(pk_col == pk_value)
                            .values(**{k: _normalize(v, columns_by_name[k]) for k, v in deferred_values.items()})
                        )

        async with target_engine.connect() as conn:
            target_count = 0 if dry_run else (
                await conn.execute(select(func.count()).select_from(target_table))
            ).scalar_one()

        source_count = len(rows)
        status = "" if dry_run or source_count == target_count else "  MISMATCH"
        print(f"  {name:<26}{source_count:>10}{target_count:>10}{status}")
        if not dry_run and source_count != target_count:
            mismatches.append(name)

    await source_engine.dispose()
    await target_engine.dispose()

    if mismatches:
        print(f"\nRow count mismatch in: {', '.join(mismatches)}", file=sys.stderr)
        return 1
    print("\ndry run — no rows written" if dry_run else "\nmigration complete, all row counts match")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report source row counts only, write nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(migrate(args.dry_run)))
