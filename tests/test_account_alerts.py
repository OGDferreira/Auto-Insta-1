from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import routes
from app.db import Base
from app.models import InstagramAccount, ScheduledPost, User


@pytest.mark.asyncio
async def test_account_alerts_use_publication_time_and_report_account_problems():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)

    async with session_factory() as db:
        owner = User(email="alerts@example.com", username="alerts", password_hash="hash")
        db.add(owner)
        await db.flush()
        fresh = InstagramAccount(
            owner_id=owner.id, instagram_user_id="ig-fresh", username="fresh",
            access_token_encrypted="token", connection_status="connected",
        )
        stale = InstagramAccount(
            owner_id=owner.id, instagram_user_id="ig-stale", username="stale",
            access_token_encrypted="token", connection_status="connected",
            created_at=now - timedelta(hours=2),
        )
        restricted = InstagramAccount(
            owner_id=owner.id, instagram_user_id="ig-restricted", username="restricted",
            access_token_encrypted="token", connection_status="suspended",
            status_reason="Publicação temporariamente restrita.",
        )
        db.add_all([fresh, stale, restricted])
        await db.flush()
        db.add(ScheduledPost(
            owner_id=owner.id, account_id=fresh.id, status="published",
            scheduled_for=now - timedelta(minutes=90),
            published_at=now - timedelta(minutes=69),
            media_url="https://example.com/fresh.jpg",
        ))
        db.add(ScheduledPost(
            owner_id=owner.id, account_id=stale.id, status="published",
            scheduled_for=now - timedelta(minutes=10),
            published_at=now - timedelta(minutes=71),
            media_url="https://example.com/stale.jpg",
        ))
        await db.commit()

        alerts = await routes._account_alerts(
            db, owner.id, [fresh, stale, restricted], now
        )

        assert [alert["account_id"] for alert in alerts] == [stale.id, restricted.id]
        stale_alert, restricted_alert = alerts
        assert stale_alert["type"] == "stale"
        assert stale_alert["severity"] == "warning"
        assert "71 minutos" in stale_alert["reason"]
        assert restricted_alert["type"] == "connection"
        assert restricted_alert["severity"] == "error"
        assert restricted_alert["reason"] == "Publicação temporariamente restrita."

    await engine.dispose()


@pytest.mark.asyncio
async def test_account_alerts_include_latest_unresolved_publication_failure():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)

    async with session_factory() as db:
        owner = User(email="failure-alerts@example.com", username="failure", password_hash="hash")
        db.add(owner)
        await db.flush()
        account = InstagramAccount(
            owner_id=owner.id, instagram_user_id="ig-failure", username="failure",
            access_token_encrypted="token", connection_status="connected",
            created_at=now - timedelta(minutes=20),
        )
        db.add(account)
        await db.flush()
        db.add(ScheduledPost(
            owner_id=owner.id, account_id=account.id, status="failed",
            scheduled_for=now - timedelta(minutes=5),
            error_at=now - timedelta(minutes=4),
            error_message="Token inválido para publicar.",
            media_url="https://example.com/failed.jpg",
        ))
        await db.commit()

        alerts = await routes._account_alerts(db, owner.id, now=now)

        assert len(alerts) == 1
        assert alerts[0]["username"] == "failure"
        assert alerts[0]["type"] == "publication"
        assert alerts[0]["reason"] == "Token inválido para publicar."

    await engine.dispose()
