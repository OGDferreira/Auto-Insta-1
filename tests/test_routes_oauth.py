import logging
from urllib.parse import parse_qs, urlparse

from app.config import get_settings
from app.main import app
from app.routes import current_user
from app.oauth import OAUTH_SCOPES
from fastapi.testclient import TestClient


def test_instagram_start_logs_complete_authorization_url(monkeypatch, caplog):
    monkeypatch.setenv("META_APP_ID", "test-app")
    get_settings.cache_clear()
    app.dependency_overrides[current_user] = lambda: object()

    with caplog.at_level(logging.INFO, logger="app.routes"):
        with TestClient(app) as client:
            response = client.get("/auth/instagram/start", follow_redirects=False)

    app.dependency_overrides.clear()
    assert response.status_code == 307
    location = response.headers["location"]
    assert location == next(
        record.getMessage().split(": ", 1)[1]
        for record in caplog.records
        if record.getMessage().startswith("Instagram OAuth authorization URL: ")
    )
    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == ["test-app"]
    assert query["redirect_uri"] == ["https://auto-insta-web.onrender.com/auth/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [",".join(OAUTH_SCOPES)]
