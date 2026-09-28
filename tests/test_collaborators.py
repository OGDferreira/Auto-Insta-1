from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app import routes
from app.db import Base
from app.models import CollaboratorConnection, CollaboratorDailyBonus, InstagramAccount, User


def request_for_path(path: str, user_id: int | None = None) -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "session": {"user_id": user_id} if user_id else {},
    })


@pytest.mark.asyncio
async def test_collaborator_history_snapshots_rates_and_awards_bonus_once(monkeypatch):
    assert routes.format_brl("1234.5") == "R$ 1.234,50"

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2025, 1, 1, 2, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(routes, "datetime", FixedDateTime)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="owner@example.com", username="owner", password_hash="hash")
        db.add(owner)
        await db.flush()
        collaborator = User(
            email=None,
            username="worker",
            password_hash="hash",
            role="collaborator",
            parent_id=owner.id,
            collaborator_rate_per_account=10,
            collaborator_daily_target=2,
            collaborator_daily_bonus=15,
        )
        db.add(collaborator)
        await db.flush()
        first_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-first",
            username="first",
            access_token_encrypted="token",
            connection_status="connected",
        )
        second_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-second",
            username="second",
            access_token_encrypted="token",
            connection_status="active",
        )
        reconnect = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-first",
            username="first",
            access_token_encrypted="token",
            connection_status="connected",
        )
        failed_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-failed",
            username="failed",
            access_token_encrypted="token",
            connection_status="error",
        )
        db.add_all([first_account, second_account, reconnect, failed_account])
        await db.flush()

        await routes._record_collaborator_connection(db, collaborator, first_account)
        collaborator.collaborator_rate_per_account = 20
        await routes._record_collaborator_connection(db, collaborator, second_account)
        await routes._record_collaborator_connection(db, collaborator, reconnect)
        await routes._record_collaborator_connection(db, collaborator, failed_account)
        await db.commit()

        records = (await db.scalars(select(CollaboratorConnection))).all()
        bonuses = (await db.scalars(select(CollaboratorDailyBonus))).all()
        assert len(records) == 2
        assert [float(record.rate_per_account) for record in records] == [10, 20]
        assert len(bonuses) == 1
        assert bonuses[0].local_date == date(2024, 12, 31)
        assert float(bonuses[0].amount) == 15

        rows = await routes._collaborator_daily_summary(
            db, collaborator, date(2024, 12, 31), date(2025, 1, 1)
        )
        assert rows[0]["connections"] == 2
        assert rows[0]["rate_summary"] == "R$ 10,00 × 1 · R$ 20,00 × 1"
        assert rows[0]["rate_total"] == 30
        assert rows[0]["goal_reached"] is True
        assert rows[0]["bonus"] == 15
        assert rows[0]["total"] == 45
        assert rows[1]["connections"] == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_collaborator_reports_and_management_are_scoped_to_owner():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        first_owner = User(email="one@example.com", username="one", password_hash="hash")
        second_owner = User(email="two@example.com", username="two", password_hash="hash")
        db.add_all([first_owner, second_owner])
        await db.flush()
        own_collaborator = User(
            email=None, username="own", password_hash="hash", role="collaborator",
            parent_id=first_owner.id,
        )
        foreign_collaborator = User(
            email=None, username="foreign", password_hash="hash", role="collaborator",
            parent_id=second_owner.id,
        )
        db.add_all([own_collaborator, foreign_collaborator])
        await db.commit()

        listed = await routes.list_collaborators(user=first_owner, db=db)
        assert [item["username"] for item in listed["collaborators"]] == ["own"]

        own_report = await routes.collaborator_report(
            request_for_path(f"/collaborators/{own_collaborator.id}/report"),
            own_collaborator.id,
            user=own_collaborator,
            db=db,
        )
        assert own_report.status_code == 200

        with pytest.raises(HTTPException) as error:
            await routes.collaborator_report(
                request_for_path(f"/collaborators/{foreign_collaborator.id}/report"),
                foreign_collaborator.id,
                user=own_collaborator,
                db=db,
            )
        assert error.value.status_code == 404

        allowed_user = await routes.current_user(
            request_for_path(f"/collaborators/{own_collaborator.id}/report", own_collaborator.id),
            db=db,
        )
        assert allowed_user.id == own_collaborator.id

    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_collaborator_is_scoped_to_owner():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="delete-owner@example.com", username="owner", password_hash="hash")
        other_owner = User(email="other-owner@example.com", username="other", password_hash="hash")
        db.add_all([owner, other_owner])
        await db.flush()
        own_collaborator = User(
            email=None, username="own-worker", password_hash="hash", role="collaborator",
            parent_id=owner.id,
        )
        foreign_collaborator = User(
            email=None, username="foreign-worker", password_hash="hash", role="collaborator",
            parent_id=other_owner.id,
        )
        db.add_all([own_collaborator, foreign_collaborator])
        await db.commit()

        with pytest.raises(HTTPException) as error:
            await routes.delete_collaborator(
                foreign_collaborator.id, user=owner, db=db
            )
        assert error.value.status_code == 404
        assert await db.get(User, foreign_collaborator.id) is not None

        result = await routes.delete_collaborator(
            own_collaborator.id, user=owner, db=db
        )
        assert result == {"id": own_collaborator.id, "deleted": True}
        assert await db.get(User, own_collaborator.id) is None

    await engine.dispose()
