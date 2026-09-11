from urllib.parse import parse_qs, urlparse

from app.oauth import OAUTH_SCOPES, authorization_url, new_state


def test_authorization_url_has_required_scopes(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "test-app")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    # Settings is cached by the application, so assert the contract in the generated URL.
    url = authorization_url("state-value")
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert query["state"] == ["state-value"]
    assert set(query["scope"][0].split(",")) == set(OAUTH_SCOPES)


def test_oauth_state_is_unpredictable():
    assert new_state() != new_state()
    assert len(new_state()) >= 32
