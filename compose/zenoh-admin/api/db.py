import os
import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

POSTGRES_USER = os.environ["ZENOH_ADMIN_DB_USER"]
POSTGRES_PASSWORD = os.environ["ZENOH_ADMIN_DB_PASSWORD"]
_ADDRESS = os.environ.get("ZENOH_ADMIN_DB_ADDRESS", "zenoh-admin-db:5432")
POSTGRES_HOST, _, _port = _ADDRESS.partition(":")
POSTGRES_PORT = int(_port) if _port else 5432

DATABASE_URL = (
    f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/admin"
)


async def ensure_database():
    """Create the 'admin' database if it doesn't exist."""
    conn = await asyncpg.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'admin'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE admin")
            print("[zenoh-admin] Created 'admin' database", flush=True)
    finally:
        await conn.close()

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        yield session
