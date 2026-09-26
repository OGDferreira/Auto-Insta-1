from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import jobs, routes
from app.db import Base
from app.models import InstagramAccount, PostingBatch, ScheduledPost, User


@pytest.mark.asyncio
async def test_loop_restarts_playlist_and_includes_added_accounts(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = []
    monkeypatch.setattr(routes, "schedule_post", lambda post_id, when: scheduled.append((post_id, when)))
    monkeypatch.setattr(jobs, "schedule_post", lambda post_id, when: scheduled.append((post_id, when)))
    monkeypatch.setattr(jobs, "SessionLocal", session_factory)

    async with session_factory() as db:
        owner = User(email="loop@example.com", username="loop", password_hash="hash")
        db.add(owner)
        await db.flush()
        first_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-loop-1",
            username="loop_one",
            access_token_encrypted="encrypted",
        )
        db.add(first_account)
        await db.commit()

        created_at = datetime.now(timezone.utc)
        await routes.create_bulk_posts(
            account_ids=[first_account.id],
            media_urls=["https://example.com/first.mp4", "https://example.com/second.mp4"],
            media_types=["VIDEO", "VIDEO"],
            storage_paths=["first.mp4", "second.mp4"],
            drive_media_urls=[],
            drive_account_emails=[],
            drive_credentials_encrypted=[],
            captions=[],
            caption_mode="global",
            caption="Playlist caption",
            scheduled_for="",
            interval_minutes=2,
            batch_name="Indefinite loop",
            is_loop=True,
            user=owner,
            db=db,
        )

        batch = await db.scalar(select(PostingBatch).where(PostingBatch.is_loop.is_(True)))
        initial_posts = (await db.scalars(
            select(ScheduledPost)
            .where(ScheduledPost.batch_id == batch.id)
            .order_by(ScheduledPost.loop_index)
        )).all()
        assert [post.loop_index for post in initial_posts] == [0, 1]
        first_scheduled = initial_posts[0].scheduled_for
        if first_scheduled.tzinfo is None:
            first_scheduled = first_scheduled.replace(tzinfo=timezone.utc)
        assert abs((first_scheduled - created_at).total_seconds() - 180) < 1
        assert len(scheduled) == 2

        for post in initial_posts:
            post.status = "published"
        await db.commit()
        await jobs._advance_loop_after_post(initial_posts[-1].id)

        restarted_post = await db.scalar(
            select(ScheduledPost)
            .where(
                ScheduledPost.batch_id == batch.id,
                ScheduledPost.account_id == first_account.id,
                ScheduledPost.id > initial_posts[-1].id,
            )
        )
        assert restarted_post is not None
        assert restarted_post.loop_index == 0
        assert restarted_post.media_url == "https://example.com/first.mp4"
        restarted_at = restarted_post.scheduled_for
        if restarted_at.tzinfo is None:
            restarted_at = restarted_at.replace(tzinfo=timezone.utc)
        assert restarted_at > datetime.now(timezone.utc)

        second_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-loop-2",
            username="loop_two",
            access_token_encrypted="encrypted",
        )
        db.add(second_account)
        await db.flush()
        await routes.update_batch_accounts(
            batch_id=batch.id,
            account_ids=[first_account.id, second_account.id],
            batch_name="Updated Loop",
            interval_minutes=5,
            user=owner,
            db=db,
        )

        added_posts = (await db.scalars(
            select(ScheduledPost)
            .where(
                ScheduledPost.batch_id == batch.id,
                ScheduledPost.account_id == second_account.id,
            )
            .order_by(ScheduledPost.loop_index)
        )).all()
        assert [post.media_url for post in added_posts] == [
            "https://example.com/first.mp4",
            "https://example.com/second.mp4",
        ]
        assert all(post.status in jobs.PENDING_STATUSES for post in added_posts)
        assert batch.name == "Updated Loop"
        assert batch.loop_interval_minutes == 5

        third_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-loop-3",
            username="loop_three",
            access_token_encrypted="encrypted",
        )
        db.add(third_account)
        await db.commit()
        request = Request({
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/dashboard",
            "raw_path": b"/dashboard",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "session": {},
        })
        dashboard = await routes.dashboard(request, user=owner, db=db)
        html = dashboard.body.decode()
        editor = re.search(r'<form class="loop-editor"[^>]*>(.*?)</form>', html, re.DOTALL)
        assert editor is not None
        assert re.search(r'name="account_ids" value="1" checked', editor.group(1))
        assert re.search(r'name="account_ids" value="3"\s*>', editor.group(1))
        assert 'class="loop-board-title"' in html

    await engine.dispose()


@pytest.mark.asyncio
async def test_new_account_is_added_to_existing_loop_playlist():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="dynamic@example.com", username="dynamic", password_hash="hash")
        db.add(owner)
        await db.flush()
        existing_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-dynamic-1",
            username="existing",
            access_token_encrypted="encrypted",
        )
        new_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-dynamic-2",
            username="new_account",
            access_token_encrypted="encrypted",
            connection_status="connected",
        )
        db.add_all([existing_account, new_account])
        await db.flush()
        batch = PostingBatch(
            owner_id=owner.id,
            name="Dynamic loop",
            account_ids=json.dumps([existing_account.id]),
            is_loop=True,
            loop_interval_minutes=4,
        )
        db.add(batch)
        await db.flush()
        now = datetime.now(timezone.utc)
        db.add_all([
            ScheduledPost(
                owner_id=owner.id,
                account_id=existing_account.id,
                batch_id=batch.id,
                loop_index=index,
                media_url=f"https://example.com/video-{index}.mp4",
                media_type="REELS",
                caption="Loop caption",
                status="published",
                scheduled_for=now,
            )
            for index in range(2)
        ])
        await db.commit()

        to_schedule = await routes._include_account_in_existing_loops(
            db, owner.id, new_account.id
        )
        await db.commit()

        new_posts = (await db.scalars(
            select(ScheduledPost)
            .where(
                ScheduledPost.batch_id == batch.id,
                ScheduledPost.account_id == new_account.id,
            )
            .order_by(ScheduledPost.loop_index)
        )).all()
        assert [post.media_url for post in new_posts] == [
            "https://example.com/video-0.mp4",
            "https://example.com/video-1.mp4",
        ]
        assert json.loads(batch.account_ids) == [existing_account.id, new_account.id]
        assert len(to_schedule) == 2
        assert all(should_schedule for should_schedule, _, _ in to_schedule)

    await engine.dispose()


@pytest.mark.asyncio
async def test_loop_failure_is_timestamped_and_does_not_stop_next_cycle(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(jobs, "decrypt_token", lambda _token: "token")
    scheduled = []
    monkeypatch.setattr(jobs, "schedule_post", lambda post_id, when: scheduled.append((post_id, when)))

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def reject_account(_client, account, _token, _settings):
        account.connection_status = "error"
        account.status_reason = "Instagram permission denied"
        return False

    monkeypatch.setattr(jobs.httpx, "AsyncClient", lambda **_kwargs: FakeHttpClient())
    monkeypatch.setattr(jobs, "_refresh_account_status", reject_account)

    async with session_factory() as db:
        owner = User(email="failure@example.com", username="failure", password_hash="hash")
        db.add(owner)
        await db.flush()
        account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-failure",
            username="failure_account",
            access_token_encrypted="encrypted",
        )
        db.add(account)
        await db.flush()
        batch = PostingBatch(
            owner_id=owner.id,
            name="Failure loop",
            account_ids=json.dumps([account.id]),
            is_loop=True,
            loop_interval_minutes=1,
        )
        db.add(batch)
        await db.flush()
        failed_post = ScheduledPost(
            owner_id=owner.id,
            account_id=account.id,
            batch_id=batch.id,
            loop_index=0,
            media_url="https://example.com/failure.mp4",
            media_type="REELS",
            scheduled_for=datetime.now(timezone.utc),
        )
        db.add(failed_post)
        await db.commit()
        failed_post_id = failed_post.id
        batch_id = batch.id
        account_id = account.id

    await jobs._publish(failed_post_id)

    async with session_factory() as db:
        failed_post = await db.get(ScheduledPost, failed_post_id)
        next_post = await db.scalar(
            select(ScheduledPost).where(
                ScheduledPost.batch_id == batch_id,
                ScheduledPost.account_id == account_id,
                ScheduledPost.id != failed_post_id,
            )
        )
        assert failed_post.status == "failed"
        assert failed_post.error_message == "Instagram permission denied"
        assert failed_post.error_at is not None
        assert next_post is not None
        assert next_post.loop_index == 0
        assert next_post.status in jobs.PENDING_STATUSES
        assert len(scheduled) == 1

    await engine.dispose()