from datetime import datetime, timezone
import asyncio
import logging
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update

from .config import get_settings
from .db import SessionLocal
from .models import InstagramAccount, InstagramMetric, ScheduledPost
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


def _validate_media_url(media_url: str) -> None:
    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("A URL da mídia precisa ser absoluta e pública (http/https).")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            "A URL da mídia aponta para localhost e não é acessível pela Meta. "
            "Configure PUBLIC_BASE_URL com a URL HTTPS pública do Render."
        )


async def _wait_for_container(client: httpx.AsyncClient, base: str, container_id: str, token: str) -> None:
    """Wait until Meta has finished processing the uploaded media container."""
    # Aumentado para 40 tentativas com sleep de 3s (Total ~120s) para garantir o download de vídeos pela Meta
    for _ in range(40):
        response = await client.get(
            f"{base}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
        )
        if response.is_error:
            raise RuntimeError(_api_error(response))
        
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Resposta inválida ao consultar container: {response.text[:900]}") from exc
        status_code = payload.get("status_code")
        
        if status_code == "FINISHED":
            return
        if status_code in {"ERROR", "EXPIRED"}:
            status_msg = payload.get("status") or "sem mensagem da Meta"
            raise RuntimeError(
                f"Erro no container da Meta ({status_code}): {status_msg}. "
                "Verifique se a URL da mídia é pública e acessível pela Meta."
            )
            
        await asyncio.sleep(3)
        
    raise RuntimeError("A Meta (Instagram) não concluiu o processamento da mídia no tempo esperado.")


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
    scheduler.add_job(
        collect_instagram_insights,
        "interval",
        hours=24,
        id="collect-instagram-insights",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


async def collect_instagram_insights() -> None:
    """Collect daily Instagram impressions and reach for every connected account."""
    settings = get_settings()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as db:
        accounts = (await db.scalars(select(InstagramAccount))).all()
        async with httpx.AsyncClient(timeout=30) as client:
            for account in accounts:
                try:
                    response = await client.get(
                        f"https://graph.instagram.com/{settings.graph_api_version}/{account.instagram_user_id}/insights",
                        params={
                            "metric": "impressions,reach",
                            "period": "day",
                            "access_token": decrypt_token(account.access_token_encrypted),
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    values = {
                        item.get("name"): item.get("values", [{}])[-1].get("value", 0)
                        for item in payload.get("data", [])
                    }
                    metric = await db.scalar(select(InstagramMetric).where(
                        InstagramMetric.account_id == account.id,
                        InstagramMetric.metric_date == today,
                    ))
                    if metric is None:
                        metric = InstagramMetric(account_id=account.id, metric_date=today)
                        db.add(metric)
                    metric.impressions = int(values.get("impressions", 0) or 0)
                    metric.reach = int(values.get("reach", 0) or 0)
                except Exception:
                    logger.exception("Falha ao coletar Insights da conta %s", account.instagram_user_id)
        await db.commit()


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
            
            # Este projeto usa Instagram Login, cujo token é válido em graph.instagram.com.
            base = f"https://graph.instagram.com/{settings.graph_api_version}"
            
            # CORREÇÃO 2: Forçar o tipo REELS caso o banco de dados envie VIDEO
            media_type = post.media_type.upper()
            if media_type == "VIDEO":
                media_type = "REELS"
            if media_type not in {"IMAGE", "REELS"}:
                raise RuntimeError(f"Tipo de mídia não suportado: {post.media_type}")
            _validate_media_url(post.media_url)
                
            # Define a chave correta da URL (image_url vs video_url)
            media_key = "image_url" if media_type == "IMAGE" else "video_url"
            
            # Timeout aumentado para 60s para evitar quedas caso a Meta demore a responder
            async with httpx.AsyncClient(timeout=60) as client:
                params = {
                    "access_token": token,
                    "caption": post.caption,
                    media_key: post.media_url,
                    "media_type": media_type,
                }
                
                # ETAPA A: Criar o Container de Mídia
                container = await client.post(f"{base}/{account.instagram_user_id}/media", params=params)
                if container.is_error:
                    raise RuntimeError(_api_error(container))
                    
                try:
                    container_id = container.json()["id"]
                except (ValueError, KeyError) as exc:
                    raise RuntimeError(f"Resposta inválida ao criar container: {container.text[:900]}") from exc
                    
                # ETAPA B: Aguardar Processamento (Polling)
                # Obrigatório para vídeos/Reels. Imagens são imediatas e não expõem status_code.
                if media_type == "REELS":
                    await _wait_for_container(client, base, container_id, token)
                    
                # ETAPA C: Publicar o Container Final
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
