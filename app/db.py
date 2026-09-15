from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _async_database_url(url: str) -> str:
    """Use the async PostgreSQL driver while retaining SQLite test support."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(_async_database_url(get_settings().database_url), pool_pre_ping=True)
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
            user_columns = await connection.exec_driver_sql("PRAGMA table_info(users)")
            existing_user_columns = {row[1] for row in user_columns}
            if "username" not in existing_user_columns:
                await connection.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN username TEXT NOT NULL DEFAULT ''"
                )
            if "role" not in existing_user_columns:
                await connection.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'"
                )
            if "parent_id" not in existing_user_columns:
                await connection.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN parent_id INTEGER"
                )
            await connection.exec_driver_sql(
                "UPDATE users SET username = substr(email, 1, instr(email, '@') - 1) "
                "WHERE username = ''"
            )
            columns = await connection.exec_driver_sql("PRAGMA table_info(instagram_accounts)")
            existing = {row[1] for row in columns}
            new_columns = {
                "profile_picture_url": "TEXT",
                "direct_reply_enabled": "BOOLEAN NOT NULL DEFAULT 0",
                "direct_reply_text": "TEXT NOT NULL DEFAULT ''",
                "comment_reply_enabled": "BOOLEAN NOT NULL DEFAULT 0",
                "comment_reply_text": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in new_columns.items():
                if name not in existing:
                    await connection.exec_driver_sql(
                        f"ALTER TABLE instagram_accounts ADD COLUMN {name} {definition}"
                    )
