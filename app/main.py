from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import init_db
from . import jobs
from .jobs import collect_instagram_insights, reset_scheduler, schedule_pending_posts
from .routes import router
from .webhooks import router as webhook_router
from .observability import configure_logging

settings = get_settings()
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
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
app.include_router(router)
app.include_router(webhook_router)


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    reset_scheduler()
    await schedule_pending_posts()
    await collect_instagram_insights()
    jobs.scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if jobs.scheduler.running:
        jobs.scheduler.shutdown(wait=False)
