from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import logging
import io
import json
import asyncio
import re
from mimetypes import guess_type
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from supabase import create_client
from PIL import Image, ImageDraw
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .config import get_settings
from .db import get_db
from .jobs import PENDING_STATUSES, collect_instagram_insights, schedule_post
from .models import BotEvent, InstagramAccount, InstagramMetric, ScheduledPost, User
from .models import NotificationSubscription
from .oauth import (
    authorization_url,
    exchange_code,
    exchange_long_lived_token,
    fetch_instagram_business_account,
    fetch_profile,
    new_state,
)
from .observability import get_recent_logs
from .security import encrypt_token, hash_password, verify_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
UPLOAD_DIR = Path("uploads")
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{2,80}$")
LOCAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")
ACCOUNT_STATUS_CLASSES = {"connected", "suspended", "error", "pending"}


def account_status_classes(accounts: list[InstagramAccount]) -> dict[int, str]:
    return {
        account.id: (
            "connected"
            if account.connection_status == "active"
            else account.connection_status
            if account.connection_status in ACCOUNT_STATUS_CLASSES
            else "error"
        )
        for account in accounts
    }


def _metric_views_by_account(
    metric_rows: list[InstagramMetric],
    account_ids: list[int],
) -> dict[int, int]:
    result: dict[int, int] = {}
    for account_id in account_ids:
        rows = sorted(
            (row for row in metric_rows if row.account_id == account_id),
            key=lambda row: row.metric_date,
            reverse=True,
        )
        result[account_id] = int(
            sum(row.impressions for row in rows)
            if rows
            else 0
        )
    return result


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    user = await db.get(User, int(user_id))
    if not user:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    if user.role == "collaborator" and request.url.path not in {
        "/hub", "/auth/instagram/start", "/auth/callback", "/media/upload",
        "/logout", "/profile", "/collaborators",
    } and not request.url.path.startswith(("/accounts/", "/api/drive/", "/api/notifications/")):
        request.session["access_notice"] = "Acesso restrito: colaboradores usam apenas o Hub de Contas."
        raise HTTPException(status_code=307, headers={"Location": "/hub"})
    return user


def workspace_owner_id(user: User) -> int:
    return user.parent_id if user.role == "collaborator" and user.parent_id else user.id


async def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=307, headers={"Location": "/hub"})
    return user


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


def parse_scheduled_datetime(value: str) -> datetime:
    """Interpret datetime-local values in Sao Paulo and store the UTC instant."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="scheduled_for inválido") from exc
    local_value = parsed.replace(tzinfo=LOCAL_TIMEZONE) if parsed.tzinfo is None else parsed
    return local_value.astimezone(timezone.utc)


def local_scheduled_datetime(value: datetime) -> str:
    """Format a stored UTC value as the user's configured local wall-clock time."""
    utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return utc_value.astimezone(LOCAL_TIMEZONE).strftime("%d/%m/%Y %H:%M")


templates.env.globals["local_scheduled_datetime"] = local_scheduled_datetime


def _notification_payload(total_views: int, account_count: int) -> str:
    return f"Auto-Insta: {total_views:,} visualizações em {account_count} conta(s).".replace(",", ".")


async def _send_web_push_notifications(db: AsyncSession, user_id: int, message: str) -> int:
    settings = get_settings()
    if not settings.vapid_private_key or not settings.vapid_subject:
        return 0
    from pywebpush import WebPushException, webpush

    subscriptions = (await db.scalars(
        select(NotificationSubscription).where(NotificationSubscription.user_id == user_id)
    )).all()
    sent = 0
    for subscription in subscriptions:
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps({"title": "Auto-Insta", "body": message}),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
            sent += 1
        except WebPushException as exc:
            if getattr(exc, "response", None) is not None and exc.response.status_code in {404, 410}:
                await db.delete(subscription)
            else:
                logger.warning("Falha ao enviar notificação push: %s", exc)
    await db.commit()
    return sent


def _google_flow(state: str | None = None) -> Flow:
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET não configurados")
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES, state=state)
    flow.redirect_uri = settings.google_redirect_uri
    return flow


def _thumbnail_bytes(content: bytes, media_type: str) -> bytes:
    if media_type.startswith("image/"):
        image = Image.open(io.BytesIO(content)).convert("RGB")
        image.thumbnail((640, 640))
    else:
        image = Image.new("RGB", (640, 360), "#111827")
        draw = ImageDraw.Draw(image)
        draw.text((24, 160), "Pré-visualização do vídeo", fill="#38bdf8")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=72, optimize=True)
    return output.getvalue()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})


@router.post("/media/upload")
async def upload_media(
    media: UploadFile = File(...),
    user: User = Depends(current_user),
):
    extension = Path(media.filename or "").suffix.lower()
    if not extension:
        raise HTTPException(status_code=400, detail="Arquivo sem extensão")
    filename = f"{uuid4().hex}{extension}"
    upload_content_type = media.content_type or guess_type(filename)[0] or "application/octet-stream"
    try:
        content = await media.read(MAX_UPLOAD_SIZE + 1)
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="Arquivo excede o limite de 50 MB")
    except HTTPException:
        raise
    finally:
        await media.close()
    settings = get_settings()
    storage_key = settings.supabase_service_role or settings.supabase_key
    if not settings.supabase_url or not storage_key:
        raise HTTPException(status_code=503, detail="Supabase Storage não configurado")

    thumbnail_content = _thumbnail_bytes(content, upload_content_type)
    def upload_to_storage() -> dict[str, str]:
        client = create_client(settings.supabase_url, storage_key)
        folder = uuid4().hex
        path = f"{folder}/{filename}"
        thumbnail_path = f"{folder}/thumbnail.jpg"
        client.storage.from_(settings.supabase_storage_bucket).upload(
            path,
            content,
            file_options={
                "content-type": upload_content_type,
                "upsert": False,
            },
        )
        client.storage.from_(settings.supabase_storage_bucket).upload(
            thumbnail_path, thumbnail_content,
            file_options={"content-type": "image/jpeg", "upsert": False},
        )
        bucket = client.storage.from_(settings.supabase_storage_bucket)
        return {
            "url": bucket.get_public_url(path),
            "thumbnail_url": bucket.get_public_url(thumbnail_path),
            "storage_path": path,
            "thumbnail_storage_path": thumbnail_path,
        }

    try:
        stored = await asyncio.to_thread(upload_to_storage)
    except Exception as exc:
        logger.exception("Falha ao armazenar mídia %s no Supabase Storage", media.filename)
        error_message = str(exc).strip().replace("\n", " ")
        raise HTTPException(
            status_code=502,
            detail=f"Falha no Supabase Storage: {error_message[:300]}",
        ) from exc
    return {
        **stored,
        "media_type": "VIDEO" if upload_content_type.startswith("video/") else "IMAGE",
    }


@router.get("/auth/google/start")
async def google_start(request: Request, user: User = Depends(current_user)):
    state = new_state()
    flow = _google_flow(state)
    request.session["google_oauth_state"] = state
    authorization_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return RedirectResponse(authorization_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/auth/google/callback")
async def google_callback(request: Request):
    state = request.session.pop("google_oauth_state", None)
    if not state or request.query_params.get("state") != state:
        raise HTTPException(status_code=400, detail="Estado OAuth Google inválido")
    flow = _google_flow(state)
    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as exc:
        logger.exception("Falha no callback OAuth Google")
        raise HTTPException(status_code=502, detail="Não foi possível autenticar no Google") from exc
    request.session["google_credentials"] = flow.credentials.to_json()
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/drive/files")
async def drive_files(request: Request, user: User = Depends(current_user)):
    serialized = request.session.get("google_credentials")
    if not serialized:
        raise HTTPException(status_code=401, detail="Autentique-se no Google antes de importar do Drive")
    credentials = Credentials.from_authorized_user_info(json.loads(serialized), GOOGLE_SCOPES)
    def list_files():
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return service.files().list(
            q="trashed = false and (mimeType contains 'image/' or mimeType contains 'video/')",
            fields="files(id,name,mimeType,size,thumbnailLink,modifiedTime)",
            orderBy="modifiedTime desc", pageSize=100,
        ).execute().get("files", [])
    try:
        return {"files": await asyncio.to_thread(list_files)}
    except Exception as exc:
        logger.exception("Falha ao listar arquivos do Google Drive")
        raise HTTPException(status_code=502, detail="Não foi possível listar o Google Drive") from exc


@router.post("/api/drive/import")
async def drive_import(
    request: Request,
    file_id: str = Form(...),
    user: User = Depends(current_user),
):
    serialized = request.session.get("google_credentials")
    if not serialized:
        raise HTTPException(status_code=401, detail="Autentique-se no Google antes de importar do Drive")
    credentials = Credentials.from_authorized_user_info(json.loads(serialized), GOOGLE_SCOPES)
    settings = get_settings()
    storage_key = settings.supabase_service_role or settings.supabase_key
    if not settings.supabase_url or not storage_key:
        raise HTTPException(status_code=503, detail="Supabase Storage não configurado")
    def download_and_upload() -> dict[str, str]:
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        metadata = service.files().get(fileId=file_id, fields="name,mimeType").execute()
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, service.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        content = buffer.getvalue()
        if len(content) > MAX_UPLOAD_SIZE:
            raise ValueError("Arquivo excede o limite de 50 MB")
        media_type = metadata["mimeType"]
        filename = f"{uuid4().hex}{Path(metadata['name']).suffix.lower()}"
        folder = uuid4().hex
        path, thumb_path = f"{folder}/{filename}", f"{folder}/thumbnail.jpg"
        client = create_client(settings.supabase_url, storage_key)
        bucket = client.storage.from_(settings.supabase_storage_bucket)
        bucket.upload(path, content, file_options={"content-type": media_type, "upsert": False})
        bucket.upload(thumb_path, _thumbnail_bytes(content, media_type),
                      file_options={"content-type": "image/jpeg", "upsert": False})
        return {"url": bucket.get_public_url(path), "thumbnail_url": bucket.get_public_url(thumb_path),
                "storage_path": path, "thumbnail_storage_path": thumb_path,
                "media_type": "VIDEO" if media_type.startswith("video/") else "IMAGE"}
    try:
        return await asyncio.to_thread(download_and_upload)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Falha ao importar arquivo do Google Drive")
        raise HTTPException(status_code=502, detail="Não foi possível importar o arquivo") from exc


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    user = await db.get(User, int(request.session["user_id"]))
    return RedirectResponse("/hub" if user and user.role == "collaborator" else "/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(""),
    username: str = Form(""),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    username = username.strip()
    form_data = {"request": request, "username": username}
    if not USERNAME_PATTERN.fullmatch(username):
        return templates.TemplateResponse(
            "register.html",
            {
                **form_data,
                "error": "Nome de usuário inválido. Use 2 a 80 letras, números, ponto, hífen ou underline.",
            },
            status_code=400,
        )
    username_in_use = await db.scalar(select(User).where(User.username == username))
    if username_in_use:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Nome de usuário já está em uso"}, status_code=409
        )
    user = User(
        email=f"{username}@local.invalid",
        username=username,
        password_hash=hash_password(password),
        role="admin",
    )
    db.add(user)
    await db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/profile")
async def update_profile(
    username: str = Form(...),
    password: str = Form(""),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(status_code=400, detail="Nome de usuário inválido")
    existing = await db.scalar(select(User).where(User.username == username, User.id != user.id))
    if existing:
        raise HTTPException(status_code=409, detail="Nome de usuário já está em uso")
    user.username = username
    if password:
        user.password_hash = hash_password(password)
    await db.commit()
    return RedirectResponse("/hub" if user.role == "collaborator" else "/dashboard#overview", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/notifications/vapid-public-key")
async def vapid_public_key(user: User = Depends(current_user)):
    del user
    public_key = get_settings().vapid_public_key
    if not public_key:
        raise HTTPException(status_code=503, detail="VAPID não configurado")
    return {"public_key": public_key}


@router.post("/api/notifications/subscribe")
async def subscribe_notifications(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    payload = await request.json()
    endpoint = str(payload.get("endpoint", "")).strip()
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(status_code=400, detail="Assinatura de notificação inválida")
    subscription = await db.scalar(
        select(NotificationSubscription).where(NotificationSubscription.endpoint == endpoint)
    )
    if subscription is None:
        subscription = NotificationSubscription(
            user_id=user.id, endpoint=endpoint,
            p256dh=str(keys["p256dh"]), auth=str(keys["auth"]),
        )
        db.add(subscription)
    else:
        subscription.user_id = user.id
        subscription.p256dh = str(keys["p256dh"])
        subscription.auth = str(keys["auth"])
    await db.commit()
    return {"subscribed": True}


@router.post("/api/notifications/test")
async def test_notifications(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    accounts = (await db.scalars(
        select(InstagramAccount).where(InstagramAccount.owner_id == owner_id)
    )).all()
    account_ids = [account.id for account in accounts]
    rows = (await db.scalars(
        select(InstagramMetric).where(InstagramMetric.account_id.in_(account_ids))
    )).all() if account_ids else []
    total_views = sum(int(row.impressions or 0) for row in rows)
    message = _notification_payload(total_views, len(accounts))
    sent = await _send_web_push_notifications(db, user.id, message)
    return {"sent": sent, "message": message, "accounts": len(accounts), "views": total_views}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(
    request: Request,
    identifier: str | None = Form(None),
    email: str | None = Form(None),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    normalized = (identifier or email or "").strip()
    user = await db.scalar(select(User).where(User.username == normalized))
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "identifier": identifier, "error": "Credenciais inválidas"}, status_code=401
        )
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/hub" if user.role == "collaborator" else "/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    period_days: int = 7,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role == "collaborator":
        return RedirectResponse("/hub", status_code=status.HTTP_303_SEE_OTHER)
    owner_id = workspace_owner_id(user)
    accounts = (await db.scalars(select(InstagramAccount).where(InstagramAccount.owner_id == owner_id))).all()
    posts = (
        await db.scalars(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.account))
            .where(ScheduledPost.owner_id == owner_id)
            .order_by(ScheduledPost.scheduled_for.desc())
        )
    ).all()
    today = datetime.now(timezone.utc).date()
    today_posts = [post for post in posts if post.created_at and post.created_at.date() == today]
    if period_days not in {7, 30, 90}:
        period_days = 7
    metric_start = datetime.now(timezone.utc) - timedelta(days=period_days - 1)
    metric_rows = (
        await db.scalars(
            select(InstagramMetric).where(
                InstagramMetric.account_id.in_([a.id for a in accounts]),
                InstagramMetric.metric_date >= metric_start,
            )
        )
    ).all() if accounts else []
    events = (
        await db.scalars(
            select(BotEvent).where(
                or_(
                    BotEvent.account_id.in_([a.id for a in accounts]),
                    BotEvent.account_id.is_(None),
                )
            )
        )
    ).all() if accounts else (await db.scalars(select(BotEvent).where(BotEvent.account_id.is_(None)))).all()
    views_by_account = _metric_views_by_account(metric_rows, [account.id for account in accounts])
    total_views = sum(views_by_account.values())
    account_views = views_by_account
    event_counts = {event_type: sum(event.event_type == event_type for event in events) for event_type in (
        "link_click", "lead_initiated", "pix_generated", "pix_paid"
    )}
    lead_count = event_counts["lead_initiated"]
    generated_count = event_counts["pix_generated"]
    paid_count = event_counts["pix_paid"]
    funnel_rates = {
        "views_to_leads": round(lead_count / total_views * 100, 2) if total_views else 0,
        "leads_to_pix": round(generated_count / lead_count * 100, 2) if lead_count else 0,
        "pix_to_paid": round(paid_count / generated_count * 100, 2) if generated_count else 0,
    }
    metrics = {
        "active_accounts": len(accounts),
        "today_posts": len(today_posts),
        "daily_views": total_views,
        "total_views": total_views,
        "average_views": round(total_views / max(len(accounts), 1)),
        "funnel": event_counts,
        "funnel_rates": funnel_rates,
        "pix_status": {
            "paid": event_counts["pix_paid"],
            "pending": sum(event.event_type == "pix_pending" for event in events),
            "generated": event_counts["pix_generated"],
        },
        "published": sum(post.status == "published" for post in posts),
        "pending": sum(post.status == "scheduled" for post in posts),
        "failed": sum(post.status == "failed" for post in posts),
    }
    volume_days = []
    for offset in range(period_days - 1, -1, -1):
        day = datetime.now(timezone.utc).date() - timedelta(days=offset)
        volume_days.append({
            "label": day.strftime("%d/%m"),
            "published": sum(post.status == "published" and post.created_at and post.created_at.date() == day for post in posts),
            "interactions": 0,
        })
    response = templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "accounts": accounts,
            "account_status_classes": account_status_classes(accounts),
            "account_views": account_views,
            "posts": posts,
            "metrics": metrics,
            "app_version": get_settings().app_version,
            "deploy_timestamp": get_settings().deploy_timestamp or "não informado",
            "chart_status": {
                "published": sum(post.status == "published" for post in posts),
                "scheduled": sum(post.status in {"scheduled", "pending", "aguardando", "processing"} for post in posts),
                "failed": sum(post.status == "failed" for post in posts),
            },
            "volume_days": volume_days,
            "period_days": period_days,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/accounts/import")
async def import_accounts(
    request: Request,
    accounts_text: str = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    created = 0
    for line in accounts_text.splitlines():
        parts = [part.strip() for part in line.split(";", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        username, _password = parts
        db.add(InstagramAccount(
            owner_id=owner_id,
            instagram_user_id=f"pending-{uuid4().hex}",
            username=username[:120],
            access_token_encrypted="",
            connection_status="pending",
            status_reason="Conta importada. Conecte-a para validar o acesso.",
        ))
        created += 1
    if not created:
        raise HTTPException(status_code=400, detail="Use uma conta por linha no formato usuario;senha")
    await db.commit()
    request.session["access_notice"] = f"{created} conta(s) importada(s) como pendente(s)."
    return RedirectResponse("/dashboard#accounts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/delete-selected")
async def delete_selected_accounts(
    account_ids: list[int] = Form(...),
    return_to: str = Form("/dashboard#accounts"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if not account_ids:
        raise HTTPException(status_code=400, detail="Nenhuma conta selecionada")
    await db.execute(delete(InstagramAccount).where(
        InstagramAccount.id.in_(set(account_ids)),
        InstagramAccount.owner_id == workspace_owner_id(user),
    ))
    await db.commit()
    destination = return_to if return_to in {"/dashboard#accounts", "/hub"} else "/dashboard#accounts"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/hub", response_class=HTMLResponse)
async def hub(request: Request, status_filter: str = "", user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    owner_id = workspace_owner_id(user)
    query = select(InstagramAccount).where(InstagramAccount.owner_id == owner_id)
    if status_filter in ACCOUNT_STATUS_CLASSES:
        query = query.where(InstagramAccount.connection_status == status_filter)
    accounts = (await db.scalars(query)).all()
    account_views = {}
    for account in accounts:
        rows = (await db.scalars(select(InstagramMetric).where(
            InstagramMetric.account_id == account.id
        ).order_by(InstagramMetric.metric_date.desc()).limit(10))).all()
        account_views[account.id] = _metric_views_by_account(rows, [account.id])[account.id]
    response = templates.TemplateResponse("hub.html", {"request": request, "user": user, "accounts": accounts, "account_status_classes": account_status_classes(accounts), "account_views": account_views, "status_filter": status_filter, "notice": request.session.pop("access_notice", None)})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/metrics/refresh")
async def refresh_metrics(user: User = Depends(current_user)):
    await collect_instagram_insights()
    response = JSONResponse({"refreshed": True})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/metrics", response_class=HTMLResponse)
async def metrics_page(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    owner_id = workspace_owner_id(user)
    accounts = (await db.scalars(select(InstagramAccount).where(InstagramAccount.owner_id == owner_id))).all()
    account_ids = [account.id for account in accounts]
    event_query = select(BotEvent).options(selectinload(BotEvent.account)).where(
        or_(BotEvent.account_id.in_(account_ids), BotEvent.account_id.is_(None))
    ).order_by(BotEvent.timestamp.desc()) if account_ids else select(BotEvent).options(selectinload(BotEvent.account)).where(BotEvent.account_id.is_(None)).order_by(BotEvent.timestamp.desc())
    events = (await db.scalars(event_query)).all()
    counts = {event_type: sum(event.event_type == event_type for event in events) for event_type in (
        "link_click", "lead_initiated", "pix_generated", "pix_paid"
    )}
    paid_events = [event for event in events if event.event_type == "pix_paid"]
    generated_events = [event for event in events if event.event_type == "pix_generated"]
    today = datetime.now(timezone.utc).date()
    daily_activity = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_events = [event for event in events if event.timestamp and event.timestamp.date() == day]
        daily_activity.append({
            "label": day.strftime("%a").capitalize(),
            "revenue": round(sum(event.value for event in day_events if event.event_type == "pix_paid"), 2),
            "leads": sum(event.event_type == "lead_initiated" for event in day_events),
        })
    return templates.TemplateResponse("metrics.html", {
        "request": request, "user": user,
        "sharkbot_webhook_url": get_settings().sharkbot_webhook_url,
        "bot_name": "Sharkbot",
        "approved_sales": sum(event.value for event in paid_events),
        "conversion_rate": (len(paid_events) / len(generated_events) * 100) if generated_events else 0,
        "total_starts": counts["lead_initiated"],
        "average_ticket": (sum(event.value for event in paid_events) / len(paid_events)) if paid_events else 0,
        "daily_activity": daily_activity,
        "funnel": [counts["link_click"], counts["lead_initiated"], counts["pix_generated"], counts["pix_paid"]],
        "pix_status": {
            "paid": counts["pix_paid"],
            "pending": sum(event.event_type == "pix_pending" for event in events),
            "generated": counts["pix_generated"],
        },
        "events": events,
    })


@router.post("/collaborators")
async def create_collaborator(
    username: str = Form(...),
    password: str = Form(...),
    user: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(status_code=400, detail="Nome de usuário inválido")
    if await db.scalar(select(User).where(User.username == username)):
        raise HTTPException(status_code=409, detail="Nome de usuário já está em uso")
    collaborator = User(
        email=f"{username}@collaborator.local",
        username=username,
        password_hash=hash_password(password),
        role="collaborator",
        parent_id=user.id,
    )
    db.add(collaborator)
    await db.commit()
    return RedirectResponse("/dashboard#overview", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/status")
async def api_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    posts = (
        await db.scalars(
            select(ScheduledPost)
            .options(selectinload(ScheduledPost.account))
            .where(ScheduledPost.owner_id == workspace_owner_id(user))
            .order_by(ScheduledPost.scheduled_for.desc())
        )
    ).all()
    account_ids = [account.id for account in (
        await db.scalars(select(InstagramAccount).where(
            InstagramAccount.owner_id == workspace_owner_id(user)
        ))
    ).all()]
    bot_query = select(BotEvent).where(BotEvent.account_id.is_(None))
    if account_ids:
        bot_query = select(BotEvent).where(
            or_(BotEvent.account_id.in_(account_ids), BotEvent.account_id.is_(None))
        )
    bot_events = (await db.scalars(bot_query)).all()
    metric_rows = (
        await db.scalars(
            select(InstagramMetric).where(InstagramMetric.account_id.in_(account_ids))
        )
    ).all() if account_ids else []
    account_views = _metric_views_by_account(metric_rows, account_ids)
    total_views = sum(account_views.values())
    bot_counts = {
        event_type: sum(event.event_type == event_type for event in bot_events)
        for event_type in ("link_click", "lead_initiated", "pix_generated", "pix_paid", "pix_pending")
    }
    response = JSONResponse({
        "metrics": {
            "pending": sum(post.status in {"scheduled", "processing", "aguardando", "pending"} for post in posts),
            "published": sum(post.status == "published" for post in posts),
            "failed": sum(post.status == "failed" for post in posts),
            "total_views": total_views,
        },
        "sharkbot": bot_counts,
        "account_views": account_views,
        "posts": [
            {
                "id": post.id,
                "media_url": post.media_url,
                "media_type": post.media_type,
                "status": post.status,
                "scheduled_for": local_scheduled_datetime(post.scheduled_for),
                "account": post.account.username if post.account else "",
            }
            for post in posts
        ],
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/api/logs")
async def api_logs(user: User = Depends(current_user)):
    return {"logs": get_recent_logs()}


@router.get("/auth/instagram/start")
async def instagram_start(request: Request, user: User = Depends(current_user)):
    state = new_state()
    request.session["instagram_oauth_state"] = state
    redirect_url = authorization_url(state)
    logger.warning("Instagram OAuth authorization URL: %s", redirect_url)
    return RedirectResponse(redirect_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/auth/callback")
async def instagram_callback(
    request: Request, code: str | None = None, state: str | None = None, db: AsyncSession = Depends(get_db)
):
    if not request.session.get("user_id") or not state or state != request.session.pop("instagram_oauth_state", None):
        raise HTTPException(status_code=400, detail="OAuth state inválido")
    if not code:
        raise HTTPException(status_code=400, detail="Código OAuth ausente")
    try:
        token_data = await exchange_code(code)
        short_token = token_data["access_token"]
        long_lived_token = await exchange_long_lived_token(short_token)
        profile = await fetch_profile(long_lived_token)
        business_account = await fetch_instagram_business_account(
            long_lived_token,
            str(profile.get("user_id") or profile.get("id")),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha no OAuth do Instagram: {exc}") from exc
    user = await db.get(User, int(request.session["user_id"]))
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    owner_id = workspace_owner_id(user)
    profile_id = str(profile.get("user_id") or profile["id"])
    account_ids = {profile_id}
    if business_account:
        account_ids.add(business_account["instagram_user_id"])
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.owner_id == owner_id,
            InstagramAccount.instagram_user_id.in_(account_ids),
        )
    )
    if account:
        if business_account:
            account.instagram_user_id = business_account["instagram_user_id"]
            account.facebook_page_id = business_account.get("page_id")
        account.username = profile.get("username", account.username)
        account.profile_picture_url = profile.get("profile_picture_url", account.profile_picture_url)
        account.access_token_encrypted = encrypt_token(long_lived_token)
    else:
        db.add(
            InstagramAccount(
                owner_id=owner_id,
                instagram_user_id=(
                    business_account["instagram_user_id"]
                    if business_account
                    else str(profile.get("user_id") or profile["id"])
                ),
                facebook_page_id=business_account.get("page_id") if business_account else None,
                username=profile.get("username", ""),
                profile_picture_url=profile.get("profile_picture_url"),
                access_token_encrypted=encrypt_token(long_lived_token),
            )
        )
    await db.commit()
    return RedirectResponse("/hub" if user.role == "collaborator" else "/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/delete")
async def delete_account(
    account_id: int,
    return_to: str = Form("/hub"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id, InstagramAccount.owner_id == owner_id
        )
    )
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    await db.delete(account)
    await db.commit()
    default_destination = "/hub" if user.role == "collaborator" else "/dashboard#accounts"
    destination = return_to if return_to in {"/dashboard#accounts", "/hub"} else default_destination
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/auto-reply")
async def update_auto_reply(
    account_id: int,
    direct_enabled: bool = Form(False),
    direct_text: str = Form(""),
    comment_enabled: bool = Form(False),
    comment_text: str = Form(""),
    apply_all: bool = Form(False),
    enabled: bool | None = Form(None),
    text: str | None = Form(None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id, InstagramAccount.owner_id == owner_id
        )
    )
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    direct_enabled = direct_enabled if enabled is None else enabled
    direct_text = (direct_text if text is None else text)[:2000]
    target_accounts = (
        (await db.scalars(select(InstagramAccount).where(InstagramAccount.owner_id == owner_id))).all()
        if apply_all
        else [account]
    )
    for target in target_accounts:
        target.direct_reply_enabled = direct_enabled
        target.direct_reply_text = direct_text
        target.comment_reply_enabled = comment_enabled
        target.comment_reply_text = comment_text[:2000]
        target.auto_reply_enabled = target.direct_reply_enabled or target.comment_reply_enabled
        target.auto_reply_text = target.direct_reply_text or target.comment_reply_text
    await db.commit()
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts")
async def create_post(
    request: Request,
    account_id: int = Form(...),
    media_url: str = Form(...),
    media_type: str = Form("IMAGE"),
    caption: str = Form(""),
    scheduled_for: str = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id, InstagramAccount.owner_id == owner_id
        )
    )
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    when = parse_scheduled_datetime(scheduled_for)
    if when <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="O horário do agendamento deve estar no futuro")
    media_type = media_type.upper()
    if media_type == "VIDEO":
        media_type = "REELS"
    if media_type not in {"IMAGE", "REELS"}:
        raise HTTPException(status_code=400, detail="media_type deve ser IMAGE ou REELS")
    post = ScheduledPost(
        owner_id=owner_id,
        account_id=account.id,
        media_url=media_url,
        media_type=media_type,
        caption=caption,
        scheduled_for=when,
    )
    db.add(post)
    await db.flush()
    await db.commit()
    schedule_post(post.id, post.scheduled_for)
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/{post_id}/delete")
async def delete_post(
    post_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    post = await db.scalar(
        select(ScheduledPost).where(
            ScheduledPost.id == post_id,
            ScheduledPost.owner_id == owner_id,
        )
    )
    if not post:
        raise HTTPException(status_code=404, detail="Publicação não encontrada")
    await db.delete(post)
    await db.commit()
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/delete-selected")
async def delete_selected_posts(
    post_ids: list[int] = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if not post_ids:
        raise HTTPException(status_code=400, detail="Nenhuma publicação selecionada")
    owner_id = workspace_owner_id(user)
    await db.execute(
        delete(ScheduledPost).where(
            ScheduledPost.id.in_(set(post_ids)),
            ScheduledPost.owner_id == owner_id,
        )
    )
    await db.commit()
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/clear-failed")
async def clear_failed_posts(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    await db.execute(
        delete(ScheduledPost).where(
            ScheduledPost.owner_id == owner_id,
            ScheduledPost.status == "failed",
        )
    )
    await db.commit()
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/publish-selected")
async def publish_selected_posts(
    post_ids: list[int] = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    posts = (
        await db.scalars(
            select(ScheduledPost).where(
                ScheduledPost.id.in_(post_ids),
                ScheduledPost.owner_id == owner_id,
                ScheduledPost.status.in_(PENDING_STATUSES),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    for post in posts:
        post.scheduled_for = now
    await db.commit()
    for post in posts:
        schedule_post(post.id, now)
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/bulk")
async def create_bulk_posts(
    account_ids: list[int] = Form(...),
    media_urls: list[str] = Form(...),
    media_types: list[str] = Form(...),
    thumbnail_urls: list[str] = Form(default=[]),
    storage_paths: list[str] = Form(default=[]),
    thumbnail_storage_paths: list[str] = Form(default=[]),
    captions: list[str] = Form(default=[]),
    caption_mode: str = Form("global"),
    caption: str = Form(""),
    scheduled_for: str = Form(...),
    interval_minutes: int = Form(1),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    if interval_minutes < 1:
        raise HTTPException(status_code=400, detail="O intervalo mínimo é de 1 minuto")
    if not media_urls or len(media_urls) != len(media_types):
        raise HTTPException(status_code=400, detail="Lista de mídias inválida")
    if caption_mode not in {"global", "individual"}:
        raise HTTPException(status_code=400, detail="Modo de legenda inválido")
    if caption_mode == "global":
        captions = [caption[:2200]] * len(media_urls)
    elif len(media_urls) != len(captions):
        raise HTTPException(status_code=400, detail="Lista de legendas inválida")
    accounts = (
        await db.scalars(
            select(InstagramAccount).where(
                InstagramAccount.id.in_(account_ids), InstagramAccount.owner_id == owner_id
            )
        )
    ).all()
    if len(accounts) != len(set(account_ids)) or not accounts:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    accounts_by_id = {account.id: account for account in accounts}
    ordered_accounts = [accounts_by_id[account_id] for account_id in account_ids]
    first_time = parse_scheduled_datetime(scheduled_for)
    if first_time <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="O horário do agendamento deve estar no futuro")

    posts_to_schedule = []
    for media_index, (media_url, media_type, caption) in enumerate(zip(media_urls, media_types, captions)):
        normalized_type = media_type.upper()
        if normalized_type == "VIDEO":
            normalized_type = "REELS"
        if normalized_type not in {"IMAGE", "REELS"}:
            raise HTTPException(status_code=400, detail="media_type deve ser IMAGE ou REELS")
        for account_index, account in enumerate(ordered_accounts):
            sequence_index = media_index * len(ordered_accounts) + account_index
            post = ScheduledPost(
                owner_id=owner_id,
                account_id=account.id,
                media_url=media_url,
                original_media_url=media_url,
                thumbnail_url=thumbnail_urls[media_index] if media_index < len(thumbnail_urls) else media_url,
                storage_path=storage_paths[media_index] if media_index < len(storage_paths) else None,
                thumbnail_storage_path=thumbnail_storage_paths[media_index] if media_index < len(thumbnail_storage_paths) else None,
                media_type=normalized_type,
                caption=caption,
                scheduled_for=first_time + timedelta(minutes=sequence_index * interval_minutes),
            )
            db.add(post)
            posts_to_schedule.append(post)
    await db.flush()
    await db.commit()
    for post in posts_to_schedule:
        schedule_post(post.id, post.scheduled_for)
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)
