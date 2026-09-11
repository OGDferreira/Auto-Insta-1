import asyncio
from datetime import datetime

import httpx
from redis import Redis
from rq import Queue
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import InstagramAccount, ScheduledPost
from .security import decrypt_token


def enqueue_post(post: ScheduledPost) -> str:
    queue = Queue("instagram", connection=Redis.from_url(get_settings().redis_url))
    job = queue.enqueue_at(post.scheduled_for, publish_scheduled_post, post.id)
    return job.id


async def _publish(post_id: int) -> None:
    settings = get_settings()
    async with SessionLocal() as db:
        result = await db.execute(select(ScheduledPost).where(ScheduledPost.id == post_id))
        post = result.scalar_one_or_none()
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


def publish_scheduled_post(post_id: int) -> None:
    asyncio.run(_publish(post_id))
