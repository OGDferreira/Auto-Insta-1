import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app import jobs, routes
from app.db import Base
from app.models import (
    AppNotification,
    CollaboratorConnectionBatch,
    InstagramAccount,
    NotificationSubscription,
    PostingBatch,
    ScheduledPost,
    User,
)


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
async def test_account_alerts_offer_direct_retry_for_failed_publications():
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
        failure = alerts[0]
        assert failure["type"] == "publication"
        assert failure["account_id"] == account.id
        assert failure["action_url"] == f"/posts/{failure['id'].split('-')[1]}/retry"
        assert failure["action_label"] == "Tentar novamente"

    await engine.dispose()


@pytest.mark.asyncio
async def test_account_alerts_offer_resume_for_paused_batches_with_pending_posts():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="paused-alerts@example.com", username="paused", password_hash="hash")
        db.add(owner)
        await db.flush()
        account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-paused",
            username="paused_profile",
            access_token_encrypted="token",
            connection_status="connected",
        )
        batch = PostingBatch(
            owner_id=owner.id,
            name="Loop da campanha",
            status="paused",
            account_ids="[]",
            is_loop=True,
        )
        db.add_all([account, batch])
        await db.flush()
        db.add(ScheduledPost(
            owner_id=owner.id,
            account_id=account.id,
            batch_id=batch.id,
            status="scheduled",
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
            media_url="https://example.com/paused.jpg",
        ))
        await db.commit()

        alerts = await routes._account_alerts(db, owner.id)

        assert len(alerts) == 1
        assert alerts[0]["type"] == "batch"
        assert alerts[0]["username"] == "Loop da campanha"
        assert alerts[0]["action_url"] == f"/batches/{batch.id}/resume"
        assert alerts[0]["action_label"] == "Retomar lote"

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
        notification = await db.scalar(
            select(AppNotification).where(AppNotification.user_id == owner.id)
        )
        assert notification.title == "Conexão perdida"
        assert notification.category == "account_error"

    await engine.dispose()


@pytest.mark.asyncio
async def test_new_collaborator_connections_are_batched_per_collaborator():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="batch-owner@example.com", username="owner", password_hash="hash")
        db.add(owner)
        await db.flush()
        collaborator = User(
            email="batch-collaborator@example.com",
            username="colaborador",
            password_hash="hash",
            role="collaborator",
            parent_id=owner.id,
        )
        other_collaborator = User(
            email="other-collaborator@example.com",
            username="outro",
            password_hash="hash",
            role="collaborator",
            parent_id=owner.id,
        )
        db.add_all([collaborator, other_collaborator])
        await db.flush()
        accounts = [
            InstagramAccount(
                owner_id=owner.id,
                instagram_user_id=f"ig-batch-{index}",
                username=f"profile_{index}",
                connection_status="connected",
            )
            for index in range(3)
        ]
        db.add_all(accounts)
        await db.flush()

        await routes._record_collaborator_connection(db, collaborator, accounts[0])
        await routes._record_collaborator_connection(db, collaborator, accounts[1])
        await routes._record_collaborator_connection(db, other_collaborator, accounts[2])
        await db.commit()

        batches = (await db.scalars(
            select(CollaboratorConnectionBatch).order_by(CollaboratorConnectionBatch.id)
        )).all()
        assert [(batch.collaborator_name, batch.account_count) for batch in batches] == [
            ("colaborador", 2),
            ("outro", 1),
        ]
        assert batches[0].notify_at - batches[0].started_at == timedelta(minutes=15)

    await engine.dispose()


@pytest.mark.asyncio
async def test_due_collaborator_notification_is_delivered_once(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    sent = []

    async def fake_push(db, user_id, title, body, url, tag):
        sent.append((user_id, title, body, url, tag))

    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(jobs, "_send_mobile_push", fake_push)

    async with session_factory() as db:
        owner = User(email="due-owner@example.com", username="due-owner", password_hash="hash")
        db.add(owner)
        await db.flush()
        now = datetime.now(timezone.utc)
        due = CollaboratorConnectionBatch(
            owner_id=owner.id,
            collaborator_id=owner.id,
            collaborator_name="Ana",
            account_count=3,
            started_at=now - timedelta(minutes=16),
            notify_at=now - timedelta(minutes=1),
        )
        future = CollaboratorConnectionBatch(
            owner_id=owner.id,
            collaborator_id=owner.id,
            collaborator_name="Ana",
            account_count=1,
            started_at=now,
            notify_at=now + timedelta(minutes=15),
        )
        db.add_all([due, future])
        await db.commit()
        due_id = due.id
        future_id = future.id
        owner_id = owner.id

    await jobs.process_due_collaborator_notifications()
    await jobs.process_due_collaborator_notifications()

    async with session_factory() as db:
        delivered = (await db.scalars(
            select(AppNotification).where(AppNotification.user_id == owner_id)
        )).all()
        due_batch = await db.get(CollaboratorConnectionBatch, due_id)
        future_batch = await db.get(CollaboratorConnectionBatch, future_id)
        assert len(delivered) == 1
        assert delivered[0].body == "Ana conectou 3 contas"
        assert due_batch.notified_at is not None
        assert future_batch.notified_at is None
    assert len(sent) == 1
    assert sent[0][2] == "Ana conectou 3 contas"

    await engine.dispose()

    await engine.dispose()


@pytest.mark.asyncio
async def test_in_app_notification_inbox_is_scoped_to_authenticated_user():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="inbox-owner@example.com", username="inbox-owner", password_hash="hash")
        other_user = User(email="inbox-other@example.com", username="inbox-other", password_hash="hash")
        db.add_all([owner, other_user])
        await db.flush()
        notification = AppNotification(
            user_id=owner.id,
            title="Conta suspensa",
            body="Verifique a conta.",
            category="account_error",
        )
        db.add(notification)
        await db.commit()

        result = await routes.list_in_app_notifications(user=owner, db=db)
        assert [item["id"] for item in result["notifications"]] == [notification.id]
        assert await routes.list_in_app_notifications(user=other_user, db=db) == {
            "notifications": []
        }
        with pytest.raises(HTTPException) as error:
            await routes.mark_in_app_notification_read(
                notification.id,
                user=other_user,
                db=db,
            )
        assert error.value.status_code == 404
        await routes.mark_in_app_notification_read(notification.id, user=owner, db=db)
        assert (await routes.list_in_app_notifications(user=owner, db=db))["notifications"] == []

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
