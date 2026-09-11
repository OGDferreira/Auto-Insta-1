from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
import os

load_dotenv()


class Settings(BaseModel):
    meta_app_id: str = ""
    meta_app_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite+aiosqlite:///./auto_insta.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "change-me-in-production"
    fernet_key: str = ""
    webhook_verify_token: str = "change-me"
    graph_api_version: str = "v25.0"
    cookie_secure: bool = False

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/instagram/callback"


@lru_cache
def get_settings() -> Settings:
    values = {
        "meta_app_id": os.getenv("META_APP_ID", ""),
        "meta_app_secret": os.getenv("META_APP_SECRET", ""),
        "public_base_url": os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"),
        "database_url": os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./auto_insta.db"),
        "redis_url": os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        "secret_key": os.getenv("SECRET_KEY", "change-me-in-production"),
        "fernet_key": os.getenv("FERNET_KEY", ""),
        "webhook_verify_token": os.getenv("WEBHOOK_VERIFY_TOKEN", "change-me"),
        "graph_api_version": os.getenv("GRAPH_API_VERSION", "v25.0"),
        "cookie_secure": os.getenv("COOKIE_SECURE", "false").lower() == "true",
    }
    # Render may provide postgres://, which SQLAlchemy needs as postgresql+asyncpg.
    if values["database_url"].startswith("postgres://"):
        values["database_url"] = values["database_url"].replace(
            "postgres://", "postgresql+asyncpg://", 1
        )
    elif values["database_url"].startswith("postgresql://"):
        values["database_url"] = values["database_url"].replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    return Settings(**values)
