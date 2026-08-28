import os

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DB_USER = os.environ["ZENOH_ADMIN_DB_USER"]
DB_PASSWORD = os.environ["ZENOH_ADMIN_DB_PASSWORD"]
_ADDRESS = os.environ.get("ZENOH_ADMIN_DB_ADDRESS", "zenoh-admin-db:5433")
DB_HOST, separator, _port = _ADDRESS.rpartition(":")
if not separator:
    DB_HOST, _port = _ADDRESS, "5433"
DB_PORT = int(_port)

DATABASE_URL = URL.create(
    "postgresql+asyncpg",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database="admin",
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        yield session
