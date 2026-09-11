from datetime import datetime, timezone
from urllib.parse import quote

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
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
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "user": user, "accounts": accounts, "posts": posts}
    )


@router.get("/auth/instagram/start")
async def instagram_start(request: Request, user: User = Depends(current_user)):
    state = new_state()
    request.session["instagram_oauth_state"] = state
    redirect_url = authorization_url(state)
    logger.info("Instagram OAuth authorization URL: %s", redirect_url)
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
        account.access_token_encrypted = encrypt_token(long_lived_token)
    else:
        db.add(
            InstagramAccount(
                owner_id=user.id,
                instagram_user_id=str(profile.get("user_id") or profile["id"]),
                username=profile.get("username", ""),
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
    if when <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Agendamento deve estar no futuro")
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
