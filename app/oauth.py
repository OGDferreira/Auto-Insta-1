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
    query = urlencode(
        {
            "client_id": settings.meta_app_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "response_type": "code",
            "scope": ",".join(OAUTH_SCOPES),
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


async def fetch_profile(access_token: str) -> dict:
    settings = get_settings()
    url = f"https://graph.instagram.com/{settings.graph_api_version}/me"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            url, params={"fields": "id,user_id,username", "access_token": access_token}
        )
        response.raise_for_status()
        return response.json()
