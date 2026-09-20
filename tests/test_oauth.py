from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.oauth import OAUTH_SCOPES, authorization_url, new_state


def test_authorization_url_has_required_scopes(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "test-app")
    from app.config import get_settings

    get_settings.cache_clear()
    url = authorization_url("state-value")
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert query["client_id"] == ["test-app"]
    assert query["redirect_uri"] == ["https://auto-insta-web.onrender.com/auth/callback"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-value"]
    assert set(query["scope"][0].split(",")) == set(OAUTH_SCOPES)


def test_authorization_url_requires_app_id(monkeypatch):
    monkeypatch.delenv("META_APP_ID", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        authorization_url("state-value")
    except RuntimeError as exc:
        assert str(exc) == "META_APP_ID must be configured before starting Instagram OAuth"
    else:
        raise AssertionError("authorization_url should reject an empty META_APP_ID")


def test_oauth_state_is_unpredictable():
    assert new_state() != new_state()
    assert len(new_state()) >= 32


def test_secure_cookie_defaults_to_https_public_base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com")
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.public_base_url == "https://app.example.com"
    assert settings.cookie_secure is True
    assert settings.secure_cookies_enabled is True


@pytest.mark.asyncio
async def test_exchange_long_lived_token_uses_unversioned_instagram_endpoint(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    monkeypatch.setenv("GRAPH_API_VERSION", "v25.0")
    from app.config import get_settings

    get_settings.cache_clear()
    requests = []
    async_client = httpx.AsyncClient

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "long-lived-token"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    from app.oauth import exchange_long_lived_token

    assert await exchange_long_lived_token("short-token") == "long-lived-token"
    assert len(requests) == 1
    assert str(requests[0].url) == (
        "https://graph.instagram.com/access_token"
        "?grant_type=ig_exchange_token&client_secret=test-secret&access_token=short-token"
    )
