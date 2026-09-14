from datetime import datetime, timezone
import asyncio
import logging
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update

from .config import get_settings
from .db import SessionLocal
from .models import InstagramAccount, ScheduledPost
from .security import decrypt_token


logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")
PENDING_STATUSES = ("scheduled", "aguardando", "pending")
LOCAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _api_error(response: httpx.Response) -> str:
    """Return the useful Meta error payload instead of only the HTTP status."""
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    return f"Instagram API {response.status_code}: {str(payload)[:900]}"


async def _wait_for_container(client: httpx.AsyncClient, base: str, container_id: str, token: str) -> None:
    """Wait until Meta has finished processing the uploaded media container."""
    for _ in range(30):
        response = await client.get(
            f"{base}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
        )
        if response.is_error:
            raise RuntimeError(_api_error(response))
        payload = response.json()
        status_code = payload.get("status_code")
        if status_code == "FINISHED":
            return
        if status_code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(
                f"Instagram container {status_code}: {payload.get('status') or 'processamento rejeitado'}"
            )
        await asyncio.sleep(2)
    raise RuntimeError("Instagram não concluiu o processamento da mídia no tempo esperado")


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
                if container.is_error:
                    raise RuntimeError(_api_error(container))
                try:
                    container_id = container.json()["id"]
                except (ValueError, KeyError) as exc:
                    raise RuntimeError(f"Resposta inválida ao criar container: {container.text[:900]}") from exc
                await _wait_for_container(client, base, container_id, token)
                published = await client.post(
                    f"{base}/{account.instagram_user_id}/media_publish",
                    params={"creation_id": container_id, "access_token": token},
                )
                if published.is_error:
                    raise RuntimeError(_api_error(published))
            post.status = "published"
            post.error_message = None
        except Exception as exc:
            post.status = "failed"
            post.error_message = str(exc)[:1000]
            logger.exception("Falha ao publicar post %s: %s", post_id, exc)
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
