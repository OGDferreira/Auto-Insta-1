from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import logging
import asyncio
import re
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from PIL import Image, ImageOps, UnidentifiedImageError
from supabase import create_client

from .config import get_settings
from .db import get_db
from .jobs import PENDING_STATUSES, schedule_post
from .models import BotEvent, InstagramAccount, InstagramMetric, ScheduledPost, User
from .oauth import (
    authorization_url,
    exchange_code,
    exchange_long_lived_token,
    fetch_profile,
    new_state,
)
from .observability import get_recent_logs
from .security import encrypt_token, hash_password, verify_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
UPLOAD_DIR = Path("uploads")
ALLOWED_UPLOAD_TYPES = {"image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime"}
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{2,80}$")
LOCAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")


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
    } and not request.url.path.startswith("/accounts/"):
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
    if media.content_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(status_code=400, detail="Formato de mídia não suportado")
    extension = Path(media.filename or "").suffix.lower()
    if not extension:
        raise HTTPException(status_code=400, detail="Arquivo sem extensão")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}{extension}"
    size = 0
    try:
        content = await media.read(MAX_UPLOAD_SIZE + 1)
        size = len(content)
        if size > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="Arquivo excede o limite de 50 MB")
        if media.content_type.startswith("image/"):
            try:
                image = ImageOps.exif_transpose(Image.open(BytesIO(content)))
                width, height = image.size
                ratio = width / height
                if ratio < 0.8 or ratio > 1.91:
                    # Instagram rejects images outside 4:5..1.91:1. Crop only
                    # invalid uploads and keep valid originals untouched.
                    target_ratio = 4 / 5
                    if ratio > target_ratio:
                        crop_width = int(height * target_ratio)
                        left = (width - crop_width) // 2
                        image = image.crop((left, 0, left + crop_width, height))
                    else:
                        crop_height = int(width / target_ratio)
                        top = (height - crop_height) // 2
                        image = image.crop((0, top, width, top + crop_height))
                    image.thumbnail((1080, 1350), Image.Resampling.LANCZOS)
                    output = BytesIO()
                    image.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
                    content = output.getvalue()
                    filename = f"{uuid4().hex}.jpg"
            except (UnidentifiedImageError, OSError) as exc:
                raise HTTPException(status_code=400, detail="Imagem inválida ou corrompida") from exc
    except HTTPException:
        raise
    finally:
        await media.close()
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_key:
        raise HTTPException(status_code=503, detail="Supabase Storage não configurado")

    def upload_to_storage() -> str:
        client = create_client(settings.supabase_url, settings.supabase_key)
        path = f"{uuid4().hex}/{filename}"
        client.storage.from_(settings.supabase_storage_bucket).upload(
            path,
            content,
            file_options={"content-type": media.content_type, "upsert": "false"},
        )
        return client.storage.from_(settings.supabase_storage_bucket).get_public_url(path)

    public_url = await asyncio.to_thread(upload_to_storage)
    return {
        "url": public_url,
        "media_type": "REELS" if media.content_type.startswith("video/") else "IMAGE",
    }


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
async def dashboard(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
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
    metric_rows = (
        await db.scalars(
            select(InstagramMetric).where(InstagramMetric.account_id.in_([a.id for a in accounts]))
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
    total_views = sum(metric.impressions for metric in metric_rows)
    event_counts = {event_type: sum(event.event_type == event_type for event in events) for event_type in (
        "link_click", "lead_initiated", "pix_generated", "pix_paid"
    )}
    metrics = {
        "active_accounts": len(accounts),
        "today_posts": len(today_posts),
        "daily_views": total_views,
        "total_views": total_views,
        "average_views": round(total_views / max(len(accounts), 1)),
        "funnel": event_counts,
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
    for offset in range(6, -1, -1):
        day = datetime.now(timezone.utc).date() - timedelta(days=offset)
        volume_days.append({
            "label": day.strftime("%d/%m"),
            "published": sum(post.status == "published" and post.created_at and post.created_at.date() == day for post in posts),
            "interactions": 0,
        })
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "accounts": accounts,
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
        },
    )


@router.get("/hub", response_class=HTMLResponse)
async def hub(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    owner_id = workspace_owner_id(user)
    accounts = (await db.scalars(select(InstagramAccount).where(InstagramAccount.owner_id == owner_id))).all()
    account_views = {}
    for account in accounts:
        account_views[account.id] = (
            await db.scalar(select(InstagramMetric.impressions).where(
                InstagramMetric.account_id == account.id
            ).order_by(InstagramMetric.metric_date.desc()).limit(1))
        ) or 0
    return templates.TemplateResponse("hub.html", {"request": request, "user": user, "accounts": accounts, "account_views": account_views, "notice": request.session.pop("access_notice", None)})


@router.get("/metrics", response_class=HTMLResponse)
async def metrics_page(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    owner_id = workspace_owner_id(user)
    accounts = (await db.scalars(select(InstagramAccount).where(InstagramAccount.owner_id == owner_id))).all()
    account_ids = [account.id for account in accounts]
    metrics = (await db.scalars(select(InstagramMetric).where(InstagramMetric.account_id.in_(account_ids)).order_by(InstagramMetric.metric_date))).all() if account_ids else []
    event_query = select(BotEvent).options(selectinload(BotEvent.account)).where(
        or_(BotEvent.account_id.in_(account_ids), BotEvent.account_id.is_(None))
    ).order_by(BotEvent.timestamp.desc()).limit(100) if account_ids else select(BotEvent).options(selectinload(BotEvent.account)).where(BotEvent.account_id.is_(None)).order_by(BotEvent.timestamp.desc()).limit(100)
    events = (await db.scalars(event_query)).all()
    views = sum(metric.impressions for metric in metrics)
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
        "request": request, "user": user, "total_views": views,
        "sharkbot_webhook_url": get_settings().sharkbot_webhook_url,
        "bot_name": "Sharkbot",
        "approved_sales": sum(event.value for event in paid_events),
        "conversion_rate": (len(paid_events) / len(generated_events) * 100) if generated_events else 0,
        "total_starts": counts["lead_initiated"],
        "average_ticket": (sum(event.value for event in paid_events) / len(paid_events)) if paid_events else 0,
        "daily_activity": daily_activity,
        "funnel": [views, counts["link_click"], counts["lead_initiated"], counts["pix_generated"], counts["pix_paid"]],
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
    return {
        "metrics": {
            "pending": sum(post.status in {"scheduled", "processing", "aguardando", "pending"} for post in posts),
            "published": sum(post.status == "published" for post in posts),
            "failed": sum(post.status == "failed" for post in posts),
        },
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
    }


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
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha no OAuth do Instagram: {exc}") from exc
    user = await db.get(User, int(request.session["user_id"]))
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    owner_id = workspace_owner_id(user)
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.owner_id == owner_id,
            InstagramAccount.instagram_user_id == str(profile.get("user_id") or profile["id"]),
        )
    )
    if account:
        account.username = profile.get("username", account.username)
        account.profile_picture_url = profile.get("profile_picture_url", account.profile_picture_url)
        account.access_token_encrypted = encrypt_token(long_lived_token)
    else:
        db.add(
            InstagramAccount(
                owner_id=owner_id,
                instagram_user_id=str(profile.get("user_id") or profile["id"]),
                username=profile.get("username", ""),
                profile_picture_url=profile.get("profile_picture_url"),
                access_token_encrypted=encrypt_token(long_lived_token),
            )
        )
    await db.commit()
    return RedirectResponse("/hub" if user.role == "collaborator" else "/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/delete")
async def delete_account(
    account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
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
    return RedirectResponse("/hub" if user.role == "collaborator" else "/dashboard", status_code=status.HTTP_303_SEE_OTHER)


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
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id, InstagramAccount.owner_id == user.id
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
        owner_id=user.id,
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
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/{post_id}/delete")
async def delete_post(
    post_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    post = await db.scalar(
        select(ScheduledPost).where(
            ScheduledPost.id == post_id,
            ScheduledPost.owner_id == user.id,
        )
    )
    if not post:
        raise HTTPException(status_code=404, detail="Publicação não encontrada")
    await db.delete(post)
    await db.commit()
    return RedirectResponse("/dashboard?tab=queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/delete-selected")
async def delete_selected_posts(
    post_ids: list[int] = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if not post_ids:
        raise HTTPException(status_code=400, detail="Nenhuma publicação selecionada")
    await db.execute(
        delete(ScheduledPost).where(
            ScheduledPost.id.in_(set(post_ids)),
            ScheduledPost.owner_id == user.id,
        )
    )
    await db.commit()
    return RedirectResponse("/dashboard?tab=queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/clear-failed")
async def clear_failed_posts(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(ScheduledPost).where(
            ScheduledPost.owner_id == user.id,
            ScheduledPost.status == "failed",
        )
    )
    await db.commit()
    return RedirectResponse("/dashboard?tab=queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/publish-selected")
async def publish_selected_posts(
    post_ids: list[int] = Form(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    posts = (
        await db.scalars(
            select(ScheduledPost).where(
                ScheduledPost.id.in_(post_ids),
                ScheduledPost.owner_id == user.id,
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
    return RedirectResponse("/dashboard?tab=queue", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/posts/bulk")
async def create_bulk_posts(
    account_ids: list[int] = Form(...),
    media_urls: list[str] = Form(...),
    media_types: list[str] = Form(...),
    captions: list[str] = Form(default=[]),
    caption_mode: str = Form("global"),
    caption: str = Form(""),
    scheduled_for: str = Form(...),
    interval_minutes: int = Form(1),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
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
                InstagramAccount.id.in_(account_ids), InstagramAccount.owner_id == user.id
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
                owner_id=user.id,
                account_id=account.id,
                media_url=media_url,
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
    return RedirectResponse("/dashboard?tab=queue", status_code=status.HTTP_303_SEE_OTHER)
