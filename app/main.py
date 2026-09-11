from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import init_db
from .jobs import schedule_pending_posts, scheduler
from .routes import router
from .webhooks import router as webhook_router

settings = get_settings()
app = FastAPI(title="Auto-Insta", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.cookie_secure,
    same_site="lax",
    max_age=60 * 60 * 24 * 14,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
app.include_router(router)
app.include_router(webhook_router)


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    await schedule_pending_posts()
    scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
