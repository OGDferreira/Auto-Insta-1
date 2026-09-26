from datetime import datetime, timezone
import re

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