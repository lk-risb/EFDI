"""One-time, fail-closed copy of zenoh-admin rows from PostgreSQL to MariaDB."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Mapping

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from . import models  # noqa: F401 -- registers every table with Base
from .db import Base, engine as mariadb_engine


def _postgres_url() -> URL:
    address = os.environ.get("EFDI_MIGRATION_POSTGRES_ADDRESS", "127.0.0.1:55433")
    host, separator, raw_port = address.rpartition(":")
    if not separator:
        host, raw_port = address, "5432"
    return URL.create(
        "postgresql+asyncpg",
        username=os.environ.get(
            "EFDI_MIGRATION_POSTGRES_USER", os.environ["ZENOH_ADMIN_DB_USER"]
        ),
        password=os.environ.get(
            "EFDI_MIGRATION_POSTGRES_PASSWORD", os.environ["ZENOH_ADMIN_DB_PASSWORD"]
        ),
        host=host,
        port=int(raw_port),
        database=os.environ.get("EFDI_MIGRATION_POSTGRES_DATABASE", "admin"),
    )


def _portable_row(row: Mapping) -> dict:
    return {
        key: str(value) if isinstance(value, uuid.UUID) else value
        for key, value in row.items()
    }


async def _count(connection: AsyncConnection, table) -> int:
    return int(await connection.scalar(select(func.count()).select_from(table)) or 0)


async def migrate() -> None:
    postgres_engine = create_async_engine(_postgres_url(), pool_pre_ping=True)
    try:
        async with mariadb_engine.begin() as destination:
            await destination.run_sync(Base.metadata.create_all)

        async with (
            postgres_engine.connect() as source,
            mariadb_engine.begin() as destination,
        ):
            source_table_names = {
                row[0]
                for row in (
                    await source.exec_driver_sql(
                        "select tablename from pg_tables where schemaname = 'public'"
                    )
                ).all()
            }
            nonempty = [
                table.name
                for table in Base.metadata.sorted_tables
                if await _count(destination, table)
            ]
            if nonempty:
                raise RuntimeError(
                    "MariaDB import target is not empty: " + ", ".join(nonempty)
                )

            source_counts: dict[str, int] = {}
            deferred_self_references: list[
                tuple[object, object, object, object, object]
            ] = []
            for table in Base.metadata.sorted_tables:
                if table.name not in source_table_names:
                    continue
                source_columns = [
                    row[0]
                    for row in (
                        await source.execute(
                            text(
                                "select column_name from information_schema.columns "
                                "where table_schema = 'public' and table_name = :table_name "
                                "order by ordinal_position"
                            ),
                            {"table_name": table.name},
                        )
                    ).all()
                ]
                selected_columns = [table.c[name] for name in source_columns]
                rows = [
                    _portable_row(row)
                    for row in (
                        await source.execute(select(*selected_columns))
                    ).mappings().all()
                ]
                source_counts[table.name] = len(rows)
                self_reference_columns = {
                    column
                    for column in table.columns
                    if column.name in source_columns
                    if any(
                        foreign_key.column.table is table
                        for foreign_key in column.foreign_keys
                    )
                }
                primary_key = list(table.primary_key.columns)[0]
                for row in rows:
                    for column in self_reference_columns:
                        if row[column.name] is not None:
                            deferred_self_references.append(
                                (
                                    table,
                                    primary_key,
                                    row[primary_key.name],
                                    column,
                                    row[column.name],
                                )
                            )
                            row[column.name] = None
                if rows:
                    await destination.execute(table.insert(), rows)

            for (
                table,
                primary_key,
                primary_value,
                column,
                value,
            ) in deferred_self_references:
                await destination.execute(
                    update(table)
                    .where(primary_key == primary_value)
                    .values({column.name: value})
                )

            mismatches = []
            for table in Base.metadata.sorted_tables:
                if table.name not in source_counts:
                    continue
                destination_count = await _count(destination, table)
                if destination_count != source_counts[table.name]:
                    mismatches.append(
                        f"{table.name}: PostgreSQL={source_counts[table.name]}, MariaDB={destination_count}"
                    )
            if mismatches:
                raise RuntimeError(
                    "row-count verification failed: " + "; ".join(mismatches)
                )

        total = sum(source_counts.values())
        print(
            f"[zenoh-admin] Migrated {total} rows across {len(source_counts)} tables",
            flush=True,
        )
    finally:
        await postgres_engine.dispose()
        await mariadb_engine.dispose()


if __name__ == "__main__":
    asyncio.run(migrate())
