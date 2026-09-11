from urllib.parse import parse_qs, urlparse

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
