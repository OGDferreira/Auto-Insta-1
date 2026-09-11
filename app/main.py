from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import init_db
from .jobs import reset_scheduler, schedule_pending_posts
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
    reset_scheduler()
    await schedule_pending_posts()
    from . import jobs

    jobs.scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    from . import jobs

    if jobs.scheduler.running:
        jobs.scheduler.shutdown(wait=False)
