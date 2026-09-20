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
    supabase_service_role: str = ""
    supabase_storage_bucket: str = "media"
    sharkbot_webhook_url: str = "https://auto-insta-web.onrender.com/webhook/sharkbot"
    secret_key: str = "change-me-in-production"
    fernet_key: str = ""
    webhook_verify_token: str = "change-me"
    graph_api_version: str = "v25.0"
    cookie_secure: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""

    @property
    def oauth_redirect_uri(self) -> str:
        return "https://auto-insta-web.onrender.com/auth/callback"

    @property
    def secure_cookies_enabled(self) -> bool:
        return self.cookie_secure or self.public_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    public_base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    cookie_secure_value = os.getenv("COOKIE_SECURE")
    cookie_secure = (
        cookie_secure_value.lower() == "true"
        if cookie_secure_value is not None
        else public_base_url.startswith("https://")
    )
    values = {
        "app_version": os.getenv("APP_VERSION", "1.1.0"),
        "deploy_timestamp": os.getenv("DEPLOY_TIMESTAMP", ""),
        "meta_app_id": os.getenv("META_APP_ID", ""),
        "meta_app_secret": os.getenv("META_APP_SECRET", ""),
        "public_base_url": public_base_url,
        "database_url": os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./auto_insta.db"),
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_key": os.getenv("SUPABASE_KEY", ""),
        "supabase_service_role": os.getenv("SERVICE_ROLE", ""),
        "supabase_storage_bucket": os.getenv("SUPABASE_STORAGE_BUCKET", "media"),
        "sharkbot_webhook_url": os.getenv(
            "SHARKBOT_WEBHOOK_URL",
            "https://auto-insta-web.onrender.com/webhook/sharkbot",
        ),
        "secret_key": os.getenv("SECRET_KEY", "change-me-in-production"),
        "fernet_key": os.getenv("FERNET_KEY", ""),
        "webhook_verify_token": os.getenv("WEBHOOK_VERIFY_TOKEN", "change-me"),
        "graph_api_version": os.getenv("GRAPH_API_VERSION", "v25.0"),
        "cookie_secure": cookie_secure,
        "google_client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "google_client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "google_redirect_uri": os.getenv(
            "GOOGLE_REDIRECT_URI",
            f"{public_base_url}/auth/google/callback",
        ),
        "vapid_public_key": os.getenv("VAPID_PUBLIC_KEY", ""),
        "vapid_private_key": os.getenv("VAPID_PRIVATE_KEY", ""),
        "vapid_subject": os.getenv("VAPID_SUBJECT", ""),
    }
    return Settings(**values)
