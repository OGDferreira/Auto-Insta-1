import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app import jobs, routes
from app.db import Base
from app.models import InstagramAccount, NotificationSubscription, ScheduledPost, User


@pytest.mark.asyncio
async def test_account_alerts_only_report_lost_or_restricted_connections():
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

        assert [alert["account_id"] for alert in alerts] == [restricted.id]
        restricted_alert = alerts[0]
        assert restricted_alert["type"] == "connection"
        assert restricted_alert["severity"] == "error"
        assert restricted_alert["title"] == "Conta restrita"
        assert restricted_alert["reason"] == "Verifique as restrições da conta no Instagram."

    await engine.dispose()


@pytest.mark.asyncio
async def test_account_alerts_ignore_publication_failures_when_connection_is_healthy():
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

        assert alerts == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_account_alerts_report_expired_tokens_but_ignore_unconnected_pending_accounts():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)

    async with session_factory() as db:
        owner = User(email="expired-alerts@example.com", username="expired", password_hash="hash")
        db.add(owner)
        await db.flush()
        expired = InstagramAccount(
            owner_id=owner.id, instagram_user_id="ig-expired", username="expired",
            access_token_encrypted="token", connection_status="connected",
            token_expires_at=now - timedelta(minutes=1),
        )
        pending = InstagramAccount(
            owner_id=owner.id, instagram_user_id="ig-pending", username="pending",
            access_token_encrypted="", connection_status="pending",
        )
        db.add_all([expired, pending])
        await db.commit()

        alerts = await routes._account_alerts(db, owner.id, [expired, pending], now)

        assert len(alerts) == 1
        assert alerts[0]["account_id"] == expired.id
        assert alerts[0]["title"] == "Token expirado"
        assert alerts[0]["reason"] == "Reconecte a conta para retomar as publicações."

    await engine.dispose()


@pytest.mark.asyncio
async def test_account_error_pushes_only_target_installed_mobile_subscriptions(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    sent_payloads = []

    def fake_webpush(**kwargs):
        sent_payloads.append(json.loads(kwargs["data"]))

    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: SimpleNamespace(vapid_private_key="private", vapid_subject="mailto:test@example.com"),
    )
    monkeypatch.setattr(jobs, "webpush", fake_webpush)

    async with session_factory() as db:
        owner = User(email="push-alerts@example.com", username="push", password_hash="hash")
        db.add(owner)
        await db.flush()
        account = InstagramAccount(
            owner_id=owner.id, instagram_user_id="ig-push", username="push_profile",
            connection_status="disconnected",
        )
        db.add(account)
        db.add_all([
            NotificationSubscription(
                user_id=owner.id, endpoint="https://push.example/desktop",
                p256dh="key", auth="auth", pwa_installed=False,
            ),
            NotificationSubscription(
                user_id=owner.id, endpoint="https://push.example/mobile",
                p256dh="key", auth="auth", pwa_installed=True,
            ),
        ])
        await db.flush()

        await jobs._notify_account_connection_error(db, account)

        assert len(sent_payloads) == 1
        assert sent_payloads[0]["title"] == "Conexão perdida"
        assert sent_payloads[0]["body"] == "@push_profile · verifique a conta no Hub."
        assert sent_payloads[0]["url"] == f"/hub#account-{account.id}"

    await engine.dispose()


@pytest.mark.asyncio
async def test_push_subscription_rejects_non_installed_devices():
    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/notifications/subscribe",
        "raw_path": b"/api/notifications/subscribe",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "server": ("testserver", 443),
        "client": ("testclient", 50000),
    }, receive=receive)

    with pytest.raises(HTTPException) as error:
        await routes.subscribe_notifications(request, user=object(), db=object())

    assert error.value.status_code == 403
