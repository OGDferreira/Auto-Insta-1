from functools import lru_cache
from urllib.parse import urlparse
from dotenv import load_dotenv
from pydantic import BaseModel
import os

load_dotenv()

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default



class Settings(BaseModel):
    app_version: str = "1.1.0"
    environment: str = "development"
    deploy_timestamp: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    public_base_url: str = "https://auto-insta-aeqr.onrender.com"
    database_url: str = "sqlite+aiosqlite:///./auto_insta.db"
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_service_role: str = ""
    supabase_storage_bucket: str = "media"
    sharkbot_webhook_url: str = "https://auto-insta-aeqr.onrender.com/webhook/sharkbot"
    secret_key: str = "change-me-in-production"
    fernet_key: str = ""
    webhook_verify_token: str = "change-me"
    graph_api_version: str = "v25.0"
    max_upload_size_mb: int = 50
    editor_upload_size_mb: int = 50
    upload_dir: str = "uploads"
    cookie_secure: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""

    @property
    def allowed_hosts(self) -> list[str]:
        configured = os.getenv("ALLOWED_HOSTS", "")
        if configured.strip():
            return [host.strip() for host in configured.split(",") if host.strip()]
        hostname = urlparse(self.public_base_url).hostname
        return [item for item in (hostname, "localhost", "127.0.0.1", "testserver") if item]

    def validate_production(self) -> None:
        if self.environment not in {"production", "prod"}:
            return
        required = {
            "SECRET_KEY": self.secret_key,
            "FERNET_KEY": self.fernet_key,
            "DATABASE_URL": self.database_url,
            "META_APP_ID": self.meta_app_id,
            "META_APP_SECRET": self.meta_app_secret,
            "WEBHOOK_VERIFY_TOKEN": self.webhook_verify_token,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        insecure = [name for name, value in {
            "SECRET_KEY": self.secret_key,
            "WEBHOOK_VERIFY_TOKEN": self.webhook_verify_token,
        }.items() if str(value).strip().lower().startswith("change-me")]
        if missing or insecure:
            details = ", ".join(dict.fromkeys(missing + insecure))
            raise RuntimeError(f"Configuração de produção incompleta ou insegura: {details}")

    @property
    def oauth_redirect_uri(self) -> str:
        return os.getenv("INSTAGRAM_REDIRECT_URI", f"{self.public_base_url}/auth/callback").rstrip("/")

    @property
    def secure_cookies_enabled(self) -> bool:
        return self.cookie_secure or self.public_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    public_base_url = os.getenv(
        "PUBLIC_BASE_URL",
        "https://auto-insta-aeqr.onrender.com",
    ).rstrip("/")
    cookie_secure_value = os.getenv("COOKIE_SECURE")
    cookie_secure = (
        cookie_secure_value.lower() == "true"
        if cookie_secure_value is not None
        else public_base_url.startswith("https://")
    )
    values = {
        "app_version": os.getenv("APP_VERSION", "1.1.0"),
        "environment": os.getenv("ENVIRONMENT", "development").lower(),
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
            "https://auto-insta-aeqr.onrender.com/webhook/sharkbot",
        ),
        "secret_key": os.getenv("SECRET_KEY", "change-me-in-production"),
        "fernet_key": os.getenv("FERNET_KEY", ""),
        "webhook_verify_token": os.getenv("WEBHOOK_VERIFY_TOKEN", "change-me"),
        "graph_api_version": os.getenv("GRAPH_API_VERSION", "v25.0"),
        "max_upload_size_mb": _env_int("MAX_UPLOAD_SIZE_MB", 50),
        "editor_upload_size_mb": _env_int("EDITOR_UPLOAD_SIZE_MB", _env_int("MAX_UPLOAD_SIZE_MB", 50)),
        "upload_dir": os.getenv("UPLOAD_DIR", "uploads"),
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
