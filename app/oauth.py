import secrets
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from .config import get_settings

OAUTH_SCOPES = (
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
    "instagram_business_manage_insights",
)


def token_expiration_from_data(token_data: dict) -> datetime | None:
    try:
        lifetime_seconds = int(token_data.get("expires_in", 0))
    except (TypeError, ValueError):
        return None
    return (
        datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)
        if lifetime_seconds > 0
        else None
    )


def authorization_url(state: str) -> str:
    settings = get_settings()
    client_id = settings.meta_app_id.strip()
    redirect_uri = settings.oauth_redirect_uri
    scope = ",".join(OAUTH_SCOPES)
    if not client_id:
        raise RuntimeError("META_APP_ID must be configured before starting Instagram OAuth")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
        }
    )
    return f"https://www.instagram.com/oauth/authorize?{query}"


def new_state() -> str:
    return secrets.token_urlsafe(32)

def new_oauth_state(user_id: int, reconnect_account_id: int | None = None) -> str:
    """Create a short-lived, signed state that survives cookie loss on provider return."""
    serializer = URLSafeTimedSerializer(get_settings().secret_key, salt="instagram-oauth")
    return serializer.dumps({"user_id": int(user_id), "reconnect_account_id": reconnect_account_id, "nonce": new_state()})

def read_oauth_state(value: str, max_age: int = 900) -> dict | None:
    serializer = URLSafeTimedSerializer(get_settings().secret_key, salt="instagram-oauth")
    try:
        payload = serializer.loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return payload if isinstance(payload, dict) and payload.get("user_id") else None


async def exchange_code(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": settings.oauth_redirect_uri,
                "code": code,
            },
        )
        response.raise_for_status()
        return response.json()


async def exchange_long_lived_token_data(short_token: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.meta_app_secret,
                "access_token": short_token,
            },
        )
        response.raise_for_status()
        return response.json()


async def exchange_long_lived_token(short_token: str) -> str:
    token_data = await exchange_long_lived_token_data(short_token)
    return token_data["access_token"]


async def fetch_profile(access_token: str) -> dict:
    settings = get_settings()
    url = f"https://graph.instagram.com/{settings.graph_api_version}/me"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            url, params={"fields": "id,user_id,username,profile_picture_url", "access_token": access_token}
        )
        response.raise_for_status()
        return response.json()


async def fetch_instagram_business_account(access_token: str, stored_id: str | None = None) -> dict | None:
    """Resolve the Instagram Business Account ID through connected Facebook Pages."""
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://graph.instagram.com/{get_settings().graph_api_version}/me",
            params={
                "fields": "id,user_id,username,profile_picture_url",
                "access_token": access_token,
            },
        )
        if response.is_error:
            return None
        profile = response.json()
        return {
            "instagram_user_id": str(profile.get("user_id") or profile.get("id")),
            "username": profile.get("username") or "",
            "profile_picture_url": profile.get("profile_picture_url"),
        }
