from datetime import datetime, timedelta, timezone
import asyncio
import logging
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from supabase import create_client
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


def _account_status_from_error(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = str(error.get("message") or response.text or "Falha ao verificar a conta")
    normalized = message.lower()
    if any(term in normalized for term in ("suspend", "disabled", "deactivated")):
        return "suspended", message[:500]
    if any(term in normalized for term in ("challenge", "checkpoint", "blocked", "restricted")):
        return "error", message[:500]
    return "error", message[:500]


async def _refresh_account_status(
    client: httpx.AsyncClient,
    account: InstagramAccount,
    token: str,
    settings,
) -> bool:
    response = await client.get(
        f"https://graph.instagram.com/{settings.graph_api_version}/me",
        params={
            "fields": "id,username",
            "access_token": token,
        },
    )
    if response.is_error:
        account.connection_status, account.status_reason = _account_status_from_error(response)
        account.status_checked_at = datetime.now(timezone.utc)
        logger.error(
            "Status da conta Instagram %s retornou %s: %s",
            account.instagram_user_id,
            response.status_code,
            account.status_reason,
        )
        return False
    account.connection_status = "connected"
    account.status_reason = None
    account.status_checked_at = datetime.now(timezone.utc)
    return True


def _local_day_start(value) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)


def _insight_values(payload: dict, today) -> list[tuple[datetime, int, int]]:
    values_by_date: dict[object, dict[str, int]] = {}
    for item in payload.get("data", []):
        name = item.get("name")
        if name not in {"impressions", "views", "reach"}:
            continue
        entries = item.get("values", [])
        if not entries and isinstance(item.get("total_value"), dict):
            entries = [{"value": item["total_value"].get("value", 0)}]
        for entry in entries:
            raw_date = entry.get("end_time")
            try:
                metric_date = (
                    datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    .astimezone(LOCAL_TIMEZONE)
                    .date()
                    if raw_date
                    else today
                )
            except (AttributeError, ValueError):
                metric_date = today
            values_by_date.setdefault(metric_date, {})[name] = int(entry.get("value", 0) or 0)
    if not values_by_date:
        values_by_date[today] = {"impressions": 0, "reach": 0}
    return [
        (
            _local_day_start(metric_date),
            values.get("impressions", values.get("views", 0)),
            values.get("reach", 0),
        )
        for metric_date, values in values_by_date.items()
    ]


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
        minutes=10,
        id="collect-instagram-insights",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


async def collect_instagram_insights() -> None:
    """Collect daily Instagram impressions and reach for every connected account."""
    settings = get_settings()
    local_today = datetime.now(LOCAL_TIMEZONE).date()
    until_date = local_today
    async with SessionLocal() as db:
        accounts = (await db.scalars(select(InstagramAccount))).all()
        async with httpx.AsyncClient(timeout=30) as client:
            for account in accounts:
                try:
                    token = decrypt_token(account.access_token_encrypted)
                    if not await _refresh_account_status(client, account, token, settings):
                        continue
                    created_at = account.created_at or datetime.now(timezone.utc)
                    connected_date = (
                        created_at.astimezone(LOCAL_TIMEZONE).date()
                        if created_at.tzinfo
                        else created_at.replace(tzinfo=timezone.utc).astimezone(LOCAL_TIMEZONE).date()
                    )
                    insights = None
                    for metric_names in ("views,reach", "impressions,reach"):
                        candidate = await client.get(
                            f"https://graph.instagram.com/{settings.graph_api_version}/{account.instagram_user_id}/insights",
                            params={
                                "metric": metric_names,
                                "period": "day",
                                "since": connected_date.isoformat(),
                                "until": until_date.isoformat(),
                                "access_token": token,
                            },
                        )
                        if not candidate.is_error:
                            insights = candidate
                            break
                        logger.warning(
                            "Insights %s indisponíveis para a conta Instagram %s: %s",
                            metric_names,
                            account.instagram_user_id,
                            _api_error(candidate),
                        )
                    if insights is None:
                        logger.warning(
                            "Insights indisponíveis para a conta Instagram %s: %s",
                            account.instagram_user_id,
                            "nenhuma métrica compatível foi retornada",
                        )
                        continue
                    payload = insights.json()
                    logger.info(
                        "Insights recebidos para Instagram %s: %s",
                        account.instagram_user_id,
                        {
                            item.get("name"): item.get("values", item.get("total_value"))
                            for item in payload.get("data", [])
                        },
                    )
                    for metric_date, impressions, reach in _insight_values(payload, local_today):
                        metric = await db.scalar(select(InstagramMetric).where(
                            InstagramMetric.account_id == account.id,
                            InstagramMetric.metric_date == metric_date,
                        ))
                        if metric is None:
                            metric = InstagramMetric(account_id=account.id, metric_date=metric_date)
                            db.add(metric)
                        metric.impressions = impressions
                        metric.reach = reach
                except Exception:
                    account.status_checked_at = datetime.now(timezone.utc)
                    account.connection_status = "error"
                    account.status_reason = "Não foi possível verificar a conexão da conta."
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
                }
                if media_type == "REELS":
                    params["media_type"] = media_type
                
                # ETAPA A: Criar o Container de Mídia
                container = await client.post(f"{base}/{account.instagram_user_id}/media", params=params)
                if container.is_error:
                    raise RuntimeError(_api_error(container))
                    
                try:
                    container_id = container.json()["id"]
                except (ValueError, KeyError) as exc:
                    raise RuntimeError(f"Resposta inválida ao criar container: {container.text[:900]}") from exc
                    
                # ETAPA B: Aguardar Processamento (Polling)
                # A Meta pode manter imagens em processamento; publicar antes
                # de o container ficar pronto retorna o erro 9007/2207027.
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
            if post.storage_path and post.thumbnail_storage_path and post.thumbnail_url:
                try:
                    storage_key = settings.supabase_service_role or settings.supabase_key
                    if settings.supabase_url and storage_key:
                        def remove_original() -> None:
                            client = create_client(settings.supabase_url, storage_key)
                            client.storage.from_(settings.supabase_storage_bucket).remove([post.storage_path])
                        await asyncio.to_thread(remove_original)
                        post.media_url = post.thumbnail_url
                        post.original_media_url = None
                        post.storage_path = None
                except Exception:
                    logger.exception("Falha ao remover mídia original do post %s", post_id)
            
        except Exception as exc:
            post.status = "failed"
            post.error_message = str(exc)[:1000]
            account.connection_status = "suspended" if "permission" in str(exc).lower() or "token" in str(exc).lower() else account.connection_status
            account.status_reason = str(exc)[:500] if account.connection_status == "suspended" else account.status_reason
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
