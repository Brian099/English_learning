from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

# 默认 SQLite（单文件、零运维）；设 DATABASE_URL=postgresql+asyncpg://... 即可切 Postgres
_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=not _is_sqlite,
    echo=False,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover
        """SQLite 的三个必要设置。

        - journal_mode=WAL：读写不互相阻塞，后台写事件时前端仍可查询
        - synchronous=NORMAL：WAL 下足够安全，写入快很多
        - busy_timeout：并发写时等待而不是立刻报 database is locked
        - foreign_keys：SQLite 默认不校验外键，必须显式打开
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """建表。M0 阶段直接 create_all；后续引入 Alembic 时替换这里。"""
    from .models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
