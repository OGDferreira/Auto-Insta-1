from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update

from .config import get_settings
from .db import SessionLocal
from .models import InstagramAccount, ScheduledPost
from .security import decrypt_token


scheduler = AsyncIOScheduler(timezone="UTC")
PENDING_STATUSES = ("scheduled", "aguardando", "pending")


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reset_scheduler() -> None:
    """Create a scheduler bound to the current application event loop."""
    global scheduler
    if scheduler.running:
        scheduler.shutdown(wait=False)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        process_due_posts,
        "interval",
        seconds=15,
        id="process-due-posts",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


def schedule_post(post_id: int, scheduled_for: datetime) -> str:
    """Schedule a post in the web process and return its scheduler job id."""
    job_id = f"scheduled-post-{post_id}"
    run_date = _utc_datetime(scheduled_for)
    now = datetime.now(timezone.utc)
    if run_date <= now:
        run_date = now
    scheduler.add_job(
        _publish,
        "date",
        run_date=run_date,
        args=[post_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    return job_id


async def schedule_pending_posts() -> None:
    """Restore pending schedules after a process restart."""
    async with SessionLocal() as db:
        posts = (
            await db.scalars(
                select(ScheduledPost).where(
                    ScheduledPost.status.in_(PENDING_STATUSES),
                )
            )
        ).all()
    for post in posts:
        schedule_post(post.id, post.scheduled_for)


async def _publish(post_id: int) -> None:
    settings = get_settings()
    async with SessionLocal() as db:
        claimed = await db.execute(
            update(ScheduledPost)
            .where(ScheduledPost.id == post_id, ScheduledPost.status.in_(PENDING_STATUSES))
            .values(status="processing")
        )
        if claimed.rowcount != 1:
            return
        await db.commit()
        post = await db.get(ScheduledPost, post_id)
        if post is None:
            return
        account = await db.get(InstagramAccount, post.account_id)
        if account is None or account.owner_id != post.owner_id:
            post.status = "failed"
            post.error_message = "Instagram account no longer belongs to this owner"
            await db.commit()
            return
        try:
            token = decrypt_token(account.access_token_encrypted)
            base = f"https://graph.instagram.com/{settings.graph_api_version}"
            async with httpx.AsyncClient(timeout=30) as client:
                params = {
                    "access_token": token,
                    "caption": post.caption,
                    "image_url" if post.media_type.upper() == "IMAGE" else "video_url": post.media_url,
                    "media_type": post.media_type.upper(),
                }
                container = await client.post(f"{base}/{account.instagram_user_id}/media", params=params)
                container.raise_for_status()
                container_id = container.json()["id"]
                published = await client.post(
                    f"{base}/{account.instagram_user_id}/media_publish",
                    params={"creation_id": container_id, "access_token": token},
                )
                published.raise_for_status()
            post.status = "published"
            post.error_message = None
        except Exception as exc:
            post.status = "failed"
            post.error_message = str(exc)[:1000]
        await db.commit()


async def process_due_posts() -> None:
    """Recover due posts that missed their one-shot scheduler job."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        due_ids = (
            await db.scalars(
                select(ScheduledPost.id)
                .where(
                    ScheduledPost.status.in_(PENDING_STATUSES),
                    ScheduledPost.scheduled_for <= now,
                )
                .order_by(ScheduledPost.scheduled_for, ScheduledPost.id)
                .limit(50)
            )
        ).all()
    for post_id in due_ids:
        await _publish(post_id)
