from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import init_db
from . import jobs
from .jobs import reset_scheduler, schedule_pending_posts
from .routes import router
from .webhooks import router as webhook_router
from .observability import configure_logging

settings = get_settings()
settings.validate_production()
configure_logging()
app = FastAPI(title="Auto-Insta", version=settings.app_version)
app.mount("/uploads", StaticFiles(directory="uploads", check_dir=False), name="uploads")
app.mount("/static", StaticFiles(directory="app/static", check_dir=False), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.secure_cookies_enabled,
    same_site="lax",
    max_age=60 * 60 * 24 * 14,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://graph.instagram.com https://api.instagram.com https://*.supabase.co; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if settings.secure_cookies_enabled:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response
app.include_router(router)
app.include_router(webhook_router)


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    reset_scheduler()
    await schedule_pending_posts()
    jobs.scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if jobs.scheduler.running:
        jobs.scheduler.shutdown(wait=False)
