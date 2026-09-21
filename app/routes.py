from datetime import datetime, timedelta, timezone

import logging
import io
import json
import asyncio
import re
import httpx
from mimetypes import guess_type
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select, update
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
from .jobs import (
    PENDING_STATUSES,
    _refresh_account_status,
    collect_instagram_insights,
    schedule_post,
    unschedule_post,
)
from .models import (
    AutomationRule,
    BotEvent,
    InstagramAccount,
    InstagramMetric,
    PostingBatch,
    ScheduledPost,
    User,
)
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
from .security import decrypt_token, encrypt_token, hash_password, verify_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
UPLOAD_DIR = Path("uploads")
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{2,80}$")
LOCAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")
ACCOUNT_STATUS_CLASSES = {"connected", "disconnected", "error", "pending"}


async def _remove_stale_pending_accounts(db: AsyncSession, owner_id: int) -> None:
    accounts = (
        await db.scalars(
            select(InstagramAccount).where(InstagramAccount.owner_id == owner_id)
        )
    ).all()
    connected_by_username = {
        account.username.strip().lower(): account
        for account in accounts
        if account.connection_status != "pending" and account.username.strip()
    }
    changed = False
    for pending_account in accounts:
        target = connected_by_username.get(pending_account.username.strip().lower())
        if pending_account.connection_status != "pending" or target is None or target.id == pending_account.id:
            continue
        await db.execute(
            update(ScheduledPost)
            .where(ScheduledPost.account_id == pending_account.id)
            .values(account_id=target.id)
        )
        await db.execute(
            update(InstagramMetric)
            .where(InstagramMetric.account_id == pending_account.id)
            .values(account_id=target.id)
        )
        await db.execute(
            update(BotEvent)
            .where(BotEvent.account_id == pending_account.id)
            .values(account_id=target.id)
        )
        await db.delete(pending_account)
        changed = True
    if changed:
        await db.commit()


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


def _meta_api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    return f"Instagram API {response.status_code}: {str(payload)[:900]}"


async def _media_is_public(media_url: str | None) -> tuple[bool, str | None]:
    if not media_url:
        return False, "A publicação não possui uma URL de mídia armazenada."
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.head(media_url)
            if response.status_code in {405, 403}:
                response = await client.get(
                    media_url,
                    headers={"Range": "bytes=0-1023"},
                )
            if response.status_code >= 400:
                return False, f"A mídia não está acessível (HTTP {response.status_code})."
            return True, None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Mídia indisponível para retry: %s", exc)
        return False, "A mídia não está acessível pela internet."


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


def _google_drive_context(credentials: Credentials) -> tuple[str, str]:
    """Return the Drive account and encrypted credentials for a later media rescue."""
    credentials_json = credentials.to_json()
    try:
        service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
        email = service.userinfo().get().execute().get("email", "")
    except Exception:
        email = ""
    return email, encrypt_token(credentials_json)


def _list_drive_children(service, folder_id: str) -> list[dict]:
    escaped_folder_id = folder_id.replace("'", "\\'")
    entries: list[dict] = []
    page_token = None
    while True:
        response = service.files().list(
            q=(
                f"trashed = false and '{escaped_folder_id}' in parents and "
                "(mimeType = 'application/vnd.google-apps.folder' or "
                "mimeType contains 'image/' or mimeType contains 'video/')"
            ),
            fields="nextPageToken,files(id,name,mimeType,size,thumbnailLink,modifiedTime,parents)",
            orderBy="modifiedTime desc",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        entries.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return entries


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
async def drive_files(
    request: Request,
    folder_id: str = "root",
    user: User = Depends(current_user),
):
    serialized = request.session.get("google_credentials")
    if not serialized:
        raise HTTPException(status_code=401, detail="Autentique-se no Google antes de importar do Drive")
    credentials = Credentials.from_authorized_user_info(json.loads(serialized), GOOGLE_SCOPES)
    def list_files():
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return _list_drive_children(service, folder_id)
    try:
        entries = await asyncio.to_thread(list_files)
        return {
            "folder_id": folder_id,
            "folders": [entry for entry in entries if entry["mimeType"] == "application/vnd.google-apps.folder"],
            "files": [entry for entry in entries if entry["mimeType"] != "application/vnd.google-apps.folder"],
        }
    except Exception as exc:
        logger.exception("Falha ao listar arquivos do Google Drive")
        raise HTTPException(status_code=502, detail="Não foi possível listar o Google Drive") from exc


@router.post("/api/drive/import")
async def drive_import(
    request: Request,
    file_id: str | None = Form(None),
    folder_id: str | None = Form(None),
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
    def download_and_upload(file_metadata: dict) -> dict[str, str]:
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        metadata = service.files().get(fileId=file_metadata["id"], fields="name,mimeType").execute()
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, service.files().get_media(fileId=file_metadata["id"]))
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
        if bool(file_id) == bool(folder_id):
            raise ValueError("Informe um arquivo ou uma pasta do Google Drive")
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        drive_account_email, drive_credentials_encrypted = await asyncio.to_thread(
            _google_drive_context, credentials
        )
        if folder_id:
            children = []
            folders_to_scan = [folder_id]
            while folders_to_scan:
                current_folder = folders_to_scan.pop()
                entries = _list_drive_children(service, current_folder)
                folders_to_scan.extend(
                    entry["id"] for entry in entries
                    if entry["mimeType"] == "application/vnd.google-apps.folder"
                )
                children.extend(
                    entry for entry in entries
                    if entry["mimeType"] != "application/vnd.google-apps.folder"
                )
            if not children:
                raise ValueError("A pasta selecionada não contém imagens ou vídeos")
            items = [await asyncio.to_thread(download_and_upload, child) for child in children]
            for item, child in zip(items, children):
                item.update({
                    "drive_media_url": f"https://drive.google.com/uc?export=download&id={child['id']}",
                    "drive_account_email": drive_account_email,
                    "drive_credentials_encrypted": drive_credentials_encrypted,
                })
            return {"items": items}
        metadata = service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        item = await asyncio.to_thread(download_and_upload, metadata)
        item.update({
            "drive_media_url": f"https://drive.google.com/uc?export=download&id={metadata['id']}",
            "drive_account_email": drive_account_email,
            "drive_credentials_encrypted": drive_credentials_encrypted,
        })
        return {"items": [item]}
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
    account_id: int | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role == "collaborator":
        return RedirectResponse("/hub", status_code=status.HTTP_303_SEE_OTHER)
    owner_id = workspace_owner_id(user)
    await _remove_stale_pending_accounts(db, owner_id)
    accounts = (await db.scalars(select(InstagramAccount).where(InstagramAccount.owner_id == owner_id))).all()
    batches = (
        await db.scalars(
            select(PostingBatch)
            .where(PostingBatch.owner_id == owner_id)
            .order_by(PostingBatch.created_at.desc())
        )
    ).all()
    batch_account_ids = {
        batch.id: set(json.loads(batch.account_ids or "[]"))
        for batch in batches
    }
    if account_id is not None and not any(account.id == account_id for account in accounts):
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    posts_query = (
        select(ScheduledPost)
        .options(selectinload(ScheduledPost.account))
        .where(ScheduledPost.owner_id == owner_id)
    )
    posts = (
        await db.scalars(
            posts_query.order_by(ScheduledPost.scheduled_for.desc())
        )
    ).all()
    def build_account_groups(batch_posts):
        grouped = {}
        for post in batch_posts:
            grouped.setdefault(post.account_id, []).append(post)
        return [
            {
                "account": account_posts[0].account,
                "posts": account_posts,
            }
            for account_posts in grouped.values()
        ]

    queue_groups = []
    for batch in batches:
        batch_posts = [post for post in posts if post.batch_id == batch.id]
        queue_groups.append({
            "batch": batch,
            "posts": batch_posts,
            "account_groups": build_account_groups(batch_posts),
        })
    queue_groups = [group for group in queue_groups if group["posts"]]
    unbatched_posts = [post for post in posts if post.batch_id is None]
    unbatched_account_groups = build_account_groups(unbatched_posts)
    today = datetime.now(timezone.utc).date()
    today_posts = [post for post in posts if post.created_at and post.created_at.date() == today]
    if period_days not in {1, 7, 30, 90}:
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
        "error_accounts": sum(account.connection_status == "error" for account in accounts),
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
            "batches": batches,
            "queue_groups": queue_groups,
            "unbatched_posts": unbatched_posts,
            "unbatched_account_groups": unbatched_account_groups,
            "batch_account_ids": batch_account_ids,
            "selected_account_id": account_id,
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


def _automation_payload(rule: AutomationRule) -> dict:
    try:
        target_account_ids = json.loads(rule.target_account_ids or "[]")
    except (TypeError, json.JSONDecodeError):
        target_account_ids = [rule.account_id] if rule.account_id else []
    return {
        "id": rule.id,
        "account_id": rule.account_id,
        "target_account_ids": target_account_ids,
        "rule_type": rule.rule_type,
        "trigger_keywords": rule.trigger_keywords,
        "message_text": rule.message_text,
        "dm_followup_text": rule.dm_followup_text,
        "media_url": rule.media_url,
        "drive_media_url": rule.drive_media_url,
        "drive_account_email": rule.drive_account_email,
        "is_active": rule.is_active,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


@router.get("/api/automations")
async def list_automations(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    rules = (
        await db.scalars(
            select(AutomationRule)
            .options(selectinload(AutomationRule.account))
            .where(AutomationRule.owner_id == owner_id)
            .order_by(AutomationRule.created_at.desc(), AutomationRule.id.desc())
        )
    ).all()
    return {
        "items": [
            {**_automation_payload(rule), "account": rule.account.username if rule.account else "Todas as contas"}
            for rule in rules
        ]
    }


@router.post("/api/automations")
async def create_automation(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    payload = await request.json()
    rule_type = str(payload.get("rule_type", "")).strip()
    message_text = str(payload.get("message_text", "")).strip()[:2000]
    raw_target_ids = payload.get("target_account_ids", payload.get("account_ids", []))
    if not isinstance(raw_target_ids, list):
        raw_target_ids = [raw_target_ids]
    target_ids = []
    for value in raw_target_ids:
        if value not in (None, "", 0, "0"):
            try:
                target_ids.append(int(value))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Alvo de automação inválido") from exc
    target_ids = list(dict.fromkeys(target_ids))
    account_id = payload.get("account_id")
    if account_id not in (None, "", 0, "0") and not target_ids:
        target_ids = [int(account_id)]
    trigger_keywords = str(payload.get("trigger_keywords", "")).strip()[:500]
    if rule_type not in {"comment_reply", "dm_reply"}:
        raise HTTPException(status_code=400, detail="Tipo de automação inválido")
    dm_followup_text = str(payload.get("dm_followup_text", "")).strip()[:2000]
    if rule_type == "comment_reply" and not message_text and not payload.get("media_url") and not payload.get("drive_media_url"):
        raise HTTPException(status_code=400, detail="Informe uma mensagem ou uma mídia")
    if rule_type == "comment_reply" and not trigger_keywords:
        raise HTTPException(status_code=400, detail="Informe ao menos uma palavra-chave")
    owner_id = workspace_owner_id(user)
    accounts = []
    if target_ids:
        accounts = (await db.scalars(select(InstagramAccount).where(
            InstagramAccount.owner_id == owner_id, InstagramAccount.id.in_(target_ids)
        ))).all()
        if len(accounts) != len(target_ids):
            raise HTTPException(status_code=404, detail="Uma ou mais contas não foram encontradas")
    account_id = target_ids[0] if len(target_ids) == 1 else None
    rule = AutomationRule(
        owner_id=owner_id,
        account_id=account_id,
        target_account_ids=json.dumps(target_ids),
        rule_type=rule_type,
        trigger_keywords=trigger_keywords,
        message_text=message_text,
        dm_followup_text=dm_followup_text,
        media_url=str(payload.get("media_url") or "").strip() or None,
        drive_media_url=str(payload.get("drive_media_url") or "").strip() or None,
        drive_account_email=str(payload.get("drive_account_email") or "").strip() or None,
        drive_credentials_encrypted=str(payload.get("drive_credentials_encrypted") or "").strip() or None,
        is_active=bool(payload.get("is_active", True)),
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _automation_payload(rule)


@router.put("/api/automations/{rule_id}")
@router.patch("/api/automations/{rule_id}")
async def update_automation(
    rule_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    rule = await db.scalar(select(AutomationRule).where(
        AutomationRule.id == rule_id, AutomationRule.owner_id == owner_id
    ))
    if not rule:
        raise HTTPException(status_code=404, detail="Automação não encontrada")
    payload = await request.json()
    if "trigger_keywords" in payload:
        rule.trigger_keywords = str(payload["trigger_keywords"]).strip()[:500]
        if rule.rule_type == "comment_reply" and not rule.trigger_keywords:
            raise HTTPException(status_code=400, detail="Informe ao menos uma palavra-chave")
    if "rule_type" in payload and payload["rule_type"] in {"comment_reply", "dm_reply"}:
        rule.rule_type = payload["rule_type"]
        if rule.rule_type == "comment_reply" and not rule.trigger_keywords:
            raise HTTPException(status_code=400, detail="Informe ao menos uma palavra-chave")
    if "target_account_ids" in payload or "account_ids" in payload or "account_id" in payload:
        raw_ids = payload.get("target_account_ids", payload.get("account_ids", [payload.get("account_id")]))
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids]
        target_ids = list(dict.fromkeys(int(value) for value in raw_ids if value not in (None, "", 0, "0")))
        if target_ids:
            count = await db.scalar(select(func.count(InstagramAccount.id)).where(
                InstagramAccount.owner_id == owner_id, InstagramAccount.id.in_(target_ids)
            ))
            if count != len(target_ids):
                raise HTTPException(status_code=404, detail="Uma ou mais contas não foram encontradas")
        rule.target_account_ids = json.dumps(target_ids)
        rule.account_id = target_ids[0] if len(target_ids) == 1 else None
    if "message_text" in payload:
        rule.message_text = str(payload["message_text"]).strip()[:2000]
    if "dm_followup_text" in payload:
        rule.dm_followup_text = str(payload["dm_followup_text"]).strip()[:2000]
    if "is_active" in payload:
        rule.is_active = bool(payload["is_active"])
    for field in ("media_url", "drive_media_url", "drive_account_email", "drive_credentials_encrypted"):
        if field in payload:
            setattr(rule, field, str(payload[field] or "").strip() or None)
    await db.commit()
    return _automation_payload(rule)


@router.delete("/api/automations/{rule_id}")
async def delete_automation(
    rule_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    rule = await db.scalar(select(AutomationRule).where(
        AutomationRule.id == rule_id, AutomationRule.owner_id == owner_id
    ))
    if not rule:
        raise HTTPException(status_code=404, detail="Automação não encontrada")
    await db.delete(rule)
    await db.commit()
    return {"deleted": rule_id}


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
    await _remove_stale_pending_accounts(db, owner_id)
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
async def api_status(
    period_days: int = 7,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if period_days not in {1, 7, 30, 90}:
        period_days = 7
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
            select(InstagramMetric).where(
                InstagramMetric.account_id.in_(account_ids),
                InstagramMetric.metric_date >= datetime.now(timezone.utc) - timedelta(days=period_days - 1),
            )
        )
    ).all() if account_ids else []
    account_views = _metric_views_by_account(metric_rows, account_ids)
    total_views = sum(account_views.values())
    bot_counts = {
        event_type: sum(event.event_type == event_type for event in bot_events)
        for event_type in ("link_click", "lead_initiated", "pix_generated", "pix_paid", "pix_pending")
    }
    paid_events = [
        event for event in bot_events
        if event.event_type == "pix_paid"
    ]
    latest_paid = max(
        paid_events,
        key=lambda event: (event.timestamp or datetime.min.replace(tzinfo=timezone.utc), event.id),
        default=None,
    )
    response = JSONResponse({
        "metrics": {
            "pending": sum(post.status in {"scheduled", "processing", "aguardando", "pending"} for post in posts),
            "published": sum(post.status == "published" for post in posts),
            "failed": sum(post.status == "failed" for post in posts),
            "error_accounts": sum(account.connection_status == "error" for account in (
                await db.scalars(select(InstagramAccount).where(
                    InstagramAccount.owner_id == workspace_owner_id(user)
                ))
            ).all()),
            "total_views": total_views,
        },
        "sharkbot": bot_counts,
        "latest_sale": {
            "value": latest_paid.value,
            "customer_name": latest_paid.customer_name,
            "timestamp": latest_paid.timestamp.isoformat() if latest_paid and latest_paid.timestamp else None,
        } if latest_paid else None,
        "account_views": account_views,
        "account_statuses": {
            account.id: {
                "status": account.connection_status,
                "reason": account.status_reason,
                "checked_at": account.status_checked_at.isoformat() if account.status_checked_at else None,
            }
            for account in (
                await db.scalars(
                    select(InstagramAccount).where(
                        InstagramAccount.owner_id == workspace_owner_id(user)
                    )
                )
            ).all()
        },
        "posts": [
            {
                "id": post.id,
                "media_url": post.media_url,
                "media_type": post.media_type,
                "status": post.status,
                "error_message": post.error_message,
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


@router.post("/api/meta/test")
async def test_meta_connection(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run a safe Meta diagnostic and expose the exact non-secret API responses."""
    settings = get_settings()
    accounts = (
        await db.scalars(
            select(InstagramAccount)
            .where(
                InstagramAccount.owner_id == workspace_owner_id(user),
                InstagramAccount.connection_status != "pending",
                InstagramAccount.access_token_encrypted != "",
            )
            .order_by(InstagramAccount.id)
        )
    ).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Nenhuma conta Instagram conectada para testar.")

    results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for account in accounts:
            try:
                token = decrypt_token(account.access_token_encrypted)
                base = f"https://graph.instagram.com/{settings.graph_api_version}"
                profile_params = {
                    "fields": "id,username,account_type,media_count",
                }
                profile = await client.get(
                    f"{base}/me",
                    params={**profile_params, "access_token": token},
                )
                profile_payload = profile.json() if profile.headers.get("content-type", "").startswith("application/json") else profile.text[:2000]
                logger.info(
                    "META_TEST profile account=%s method=GET path=/%s/me status=%s params=%s response=%s",
                    account.instagram_user_id,
                    "me",
                    profile.status_code,
                    profile_params,
                    profile_payload,
                )
                insight_results = []
                for metric_name in ("views", "content_views", "reach"):
                    insight_params = {
                        "metric": metric_name,
                        "period": "day",
                    }
                    insights = await client.get(
                        f"{base}/{account.instagram_user_id}/insights",
                        params={**insight_params, "access_token": token},
                    )
                    insight_payload = (
                        insights.json()
                        if insights.headers.get("content-type", "").startswith("application/json")
                        else insights.text[:2000]
                    )
                    insight_keys = (
                        sorted(insight_payload.keys())
                        if isinstance(insight_payload, dict)
                        else []
                    )
                    logger.info(
                        "META_TEST insights account=%s method=GET path=/%s/insights status=%s params=%s response_keys=%s response=%s",
                        account.instagram_user_id,
                        account.instagram_user_id,
                        insights.status_code,
                        insight_params,
                        insight_keys,
                        insight_payload,
                    )
                    insight_results.append({
                        "metric_requested": metric_name,
                        "status": insights.status_code,
                        "response_keys": insight_keys,
                        "response": insight_payload,
                    })
                results.append({
                    "account": account.username,
                    "profile": {
                        "status": profile.status_code,
                        "requested_fields": profile_params["fields"].split(","),
                        "response_keys": (
                            sorted(profile_payload.keys())
                            if isinstance(profile_payload, dict)
                            else []
                        ),
                        "response": profile_payload,
                    },
                    "insights": insight_results,
                })
            except Exception as exc:
                logger.exception("META_TEST failed account=%s", account.instagram_user_id)
                results.append({"account": account.username, "error": str(exc)})
    return {"results": results, "message": "Diagnóstico concluído. Consulte Logs & Sistema para o retorno completo."}


async def _feed_media_for_account(client: httpx.AsyncClient, account: InstagramAccount, token: str, settings) -> list[dict]:
    base = f"https://graph.instagram.com/{settings.graph_api_version}"
    url = f"{base}/{account.instagram_user_id}/media"
    params = {
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
        "limit": 100,
        "access_token": token,
    }
    items = []
    while url:
        response = await client.get(url, params=params)
        if response.is_error:
            raise RuntimeError(_meta_api_error(response))
        payload = response.json()
        for item in payload.get("data", []):
            items.append({"account_id": account.id, "account": account.username, **item})
        url = payload.get("paging", {}).get("next")
        params = {}
    return items


@router.get("/api/feed")
async def get_feed(
    account_ids: list[int] = Query(default=[]),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    query = select(InstagramAccount).where(
        InstagramAccount.owner_id == owner_id,
        InstagramAccount.connection_status != "pending",
        InstagramAccount.access_token_encrypted != "",
    )
    if account_ids:
        query = query.where(InstagramAccount.id.in_(set(account_ids)))
    accounts = (await db.scalars(query.order_by(InstagramAccount.username))).all()
    if account_ids and len(accounts) != len(set(account_ids)):
        raise HTTPException(status_code=404, detail="Uma ou mais contas não foram encontradas")
    results = []
    errors = []
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        for account in accounts:
            try:
                results.extend(await _feed_media_for_account(
                    client, account, decrypt_token(account.access_token_encrypted), settings
                ))
            except Exception as exc:
                logger.exception("Falha ao consultar Feed da conta %s", account.instagram_user_id)
                errors.append({"account_id": account.id, "account": account.username, "error": str(exc)})
    return {"items": results, "errors": errors}


@router.get("/api/calendar/posts")
async def calendar_posts(
    start: str | None = None,
    end: str | None = None,
    account_id: int | None = None,
    batch_id: int | None = None,
    post_status: str | None = Query(None, alias="status"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    query = (
        select(ScheduledPost)
        .options(selectinload(ScheduledPost.account), selectinload(ScheduledPost.batch))
        .where(ScheduledPost.owner_id == owner_id)
    )
    if account_id:
        query = query.where(ScheduledPost.account_id == account_id)
    if batch_id:
        query = query.where(ScheduledPost.batch_id == batch_id)
    if post_status:
        query = query.where(ScheduledPost.status == post_status)
    try:
        if start:
            query = query.where(ScheduledPost.scheduled_for >= parse_scheduled_datetime(start))
        if end:
            query = query.where(ScheduledPost.scheduled_for < parse_scheduled_datetime(end))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Intervalo de calendário inválido") from exc
    posts = (await db.scalars(query.order_by(ScheduledPost.scheduled_for))).all()
    conflicts = {}
    for post in posts:
        key = f"{post.account_id}:{post.scheduled_for.astimezone(timezone.utc).isoformat()}"
        conflicts[key] = conflicts.get(key, 0) + 1
    return {
        "items": [
            {
                "id": post.id,
                "account_id": post.account_id,
                "account": post.account.username,
                "batch_id": post.batch_id,
                "batch": post.batch.name if post.batch else None,
                "media_url": post.thumbnail_url or post.media_url,
                "media_type": post.media_type,
                "caption": post.caption,
                "scheduled_for": post.scheduled_for.isoformat(),
                "status": post.status,
                "error_message": post.error_message,
                "conflict": conflicts[
                    f"{post.account_id}:{post.scheduled_for.astimezone(timezone.utc).isoformat()}"
                ] > 1,
            }
            for post in posts
        ]
    }


@router.patch("/api/calendar/posts/{post_id}")
async def reschedule_calendar_post(
    post_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    payload = await request.json()
    raw_scheduled_for = str(payload.get("scheduled_for") or "")
    try:
        scheduled_for = parse_scheduled_datetime(raw_scheduled_for)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Data de reagendamento inválida") from exc
    if scheduled_for <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="O novo horário deve estar no futuro")
    owner_id = workspace_owner_id(user)
    post = await db.scalar(select(ScheduledPost).where(
        ScheduledPost.id == post_id, ScheduledPost.owner_id == owner_id
    ))
    if not post:
        raise HTTPException(status_code=404, detail="Publicação não encontrada")
    if post.status not in PENDING_STATUSES:
        raise HTTPException(status_code=400, detail="Somente publicações pendentes podem ser reagendadas")
    post.scheduled_for = scheduled_for
    await db.commit()
    unschedule_post(post.id)
    schedule_post(post.id, scheduled_for)
    return {"id": post.id, "scheduled_for": scheduled_for.isoformat()}


@router.get("/api/analytics")
async def analytics(
    account_ids: list[int] = Query(default=[]),
    period_days: int = 30,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if period_days not in {7, 30, 90}:
        raise HTTPException(status_code=400, detail="Período analítico inválido")
    owner_id = workspace_owner_id(user)
    query = select(InstagramAccount).where(
        InstagramAccount.owner_id == owner_id,
        InstagramAccount.access_token_encrypted != "",
    )
    if account_ids:
        query = query.where(InstagramAccount.id.in_(set(account_ids)))
    accounts = (await db.scalars(query.order_by(InstagramAccount.username))).all()
    settings = get_settings()
    start_date = (datetime.now(timezone.utc) - timedelta(days=period_days - 1)).date()
    account_rows = []
    media_rows = []
    errors = []
    async with httpx.AsyncClient(timeout=30) as client:
        for account in accounts:
            try:
                token = decrypt_token(account.access_token_encrypted)
                profile = await client.get(
                    f"https://graph.instagram.com/{settings.graph_api_version}/{account.instagram_user_id}",
                    params={"fields": "id,username,followers_count,media_count", "access_token": token},
                )
                profile_data = profile.json() if not profile.is_error else {}
                insights = await client.get(
                    f"https://graph.instagram.com/{settings.graph_api_version}/{account.instagram_user_id}/insights",
                    params={
                        "metric": "reach,impressions,profile_views,website_clicks",
                        "period": "day",
                        "since": start_date.isoformat(),
                        "until": datetime.now(timezone.utc).date().isoformat(),
                        "access_token": token,
                    },
                )
                insight_values = {}
                if not insights.is_error:
                    for item in insights.json().get("data", []):
                        values = item.get("values", [])
                        insight_values[item.get("name")] = sum(int(value.get("value", 0) or 0) for value in values)
                account_rows.append({
                    "account_id": account.id,
                    "account": account.username,
                    "followers": profile_data.get("followers_count", 0),
                    "media_count": profile_data.get("media_count", 0),
                    "growth": 0,
                    "reach": insight_values.get("reach", 0),
                    "impressions": insight_values.get("impressions", 0),
                    "profile_views": insight_values.get("profile_views", 0),
                    "link_clicks": insight_values.get("website_clicks", 0),
                })
                media = await _feed_media_for_account(client, account, token, settings)
                for item in media[:25]:
                    media_insights = {}
                    media_id = item.get("id")
                    if media_id:
                        insight_response = await client.get(
                            f"https://graph.instagram.com/{settings.graph_api_version}/{media_id}/insights",
                            params={
                                "metric": "likes,comments,shares,saved,views,total_interactions",
                                "access_token": token,
                            },
                        )
                        if not insight_response.is_error:
                            media_insights = {
                                entry.get("name"): entry.get("values", [{}])[0].get("value")
                                for entry in insight_response.json().get("data", [])
                            }
                    media_rows.append({
                        "account_id": account.id,
                        "account": account.username,
                        "id": media_id,
                        "media_url": item.get("thumbnail_url") or item.get("media_url"),
                        "caption": item.get("caption", ""),
                        "media_type": item.get("media_type"),
                        "likes": media_insights.get("likes"),
                        "comments": media_insights.get("comments"),
                        "shares": media_insights.get("shares"),
                        "saves": media_insights.get("saved"),
                        "views": media_insights.get("views"),
                        "engagement": media_insights.get("total_interactions"),
                    })
            except Exception as exc:
                logger.exception("Falha ao consultar analytics da conta %s", account.instagram_user_id)
                errors.append({"account_id": account.id, "account": account.username, "error": str(exc)})
    return {"accounts": account_rows, "media": media_rows, "errors": errors, "period_days": period_days}


@router.post("/api/feed/delete")
async def delete_feed_items(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    payload = await request.json()
    account_ids = {int(value) for value in payload.get("account_ids", [])}
    selected = payload.get("media", [])
    clear_feed = bool(payload.get("clear_feed"))
    if not account_ids:
        raise HTTPException(status_code=400, detail="Selecione ao menos uma conta")
    owner_id = workspace_owner_id(user)
    accounts = (await db.scalars(select(InstagramAccount).where(
        InstagramAccount.owner_id == owner_id,
        InstagramAccount.id.in_(account_ids),
        InstagramAccount.access_token_encrypted != "",
    ))).all()
    if len(accounts) != len(account_ids):
        raise HTTPException(status_code=404, detail="Uma ou mais contas não foram encontradas")
    by_id = {account.id: account for account in accounts}
    targets: list[tuple[InstagramAccount, str]] = []
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        if clear_feed:
            for account in accounts:
                media = await _feed_media_for_account(
                    client, account, decrypt_token(account.access_token_encrypted), settings
                )
                targets.extend((account, item["id"]) for item in media if item.get("id"))
        else:
            for item in selected:
                account = by_id.get(int(item.get("account_id", 0)))
                media_id = str(item.get("media_id", "")).strip()
                if account and media_id:
                    targets.append((account, media_id))
        if not targets:
            raise HTTPException(status_code=400, detail="Nenhuma publicação selecionada")
        deleted = []
        errors = []
        for index, (account, media_id) in enumerate(targets):
            if index:
                await asyncio.sleep(2)
            response = await client.delete(
                f"https://graph.instagram.com/{settings.graph_api_version}/{media_id}",
                params={"access_token": decrypt_token(account.access_token_encrypted)},
            )
            if response.is_error:
                errors.append({"account": account.username, "media_id": media_id, "error": _meta_api_error(response)})
            else:
                deleted.append({"account": account.username, "media_id": media_id})
    return {"deleted": deleted, "errors": errors, "throttled_seconds": 2}


@router.get("/auth/instagram/start")
async def instagram_start(
    request: Request,
    reconnect_account_id: int | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    state = new_state()
    request.session["instagram_oauth_state"] = state
    if reconnect_account_id is not None:
        account = await db.scalar(
            select(InstagramAccount).where(
                InstagramAccount.id == reconnect_account_id,
                InstagramAccount.owner_id == workspace_owner_id(user),
            )
        )
        if account:
            request.session["instagram_reconnect_account_id"] = account.id
    redirect_url = authorization_url(state)
    logger.warning("Instagram OAuth authorization URL: %s", redirect_url)
    return RedirectResponse(redirect_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/auth/callback")
async def instagram_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_reason: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    session_user_id = request.session.get("user_id")
    expected_state = request.session.pop("instagram_oauth_state", None)
    if not session_user_id or not state or state != expected_state:
        logger.warning(
            "Instagram OAuth callback rejected before token exchange: "
            "session_user=%s callback_state=%s expected_state=%s",
            bool(session_user_id),
            bool(state),
            bool(expected_state),
        )
        raise HTTPException(status_code=400, detail="OAuth state inválido")
    if error:
        request.session.pop("instagram_reconnect_account_id", None)
        detail = error_description or error_reason or error
        raise HTTPException(status_code=400, detail=f"Autorização do Instagram não concluída: {detail}")
    if not code:
        raise HTTPException(status_code=400, detail="Código OAuth ausente")
    try:
        token_data = await exchange_code(code)
        short_token = token_data["access_token"]
        try:
            access_token = await exchange_long_lived_token(short_token)
        except httpx.HTTPStatusError as exc:
            access_token = short_token
            logger.warning(
                "Meta recusou a troca para token longo (HTTP %s); usando token curto para concluir OAuth.",
                exc.response.status_code,
            )
        profile = await fetch_profile(access_token)
        business_account = await fetch_instagram_business_account(
            access_token,
            str(profile.get("user_id") or profile.get("id")),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha no OAuth do Instagram: {exc}") from exc
    user = await db.get(User, int(request.session["user_id"]))
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    owner_id = workspace_owner_id(user)
    reconnect_account_id = request.session.pop("instagram_reconnect_account_id", None)
    profile_id = str(profile.get("user_id") or profile["id"])
    account_ids = {profile_id}
    if business_account:
        account_ids.add(business_account["instagram_user_id"])
    account = None
    if reconnect_account_id is not None:
        reconnect_account = await db.scalar(
            select(InstagramAccount).where(
                InstagramAccount.id == int(reconnect_account_id),
                InstagramAccount.owner_id == owner_id,
            )
        )
        if reconnect_account and (
            reconnect_account.connection_status in {"error", "disconnected", "pending"}
            or reconnect_account.username.strip().lower() == str(profile.get("username", "")).strip().lower()
        ):
            account = reconnect_account
    if account is None:
        account = await db.scalar(
            select(InstagramAccount).where(
                InstagramAccount.owner_id == owner_id,
                InstagramAccount.instagram_user_id.in_(account_ids),
            )
        )
    if account is None and profile.get("username"):
        account = await db.scalar(
            select(InstagramAccount).where(
                InstagramAccount.owner_id == owner_id,
                InstagramAccount.connection_status == "pending",
                func.lower(func.trim(InstagramAccount.username)) == profile["username"].strip().lower(),
            )
        )
    if account:
        if business_account:
            account.instagram_user_id = business_account["instagram_user_id"]
            account.facebook_page_id = business_account.get("page_id")
        account.username = profile.get("username", account.username)
        account.profile_picture_url = profile.get("profile_picture_url", account.profile_picture_url)
        account.access_token_encrypted = encrypt_token(access_token)
    else:
        account = InstagramAccount(
            owner_id=owner_id,
            instagram_user_id=(
                business_account["instagram_user_id"]
                if business_account
                else str(profile.get("user_id") or profile["id"])
            ),
            facebook_page_id=business_account.get("page_id") if business_account else None,
            username=profile.get("username", ""),
            profile_picture_url=profile.get("profile_picture_url"),
            access_token_encrypted=encrypt_token(access_token),
        )
        db.add(account)
    await db.flush()
    async with httpx.AsyncClient(timeout=30) as client:
        await _refresh_account_status(client, account, access_token, get_settings())
    duplicate_pending = (
        await db.scalars(
            select(InstagramAccount).where(
                InstagramAccount.owner_id == owner_id,
                InstagramAccount.id != account.id,
                InstagramAccount.connection_status == "pending",
                func.lower(func.trim(InstagramAccount.username)) == account.username.strip().lower(),
            )
        )
    ).all()
    for pending_account in duplicate_pending:
        await db.execute(
            update(ScheduledPost)
            .where(ScheduledPost.account_id == pending_account.id)
            .values(account_id=account.id)
        )
        await db.execute(
            update(InstagramMetric)
            .where(InstagramMetric.account_id == pending_account.id)
            .values(account_id=account.id)
        )
        await db.execute(
            update(BotEvent)
            .where(BotEvent.account_id == pending_account.id)
            .values(account_id=account.id)
        )
        await db.delete(pending_account)
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


@router.post("/accounts/{account_id}/verify")
async def verify_account(
    account_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    account = await db.scalar(select(InstagramAccount).where(
        InstagramAccount.id == account_id,
        InstagramAccount.owner_id == owner_id,
    ))
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    if not account.access_token_encrypted:
        account.connection_status = "pending"
        account.status_reason = "A conta ainda não possui um token autorizado."
        account.status_checked_at = datetime.now(timezone.utc)
        await db.commit()
        if "application/json" in request.headers.get("accept", ""):
            return {
                "account_id": account.id,
                "status": account.connection_status,
                "reason": account.status_reason,
                "checked_at": account.status_checked_at.isoformat() if account.status_checked_at else None,
                "reconnect_url": f"/auth/instagram/start?reconnect_account_id={account.id}",
            }
        return RedirectResponse(
            f"/auth/instagram/start?reconnect_account_id={account.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        token = decrypt_token(account.access_token_encrypted)
        async with httpx.AsyncClient(timeout=30) as client:
            await _refresh_account_status(client, account, token, get_settings())
    except Exception as exc:
        account.connection_status = "error"
        account.status_reason = f"Falha ao verificar a autorização na Meta: {str(exc)[:400]}"
        account.status_checked_at = datetime.now(timezone.utc)
        logger.exception("Falha na verificação manual da conta %s", account.instagram_user_id)
    await db.commit()
    if "application/json" in request.headers.get("accept", ""):
        response = {
            "account_id": account.id,
            "status": account.connection_status,
            "reason": account.status_reason,
            "checked_at": account.status_checked_at.isoformat() if account.status_checked_at else None,
        }
        if account.connection_status != "connected":
            response["reconnect_url"] = f"/auth/instagram/start?reconnect_account_id={account.id}"
        return response
    if account.connection_status != "connected":
        return RedirectResponse(
            f"/auth/instagram/start?reconnect_account_id={account.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse("/dashboard#accounts", status_code=status.HTTP_303_SEE_OTHER)


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
    unschedule_post(post.id)
    await db.delete(post)
    await db.commit()
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/{post_id}/retry")
async def retry_post(
    post_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    post = await db.scalar(select(ScheduledPost).where(
        ScheduledPost.id == post_id,
        ScheduledPost.owner_id == owner_id,
    ))
    if not post:
        raise HTTPException(status_code=404, detail="Publicação não encontrada")
    if post.status not in {"failed", "blocked"}:
        raise HTTPException(status_code=400, detail="Somente publicações com falha podem ser tentadas novamente")
    account = await db.get(InstagramAccount, post.account_id)
    if not account or account.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    if not account.access_token_encrypted:
        raise HTTPException(status_code=400, detail="A conta ainda não foi autorizada")
    try:
        token = decrypt_token(account.access_token_encrypted)
        async with httpx.AsyncClient(timeout=30) as client:
            authorized = await _refresh_account_status(client, account, token, get_settings())
    except Exception as exc:
        authorized = False
        account.connection_status = "error"
        account.status_reason = f"Falha ao verificar a autorização na Meta: {str(exc)[:400]}"
    if not authorized:
        post.status = "blocked"
        post.error_message = account.status_reason or "A conta ainda não está autorizada para publicar."
        await db.commit()
        raise HTTPException(status_code=400, detail=post.error_message)
    media_url = post.original_media_url or post.media_url
    media_available, media_error = (True, None) if post.drive_media_url else await _media_is_public(media_url)
    if not media_available:
        post.status = "failed"
        post.error_message = media_error
        await db.commit()
        raise HTTPException(status_code=400, detail=media_error)
    post.status = "scheduled"
    post.error_message = None
    post.scheduled_for = datetime.now(timezone.utc) + timedelta(seconds=2)
    await db.commit()
    schedule_post(post.id, post.scheduled_for)
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/retry-blocked")
async def retry_blocked_posts(
    account_id: int | None = Form(None),
    intervalo: int = Form(1),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    if intervalo < 1:
        raise HTTPException(status_code=400, detail="O intervalo mínimo é de 1 minuto")
    accounts_query = select(InstagramAccount).where(InstagramAccount.owner_id == owner_id)
    if account_id is not None:
        accounts_query = accounts_query.where(InstagramAccount.id == account_id)
    accounts = (await db.scalars(accounts_query)).all()
    if account_id is not None and not accounts:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    authorized_ids = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for account in accounts:
            if not account.access_token_encrypted:
                continue
            try:
                token = decrypt_token(account.access_token_encrypted)
                if await _refresh_account_status(client, account, token, get_settings()):
                    authorized_ids.add(account.id)
            except Exception as exc:
                account.connection_status = "error"
                account.status_reason = f"Falha ao verificar a autorização na Meta: {str(exc)[:400]}"
    posts_query = select(ScheduledPost).where(
        ScheduledPost.owner_id == owner_id,
        ScheduledPost.status.in_({"failed", "blocked"}),
        ScheduledPost.account_id.in_(authorized_ids) if authorized_ids else ScheduledPost.id == -1,
    )
    posts = (await db.scalars(posts_query)).all()
    now = datetime.now(timezone.utc) + timedelta(minutes=2)
    available_posts = []
    for post in posts:
        media_available, media_error = (
            (True, None)
            if post.drive_media_url
            else await _media_is_public(post.original_media_url or post.media_url)
        )
        if media_available:
            available_posts.append(post)
        else:
            post.error_message = media_error
    for index, post in enumerate(available_posts):
        post.status = "scheduled"
        post.error_message = None
        post.scheduled_for = now + timedelta(minutes=index * intervalo)
    await db.commit()
    for post in available_posts:
        schedule_post(post.id, post.scheduled_for)
    destination = f"/dashboard?account_id={account_id}#queue" if account_id else "/dashboard#queue"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/retry-failed")
async def retry_failed_posts(
    account_id: int | None = Form(None),
    intervalo: int = Form(1),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    if intervalo < 1:
        raise HTTPException(status_code=400, detail="O intervalo mínimo é de 1 minuto")
    confirmed_at = datetime.now(timezone.utc)
    accounts_query = select(InstagramAccount).where(InstagramAccount.owner_id == owner_id)
    if account_id is not None:
        accounts_query = accounts_query.where(InstagramAccount.id == account_id)
    accounts = (await db.scalars(accounts_query)).all()
    if account_id is not None and not accounts:
        raise HTTPException(status_code=404, detail="Conta não encontrada")

    authorized_ids = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for account in accounts:
            if not account.access_token_encrypted:
                continue
            try:
                token = decrypt_token(account.access_token_encrypted)
                if await _refresh_account_status(client, account, token, get_settings()):
                    authorized_ids.add(account.id)
            except Exception as exc:
                logger.exception("Falha ao verificar autorização para retry da conta %s", account.id)
                account.connection_status = "error"
                account.status_reason = f"Falha ao verificar a autorização na Meta: {str(exc)[:400]}"

    posts_query = select(ScheduledPost).where(
        ScheduledPost.owner_id == owner_id,
        ScheduledPost.status == "failed",
        ScheduledPost.account_id.in_(authorized_ids) if authorized_ids else ScheduledPost.id == -1,
    ).order_by(ScheduledPost.scheduled_for, ScheduledPost.id)
    posts = (await db.scalars(posts_query)).all()
    first_time = confirmed_at + timedelta(minutes=2)
    for index, post in enumerate(posts):
        post.status = "scheduled"
        post.error_message = None
        post.scheduled_for = first_time + timedelta(minutes=index * intervalo)
    await db.commit()
    for post in posts:
        schedule_post(post.id, post.scheduled_for)
    destination = f"/dashboard?account_id={account_id}#queue" if account_id else "/dashboard#queue"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/delete-selected")
async def delete_selected_posts(
    post_ids: list[int] = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if not post_ids:
        raise HTTPException(status_code=400, detail="Nenhuma publicação selecionada")
    owner_id = workspace_owner_id(user)
    selected_posts = (await db.scalars(select(ScheduledPost).where(
        ScheduledPost.id.in_(set(post_ids)),
        ScheduledPost.owner_id == owner_id,
    ))).all()
    for post in selected_posts:
        unschedule_post(post.id)
    await db.execute(
        delete(ScheduledPost).where(
            ScheduledPost.id.in_(set(post_ids)),
            ScheduledPost.owner_id == owner_id,
        )
    )
    await db.commit()
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/delete-all")
async def delete_all_posts(
    account_id: int | None = Form(None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    conditions = [ScheduledPost.owner_id == owner_id]
    if account_id is not None:
        account = await db.scalar(select(InstagramAccount).where(
            InstagramAccount.id == account_id, InstagramAccount.owner_id == owner_id
        ))
        if not account:
            raise HTTPException(status_code=404, detail="Conta não encontrada")
        conditions.append(ScheduledPost.account_id == account_id)
    posts = (await db.scalars(select(ScheduledPost).where(*conditions))).all()
    for post in posts:
        unschedule_post(post.id)
    await db.execute(delete(ScheduledPost).where(*conditions))
    await db.commit()
    destination = f"/dashboard?account_id={account_id}#queue" if account_id else "/dashboard#queue"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/clear-failed")
async def clear_failed_posts(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    failed_posts = (await db.scalars(select(ScheduledPost).where(
        ScheduledPost.owner_id == owner_id,
        ScheduledPost.status.in_({"failed", "blocked"}),
    ))).all()
    for post in failed_posts:
        unschedule_post(post.id)
    await db.execute(
        delete(ScheduledPost).where(
            ScheduledPost.owner_id == owner_id,
            ScheduledPost.status.in_({"failed", "blocked"}),
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
    drive_media_urls: list[str] = Form(default=[]),
    drive_account_emails: list[str] = Form(default=[]),
    drive_credentials_encrypted: list[str] = Form(default=[]),
    captions: list[str] = Form(default=[]),
    caption_mode: str = Form("global"),
    caption: str = Form(""),
    scheduled_for: str = Form(...),
    interval_minutes: int = Form(1),
    batch_name: str = Form(""),
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

    batch = PostingBatch(
        owner_id=owner_id,
        name=batch_name.strip()[:160] or f"Lote de {first_time.astimezone(LOCAL_TIMEZONE).strftime('%d/%m %H:%M')}",
        account_ids=json.dumps(list(dict.fromkeys(account_ids))),
    )
    db.add(batch)
    await db.flush()
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
                batch_id=batch.id,
                media_url=media_url,
                original_media_url=media_url,
                drive_media_url=drive_media_urls[media_index] if media_index < len(drive_media_urls) else None,
                drive_account_email=drive_account_emails[media_index] if media_index < len(drive_account_emails) else None,
                drive_credentials_encrypted=(
                    drive_credentials_encrypted[media_index]
                    if media_index < len(drive_credentials_encrypted) else None
                ),
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


@router.post("/batches/{batch_id}/pause")
async def pause_batch(
    batch_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    batch = await db.scalar(select(PostingBatch).where(
        PostingBatch.id == batch_id, PostingBatch.owner_id == owner_id
    ))
    if not batch:
        raise HTTPException(status_code=404, detail="Lote não encontrado")
    batch.status = "paused"
    posts = (await db.scalars(select(ScheduledPost).where(
        ScheduledPost.batch_id == batch.id,
        ScheduledPost.status.in_(PENDING_STATUSES),
    ))).all()
    await db.commit()
    for post in posts:
        unschedule_post(post.id)
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/batches/{batch_id}/resume")
async def resume_batch(
    batch_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    batch = await db.scalar(select(PostingBatch).where(
        PostingBatch.id == batch_id, PostingBatch.owner_id == owner_id
    ))
    if not batch:
        raise HTTPException(status_code=404, detail="Lote não encontrado")
    batch.status = "active"
    posts = (await db.scalars(select(ScheduledPost).where(
        ScheduledPost.batch_id == batch.id,
        ScheduledPost.status.in_(PENDING_STATUSES),
    ))).all()
    await db.commit()
    for post in posts:
        schedule_post(post.id, post.scheduled_for)
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/batches/{batch_id}/delete")
async def delete_batch(
    batch_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    batch = await db.scalar(select(PostingBatch).where(
        PostingBatch.id == batch_id, PostingBatch.owner_id == owner_id
    ))
    if not batch:
        raise HTTPException(status_code=404, detail="Lote não encontrado")
    posts = (await db.scalars(select(ScheduledPost).where(
        ScheduledPost.batch_id == batch.id,
        ScheduledPost.owner_id == owner_id,
    ))).all()
    for post in posts:
        unschedule_post(post.id)
    await db.delete(batch)
    await db.commit()
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/batches/{batch_id}/accounts")
async def update_batch_accounts(
    batch_id: int,
    account_ids: list[int] = Form(default=[]),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    owner_id = workspace_owner_id(user)
    batch = await db.scalar(select(PostingBatch).where(
        PostingBatch.id == batch_id, PostingBatch.owner_id == owner_id
    ))
    if not batch:
        raise HTTPException(status_code=404, detail="Lote não encontrado")
    selected_ids = list(dict.fromkeys(account_ids))
    if not selected_ids:
        raise HTTPException(status_code=400, detail="Selecione pelo menos uma conta")
    valid_ids = set((await db.scalars(select(InstagramAccount.id).where(
        InstagramAccount.id.in_(selected_ids), InstagramAccount.owner_id == owner_id
    ))).all())
    if valid_ids != set(selected_ids):
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    previous_ids = set(json.loads(batch.account_ids or "[]"))
    added_ids = set(selected_ids) - previous_ids
    removed_ids = previous_ids - set(selected_ids)
    pending_posts = (await db.scalars(select(ScheduledPost).where(
        ScheduledPost.batch_id == batch.id,
        ScheduledPost.status.in_(PENDING_STATUSES),
    ))).all()
    if removed_ids:
        for post in pending_posts:
            if post.account_id in removed_ids:
                await db.delete(post)
    if added_ids and pending_posts:
        templates_by_media = {}
        for post in pending_posts:
            templates_by_media.setdefault(
                (post.media_url, post.media_type, post.caption, post.scheduled_for),
                post,
            )
        for account_id in added_ids:
            for template in templates_by_media.values():
                db.add(ScheduledPost(
                    owner_id=owner_id,
                    account_id=account_id,
                    batch_id=batch.id,
                    media_url=template.media_url,
                    original_media_url=template.original_media_url,
                    thumbnail_url=template.thumbnail_url,
                    storage_path=template.storage_path,
                    thumbnail_storage_path=template.thumbnail_storage_path,
                    media_type=template.media_type,
                    caption=template.caption,
                    scheduled_for=template.scheduled_for,
                ))
    batch.account_ids = json.dumps(selected_ids)
    await db.commit()
    new_posts = (await db.scalars(select(ScheduledPost).where(
        ScheduledPost.batch_id == batch.id,
        ScheduledPost.account_id.in_(added_ids),
        ScheduledPost.status.in_(PENDING_STATUSES),
    ))).all() if added_ids else []
    for post in new_posts:
        if batch.status == "active":
            schedule_post(post.id, post.scheduled_for)
    return RedirectResponse("/dashboard#queue", status_code=status.HTTP_303_SEE_OTHER)
