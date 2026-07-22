"""MariaDB schema and UTC persistence regression tests for zenoh-admin."""

import asyncio
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.dialects.mysql import mariadb
from sqlalchemy.schema import CreateTable


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")

from api.db import DATABASE_URL, SessionLocal, engine  # noqa: E402
from api.models import AdminUser, Base, RefreshToken, UTCDateTime  # noqa: E402


def test_admin_database_url_uses_asyncmy_and_preserves_password_characters():
    assert DATABASE_URL.drivername == "mysql+asyncmy"
    assert DATABASE_URL.database == "admin"
    assert DATABASE_URL.password == os.environ["ZENOH_ADMIN_DB_PASSWORD"]


def test_every_admin_table_compiles_for_mariadb_without_postgres_types():
    dialect = mariadb.MariaDBDialect()
    statements = [
        str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    ]

    assert len(statements) == 13
    assert all(" UUID" not in statement for statement in statements)
    assert "LONGTEXT" in "\n".join(statements)
    assert "COLLATE ascii_bin" in "\n".join(statements)


def test_utc_datetime_round_trip_restores_timezone_awareness():
    datatype = UTCDateTime()
    eastern = timezone(timedelta(hours=3))
    source = datetime(2026, 7, 21, 15, 30, tzinfo=eastern)

    stored = datatype.process_bind_param(source, None)
    restored = datatype.process_result_value(stored, None)

    assert stored == datetime(2026, 7, 21, 12, 30)
    assert restored == datetime(2026, 7, 21, 12, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone"):
        datatype.process_bind_param(datetime(2026, 7, 21, 12, 30), None)


def test_live_mariadb_creates_the_complete_schema():
    if os.environ.get("EFDI_TEST_MARIADB") != "1":
        pytest.skip("set EFDI_TEST_MARIADB=1 with a disposable MariaDB instance")

    async def verify():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session:
            user = AdminUser(
                username=f"mariadb-test-{uuid4()}",
                password_hash="test-only",
                role="readonly",
            )
            session.add(user)
            await session.flush()
            session.add(
                RefreshToken(
                    user_id=user.id,
                    token_hash=uuid4().hex,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
            )
            await session.commit()
            await session.refresh(user)
            assert user.created_at.tzinfo == timezone.utc
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync: set(sync.dialect.get_table_names(sync))
            )
        await engine.dispose()
        assert names == set(Base.metadata.tables)

    asyncio.run(verify())
