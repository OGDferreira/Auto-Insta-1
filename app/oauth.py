import secrets
from urllib.parse import urlencode

import httpx

from .config import get_settings

OAUTH_SCOPES = (
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
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


async def exchange_long_lived_token(short_token: str) -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://graph.instagram.com/{settings.graph_api_version}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.meta_app_secret,
                "access_token": short_token,
            },
        )
        response.raise_for_status()
        return response.json()["access_token"]


async def fetch_profile(access_token: str) -> dict:
    settings = get_settings()
    url = f"https://graph.instagram.com/{settings.graph_api_version}/me"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            url, params={"fields": "id,user_id,username,profile_picture_url", "access_token": access_token}
        )
        response.raise_for_status()
        return response.json()
