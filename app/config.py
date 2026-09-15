from functools import lru_cache
from dotenv import load_dotenv
from pydantic import BaseModel
import os

load_dotenv()


class Settings(BaseModel):
    app_version: str = "1.1.0"
    deploy_timestamp: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite+aiosqlite:///./auto_insta.db"
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_storage_bucket: str = "media"
    secret_key: str = "change-me-in-production"
    fernet_key: str = ""
    webhook_verify_token: str = "change-me"
    graph_api_version: str = "v25.0"
    cookie_secure: bool = False

    @property
    def oauth_redirect_uri(self) -> str:
        return "https://auto-insta-web.onrender.com/auth/callback"


@lru_cache
def get_settings() -> Settings:
    values = {
        "app_version": os.getenv("APP_VERSION", "1.1.0"),
        "deploy_timestamp": os.getenv("DEPLOY_TIMESTAMP", ""),
        "meta_app_id": os.getenv("META_APP_ID", ""),
        "meta_app_secret": os.getenv("META_APP_SECRET", ""),
        "public_base_url": os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"),
        "database_url": os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./auto_insta.db"),
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_key": os.getenv("SUPABASE_KEY", ""),
        "supabase_storage_bucket": os.getenv("SUPABASE_STORAGE_BUCKET", "media"),
        "secret_key": os.getenv("SECRET_KEY", "change-me-in-production"),
        "fernet_key": os.getenv("FERNET_KEY", ""),
        "webhook_verify_token": os.getenv("WEBHOOK_VERIFY_TOKEN", "change-me"),
        "graph_api_version": os.getenv("GRAPH_API_VERSION", "v25.0"),
        "cookie_secure": os.getenv("COOKIE_SECURE", "false").lower() == "true",
    }
    return Settings(**values)
