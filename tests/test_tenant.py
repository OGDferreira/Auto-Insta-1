import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import InstagramAccount, User


@pytest.mark.asyncio
async def test_account_query_isolated_by_owner():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        owner_a = User(email="a@example.com", password_hash="hash")
        owner_b = User(email="b@example.com", password_hash="hash")
        db.add_all([owner_a, owner_b])
        await db.flush()
        db.add_all(
            [
                InstagramAccount(
                    owner_id=owner_a.id,
                    instagram_user_id="ig-a",
                    username="a",
                    access_token_encrypted="encrypted",
                ),
                InstagramAccount(
                    owner_id=owner_b.id,
                    instagram_user_id="ig-b",
                    username="b",
                    access_token_encrypted="encrypted",
                ),
            ]
        )
        await db.commit()
        accounts = (
            await db.scalars(
                select(InstagramAccount).where(InstagramAccount.owner_id == owner_a.id)
            )
        ).all()
        assert [account.username for account in accounts] == ["a"]
        assert all(account.owner_id == owner_a.id for account in accounts)
    await engine.dispose()
