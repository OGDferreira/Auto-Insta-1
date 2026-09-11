from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    # Import models before create_all so metadata is complete.
    from . import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if engine.url.get_backend_name() == "sqlite":
            columns = await connection.exec_driver_sql("PRAGMA table_info(instagram_accounts)")
            existing = {row[1] for row in columns}
            if "profile_picture_url" not in existing:
                await connection.exec_driver_sql(
                    "ALTER TABLE instagram_accounts ADD COLUMN profile_picture_url TEXT"
                )
