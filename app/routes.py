from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_db
from .jobs import schedule_post
from .models import InstagramAccount, ScheduledPost, User
from .oauth import (
    authorization_url,
    exchange_code,
    exchange_long_lived_token,
    fetch_profile,
    new_state,
)
from .security import encrypt_token, hash_password, verify_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
UPLOAD_DIR = Path("uploads")
ALLOWED_UPLOAD_TYPES = {"image/jpeg", "image/png", "image/webp", "video/mp4", "video/quicktime"}
MAX_UPLOAD_SIZE = 50 * 1024 * 1024


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    user = await db.get(User, int(user_id))
    if not user:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return user


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


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
    destination = UPLOAD_DIR / filename
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await media.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="Arquivo excede o limite de 50 MB")
                output.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await media.close()
    settings = get_settings()
    return {
        "url": f"{settings.public_base_url.rstrip('/')}/uploads/{filename}",
        "media_type": "VIDEO" if media.content_type.startswith("video/") else "IMAGE",
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    email = email.strip().lower()
    if len(password) < 10:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "Senha deve ter ao menos 10 caracteres"}, status_code=400
        )
    existing = await db.scalar(select(User).where(User.email == email))
    if existing:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": "E-mail já cadastrado"}, status_code=409
        )
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    await db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Credenciais inválidas"}, status_code=401
        )
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    accounts = (await db.scalars(select(InstagramAccount).where(InstagramAccount.owner_id == user.id))).all()
    posts = (
        await db.scalars(
            select(ScheduledPost)
            .where(ScheduledPost.owner_id == user.id)
            .order_by(ScheduledPost.scheduled_for.desc())
        )
    ).all()
    today = datetime.now(timezone.utc).date()
    today_posts = [post for post in posts if post.created_at and post.created_at.date() == today]
    metrics = {
        "active_accounts": len(accounts),
        "today_posts": len(today_posts),
        "daily_views": 0,
        "average_views": 0,
        "published": sum(post.status == "published" for post in posts),
        "pending": sum(post.status == "scheduled" for post in posts),
        "failed": sum(post.status == "failed" for post in posts),
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "accounts": accounts, "posts": posts, "metrics": metrics},
    )


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
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.owner_id == user.id,
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
                owner_id=user.id,
                instagram_user_id=str(profile.get("user_id") or profile["id"]),
                username=profile.get("username", ""),
                profile_picture_url=profile.get("profile_picture_url"),
                access_token_encrypted=encrypt_token(long_lived_token),
            )
        )
    await db.commit()
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/delete")
async def delete_account(
    account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    account = await db.scalar(
        select(InstagramAccount).where(
            InstagramAccount.id == account_id, InstagramAccount.owner_id == user.id
        )
    )
    if not account:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    await db.delete(account)
    await db.commit()
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/auto-reply")
async def update_auto_reply(
    account_id: int,
    enabled: bool = Form(False),
    text: str = Form(""),
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
    account.auto_reply_enabled, account.auto_reply_text = enabled, text[:2000]
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
    try:
        when = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="scheduled_for inválido") from exc
    if when < datetime.now(timezone.utc) + timedelta(minutes=1):
        raise HTTPException(status_code=400, detail="Agendamento deve ser pelo menos 1 minuto no futuro")
    media_type = media_type.upper()
    if media_type not in {"IMAGE", "VIDEO"}:
        raise HTTPException(status_code=400, detail="media_type deve ser IMAGE ou VIDEO")
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


@router.post("/posts/bulk")
async def create_bulk_posts(
    account_ids: list[int] = Form(...),
    media_urls: list[str] = Form(...),
    media_types: list[str] = Form(...),
    captions: list[str] = Form(...),
    scheduled_for: str = Form(...),
    interval_minutes: int = Form(1),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if interval_minutes < 1:
        raise HTTPException(status_code=400, detail="O intervalo mínimo é de 1 minuto")
    if not media_urls or not (len(media_urls) == len(media_types) == len(captions)):
        raise HTTPException(status_code=400, detail="Lista de mídias inválida")
    accounts = (
        await db.scalars(
            select(InstagramAccount).where(
                InstagramAccount.id.in_(account_ids), InstagramAccount.owner_id == user.id
            )
        )
    ).all()
    if len(accounts) != len(set(account_ids)) or not accounts:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    try:
        first_time = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        first_time = first_time if first_time.tzinfo else first_time.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="scheduled_for inválido") from exc
    if first_time < datetime.now(timezone.utc) + timedelta(minutes=1):
        raise HTTPException(status_code=400, detail="Agendamento deve ser pelo menos 1 minuto no futuro")

    posts_to_schedule = []
    for media_index, (media_url, media_type, caption) in enumerate(
        zip(media_urls, media_types, captions)
    ):
        normalized_type = media_type.upper()
        if normalized_type not in {"IMAGE", "VIDEO"}:
            raise HTTPException(status_code=400, detail="media_type deve ser IMAGE ou VIDEO")
        media_time = first_time + timedelta(minutes=media_index * interval_minutes)
        for account_index, account in enumerate(accounts):
            post = ScheduledPost(
                owner_id=user.id,
                account_id=account.id,
                media_url=media_url,
                media_type=normalized_type,
                caption=caption,
                scheduled_for=media_time + timedelta(minutes=account_index if len(accounts) > 1 else 0),
            )
            db.add(post)
            posts_to_schedule.append(post)
    await db.flush()
    await db.commit()
    for post in posts_to_schedule:
        schedule_post(post.id, post.scheduled_for)
    return RedirectResponse("/dashboard?tab=queue", status_code=status.HTTP_303_SEE_OTHER)
