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
    """建表 + 轻量迁移。

    M0 阶段没有 Alembic，用 create_all 建表；已存在的表不会补列，
    所以这里再跑一遍"缺列就加"的兜底，避免升级后启动即崩。
    """
    from sqlalchemy import text

    from .models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in ADDED_COLUMNS:
            try:
                await conn.execute(text(statement))
            except Exception:
                # 列已存在（或数据库不支持 IF NOT EXISTS）就跳过
                pass

    await _bootstrap_family()


async def _bootstrap_family() -> None:
    """建表后的数据整理。

    模型：一个家庭 = 一个登录账号（1:1）。
    主账号（owner）是系统管理员，自己占一个隐藏家庭「系统」，不参与家庭列表。

    这里负责：
    1. 保证有一个主账号（首次启动用 PARENT_PASSWORD）
    2. 主账号和家庭账号共用同一个家庭时，把主账号挪到独立的「系统」家庭
    3. 多个家庭账号共用一个家庭时，各自拆成独立家庭
    4. 没有归属的学生挂到主账号所在家庭（老数据兜底）
    """
    from sqlalchemy import select, update

    from .models import Account, Family, Student
    from .security import hash_password

    async with SessionLocal() as session:
        families = (await session.execute(select(Family).order_by(Family.id))).scalars().all()
        if not families:
            families = [Family(name="系统")]
            session.add(families[0])
            await session.flush()

        # 1) 主账号
        owner = (
            await session.execute(select(Account).where(Account.role == "owner").order_by(Account.id))
        ).scalars().first()
        if owner is None:
            owner = Account(
                family_id=families[0].id,
                username="parent",
                password_hash=hash_password(settings.parent_password),
                role="owner",
                display_name="主账号",
            )
            session.add(owner)
            await session.flush()

        # 2) 主账号独占一个「系统」家庭
        shared = (
            await session.execute(
                select(Account.id).where(
                    Account.family_id == owner.family_id, Account.id != owner.id
                )
            )
        ).first()
        if shared is not None:
            system_family = Family(name="系统")
            session.add(system_family)
            await session.flush()
            owner.family_id = system_family.id
            await session.flush()

        # 3) 家庭账号各占一个家庭
        members = (
            await session.execute(select(Account).where(Account.role == "member").order_by(Account.id))
        ).scalars().all()
        seen: dict[int, int] = {}
        for member in members:
            if member.family_id is None:
                new_family = Family(name=member.display_name or member.username)
                session.add(new_family)
                await session.flush()
                member.family_id = new_family.id
            elif member.family_id in seen:
                old = await session.get(Family, member.family_id)
                new_family = Family(name=f"{old.name}（{member.username}）" if old else member.username)
                session.add(new_family)
                await session.flush()
                member.family_id = new_family.id
            seen[member.family_id] = member.id

        # 4) 老数据兜底
        await session.execute(
            update(Student).where(Student.family_id.is_(None)).values(family_id=owner.family_id)
        )
        await session.commit()


# 后加的列：老库升级时需要补上，新库 create_all 已经建好
ADDED_COLUMNS = [
    "ALTER TABLE student ADD COLUMN level VARCHAR(8)",
    "ALTER TABLE student ADD COLUMN weekday_targets JSON",
    "ALTER TABLE student ADD COLUMN family_id INTEGER",
    "ALTER TABLE plan ADD COLUMN source VARCHAR(16) DEFAULT 'manual'",
    "ALTER TABLE video ADD COLUMN speech_ms INTEGER DEFAULT 0",
    "ALTER TABLE video ADD COLUMN speech_pct FLOAT DEFAULT 0",
    "ALTER TABLE video ADD COLUMN wpm FLOAT DEFAULT 0",
    "ALTER TABLE video ADD COLUMN level_score FLOAT",
    "ALTER TABLE video ADD COLUMN unique_words INTEGER DEFAULT 0",
    "ALTER TABLE video ADD COLUMN oov_ratio FLOAT",
    "ALTER TABLE video ADD COLUMN audio_rel VARCHAR(1024)",
    "ALTER TABLE video ADD COLUMN original_name VARCHAR(500)",
    "ALTER TABLE video ADD COLUMN language VARCHAR(8) DEFAULT 'en'",
    "ALTER TABLE video ADD COLUMN lufs FLOAT",
    "ALTER TABLE video ADD COLUMN audio_lufs FLOAT",
    "ALTER TABLE video ADD COLUMN lufs_tp FLOAT",
]
