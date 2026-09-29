from datetime import datetime, timedelta, timezone
import asyncio
import io
import json
import logging
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pywebpush import WebPushException, webpush
from supabase import create_client
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import SessionLocal
from .models import (
    InstagramAccount,
    InstagramMetric,
    NotificationSubscription,
    PostingBatch,
    ScheduledPost,
)
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
    error_code = str(error.get("code") or "")
    if error_code == "190":
        return "disconnected", f"Token inválido: {message}"[:500]
    if any(term in normalized for term in ("challenge", "checkpoint", "blocked", "restricted", "disabled", "deactivated")):
        return "error", message[:500]
    return "connected", message[:500]


def _is_authentication_error(message: str) -> bool:
    normalized = message.lower()
    return (
        '"code": 190' in normalized
        or "'code': 190" in normalized
        or "error code 190" in normalized
        or "invalid oauth" in normalized
    )


async def _refresh_account_status(
    client: httpx.AsyncClient,
    account: InstagramAccount,
    token: str,
    settings,
    db: AsyncSession | None = None,
) -> bool:
    response = await client.get(
        f"https://graph.instagram.com/{settings.graph_api_version}/me",
        params={
            "fields": "id,username",
            "access_token": token,
        },
    )
    if response.is_error:
        previous_status = account.connection_status
        account.connection_status, account.status_reason = _account_status_from_error(response)
        account.status_checked_at = datetime.now(timezone.utc)
        logger.error(
            "Status da conta Instagram %s retornou %s: %s",
            account.instagram_user_id,
            response.status_code,
            account.status_reason,
        )
        if db is not None and (
            account.connection_status in {"disconnected", "suspended", "error"}
        ):
            await _detach_account_from_batches(db, account)
            if previous_status not in {"disconnected", "suspended", "error"}:
                await _notify_account_connection_error(db, account)
        return False
    account.connection_status = "connected"
    account.status_reason = None
    account.status_checked_at = datetime.now(timezone.utc)
    return True


async def _notify_account_connection_error(
    db: AsyncSession,
    account: InstagramAccount,
) -> None:
    settings = get_settings()
    if not settings.vapid_private_key or not settings.vapid_subject:
        return
    subscriptions = (await db.scalars(
        select(NotificationSubscription).where(
            NotificationSubscription.user_id == account.owner_id,
            NotificationSubscription.pwa_installed.is_(True),
        )
    )).all()
    if not subscriptions:
        return
    status_reason = (account.status_reason or "").lower()
    if "token" in status_reason or "oauth" in status_reason or "credential" in status_reason:
        title = "Token expirado"
    elif account.connection_status == "disconnected":
        title = "Conexão perdida"
    elif account.connection_status == "suspended":
        title = "Conta restrita"
    else:
        title = "Erro na conta"
    payload = json.dumps({
        "title": title,
        "body": f"@{account.username} · verifique a conta no Hub.",
        "url": f"/hub#account-{account.id}",
        "tag": f"account-{account.id}-connection",
    })
    for subscription in subscriptions:
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code in {404, 410}:
                await db.delete(subscription)
            else:
                logger.warning(
                    "Falha ao notificar erro da conta %s no PWA: %s",
                    account.id,
                    exc,
                )


def _local_day_start(value) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)


def _insight_values(payload: dict, today) -> list[tuple[datetime, int, int]]:
    values_by_date: dict[object, dict[str, int]] = {}
    for item in payload.get("data", []):
        name = item.get("name")
        if name not in {"impressions", "views", "content_views", "reach"}:
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
            values.get("views") or values.get("content_views") or values.get("impressions") or values.get("reach", 0),
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
                    if account.connection_status == "pending" or not account.access_token_encrypted:
                        continue
                    token = decrypt_token(account.access_token_encrypted)
                    if not await _refresh_account_status(client, account, token, settings, db):
                        continue
                    created_at = account.created_at or datetime.now(timezone.utc)
                    connected_date = (
                        created_at.astimezone(LOCAL_TIMEZONE).date()
                        if created_at.tzinfo
                        else created_at.replace(tzinfo=timezone.utc).astimezone(LOCAL_TIMEZONE).date()
                    )
                    insights = None
                    for metric_names in ("views,content_views,reach", "content_views,reach", "views,reach", "reach"):
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


def unschedule_post(post_id: int) -> None:
    job_id = f"scheduled-post-{post_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


async def _detach_account_from_batches(
    db: AsyncSession,
    account: InstagramAccount,
) -> None:
    batches = (
        await db.scalars(
            select(PostingBatch).where(PostingBatch.owner_id == account.owner_id)
        )
    ).all()
    for batch in batches:
        account_ids = list(dict.fromkeys(json.loads(batch.account_ids or "[]")))
        if account.id not in account_ids:
            continue
        batch.account_ids = json.dumps(
            [account_id for account_id in account_ids if account_id != account.id]
        )
        pending_posts = (
            await db.scalars(
                select(ScheduledPost).where(
                    ScheduledPost.batch_id == batch.id,
                    ScheduledPost.account_id == account.id,
                    ScheduledPost.status.in_(PENDING_STATUSES),
                )
            )
        ).all()
        for post in pending_posts:
            unschedule_post(post.id)
            await db.delete(post)


async def schedule_pending_posts() -> None:
    """Restore pending schedules after a process restart."""
    async with SessionLocal() as db:
        posts = (
            await db.scalars(
                select(ScheduledPost).outerjoin(PostingBatch).where(
                    ScheduledPost.status.in_(PENDING_STATUSES),
                    (PostingBatch.status.is_(None) | (PostingBatch.status == "active")),
                )
            )
        ).all()
    for post in posts:
        schedule_post(post.id, post.scheduled_for)


async def _advance_loop_after_post(post_id: int) -> None:
    """Queue the next playlist item after this account drains its current queue."""
    async with SessionLocal() as db:
        post = await db.get(ScheduledPost, post_id)
        if post is None or post.batch_id is None or post.loop_index is None:
            return
        batch = await db.get(PostingBatch, post.batch_id)
        if batch is None or not batch.is_loop or batch.status != "active":
            return
        if post.account_id not in set(json.loads(batch.account_ids or "[]")):
            return
        pending = await db.scalar(
            select(ScheduledPost.id)
            .where(
                ScheduledPost.batch_id == batch.id,
                ScheduledPost.account_id == post.account_id,
                ScheduledPost.status.in_((*PENDING_STATUSES, "processing")),
            )
            .limit(1)
        )
        if pending is not None:
            return
        latest_post = await db.scalar(
            select(ScheduledPost)
            .where(
                ScheduledPost.batch_id == batch.id,
                ScheduledPost.account_id == post.account_id,
                ScheduledPost.loop_index.is_not(None),
            )
            .order_by(ScheduledPost.id.desc())
            .limit(1)
        )
        if latest_post is None or latest_post.status == "processing":
            return
        media_count = await db.scalar(
            select(func.count(func.distinct(ScheduledPost.loop_index))).where(
                ScheduledPost.batch_id == batch.id,
                ScheduledPost.loop_index.is_not(None),
            )
        )
        if not media_count:
            return
        next_index = (latest_post.loop_index + 1) % media_count
        template = await db.scalar(
            select(ScheduledPost)
            .where(
                ScheduledPost.batch_id == batch.id,
                ScheduledPost.loop_index == next_index,
            )
            .order_by(ScheduledPost.id)
            .limit(1)
        )
        account = await db.get(InstagramAccount, post.account_id)
        if template is None or account is None:
            return
        next_post = ScheduledPost(
            owner_id=batch.owner_id,
            account_id=account.id,
            batch_id=batch.id,
            media_url=template.media_url,
            original_media_url=template.original_media_url,
            drive_media_url=template.drive_media_url,
            drive_account_email=template.drive_account_email,
            drive_credentials_encrypted=template.drive_credentials_encrypted,
            storage_path=template.storage_path,
            media_type=template.media_type,
            caption=template.caption,
            loop_index=next_index,
            scheduled_for=datetime.now(timezone.utc)
            + timedelta(minutes=batch.loop_interval_minutes),
        )
        db.add(next_post)
        await db.flush()
        await db.commit()
        schedule_post(next_post.id, next_post.scheduled_for)


async def _advance_loop_safely(post_id: int) -> None:
    try:
        await _advance_loop_after_post(post_id)
    except Exception:
        logger.exception("Falha ao agendar a próxima mídia do Loop após o post %s", post_id)


def _drive_file_id(url: str | None) -> str | None:
    if not url:
        return None
    return parse_qs(urlparse(url).query).get("id", [None])[0]


async def _drive_media_rescue(post: ScheduledPost, settings) -> str | None:
    """Refresh a missing public object from Drive immediately before publishing."""
    if not post.drive_media_url or not post.drive_credentials_encrypted:
        return None
    file_id = _drive_file_id(post.drive_media_url)
    if not file_id:
        return None
    try:
        credentials = Credentials.from_authorized_user_info(
            json.loads(decrypt_token(post.drive_credentials_encrypted)),
            ["https://www.googleapis.com/auth/drive.readonly"],
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(GoogleAuthRequest())

        def download_and_upload() -> tuple[str, str | None]:
            service = build("drive", "v3", credentials=credentials, cache_discovery=False)
            metadata = service.files().get(fileId=file_id, fields="name,mimeType").execute()
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(
                buffer, service.files().get_media(fileId=file_id)
            )
            done = False
            while not done:
                _, done = downloader.next_chunk()
            content = buffer.getvalue()
            if len(content) > 50 * 1024 * 1024:
                raise RuntimeError("Arquivo do Drive excede o limite de 50 MB")
            storage_key = settings.supabase_service_role or settings.supabase_key
            if not settings.supabase_url or not storage_key:
                raise RuntimeError("Supabase Storage não configurado para resgatar a mídia")
            extension = urlparse(metadata.get("name", "")).path.rsplit(".", 1)[-1]
            filename = f"drive-{post.id}.{extension}" if extension else f"drive-{post.id}"
            path = f"drive-rescue/{post.id}/{filename}"
            client = create_client(settings.supabase_url, storage_key)
            bucket = client.storage.from_(settings.supabase_storage_bucket)
            bucket.upload(
                path,
                content,
                file_options={
                    "content-type": metadata.get("mimeType", "application/octet-stream"),
                    "upsert": True,
                },
            )
            return bucket.get_public_url(path), path

        media_url, storage_path = await asyncio.to_thread(download_and_upload)
        if not await _media_url_is_available(media_url):
            raise RuntimeError("O arquivo foi recuperado, mas a URL pública do Supabase não está acessível.")
        post.media_url = media_url
        post.original_media_url = media_url
        post.storage_path = storage_path
        return media_url
    except Exception as exc:
        post.error_message = f"Falha ao recuperar mídia do Drive: {str(exc)[:700]}"
        logger.exception(
            "Falha ao resgatar mídia do Drive para post %s (conta %s)",
            post.id,
            post.drive_account_email or "desconhecida",
        )
        return None


async def _media_url_is_available(media_url: str | None) -> bool:
    """Check whether the public object still exists before using Drive rescue."""
    if not media_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.head(media_url)
            if response.status_code in {405, 501}:
                response = await client.get(media_url, headers={"Range": "bytes=0-1023"})
            return response.status_code < 400
    except httpx.HTTPError:
        logger.warning("Não foi possível verificar a mídia armazenada: %s", media_url)
        return False


async def _publish(post_id: int) -> None:
    settings = get_settings()
    async with SessionLocal() as db:
        claimed = await db.execute(
            update(ScheduledPost)
            .where(ScheduledPost.id == post_id, ScheduledPost.status.in_(PENDING_STATUSES))
            .values(status="processing", error_at=None)
        )
        if claimed.rowcount != 1:
            return
        await db.commit()
        
        post = await db.get(ScheduledPost, post_id)
        if post is None:
            return
        if post.batch_id:
            batch = await db.get(PostingBatch, post.batch_id)
            if batch is not None and batch.status == "paused":
                post.status = "scheduled"
                await db.commit()
                return
            
        account = await db.get(InstagramAccount, post.account_id)
        if account is None or account.owner_id != post.owner_id:
            post.status = "failed"
            post.error_message = "Instagram account no longer belongs to this owner"
            post.error_at = datetime.now(timezone.utc)
            logger.error(
                "Falha na publicação do post %s para conta %s às %s: %s",
                post.id,
                post.account_id,
                post.error_at.isoformat(),
                post.error_message,
            )
            await db.commit()
            await _advance_loop_safely(post_id)
            return
        try:
            previous_connection_status = account.connection_status
            token = decrypt_token(account.access_token_encrypted)
            async with httpx.AsyncClient(timeout=30) as status_client:
                if not await _refresh_account_status(status_client, account, token, settings, db):
                    post.status = "blocked" if account.connection_status == "disconnected" else "failed"
                    post.error_message = account.status_reason or "A conta não está autorizada para publicar."
                    post.error_at = datetime.now(timezone.utc)
                    logger.error(
                        "Falha na publicação do post %s para conta %s às %s: %s",
                        post.id,
                        account.id,
                        post.error_at.isoformat(),
                        post.error_message,
                    )
                    await db.commit()
                    await _advance_loop_safely(post_id)
                    return
            
            # Este projeto usa Instagram Login, cujo token é válido em graph.instagram.com.
            base = f"https://graph.instagram.com/{settings.graph_api_version}"
            
            # CORREÇÃO 2: Forçar o tipo REELS caso o banco de dados envie VIDEO
            media_type = post.media_type.upper()
            if media_type == "VIDEO":
                media_type = "REELS"
            if media_type not in {"IMAGE", "REELS"}:
                raise RuntimeError(f"Tipo de mídia não suportado: {post.media_type}")
            if post.drive_media_url and not await _media_url_is_available(post.media_url):
                rescued_url = await _drive_media_rescue(post, settings)
                if not rescued_url:
                    raise RuntimeError(post.error_message or "A mídia local não existe e não foi possível resgatá-la do Drive.")
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
            post.published_at = datetime.now(timezone.utc)
            post.error_message = None
            post.error_at = None
        except Exception as exc:
            error_message = str(exc)[:1000]
            if _is_authentication_error(error_message):
                post.status = "blocked"
                account.connection_status = "disconnected"
                account.status_reason = error_message[:500]
                account.status_checked_at = datetime.now(timezone.utc)
                await _detach_account_from_batches(db, account)
                if previous_connection_status not in {"disconnected", "suspended", "error"}:
                    await _notify_account_connection_error(db, account)
            elif isinstance(exc, httpx.RequestError):
                post.status = "failed"
                account.connection_status = "error"
                account.status_reason = f"Falha de conexão ao publicar: {error_message[:450]}"
                account.status_checked_at = datetime.now(timezone.utc)
                await _detach_account_from_batches(db, account)
                if previous_connection_status not in {"disconnected", "suspended", "error"}:
                    await _notify_account_connection_error(db, account)
            else:
                post.status = "failed"
            post.error_message = error_message
            post.error_at = datetime.now(timezone.utc)
            logger.exception(
                "Falha ao publicar post %s para conta %s às %s: %s",
                post_id,
                post.account_id,
                post.error_at.isoformat(),
                exc,
            )
            
        await db.commit()
        await _advance_loop_safely(post_id)


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
