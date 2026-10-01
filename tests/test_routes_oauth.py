import logging
from urllib.parse import parse_qs, urlparse

from app.config import get_settings
from app.main import app
from app.routes import current_user
from app.oauth import OAUTH_SCOPES
from fastapi.testclient import TestClient


def test_instagram_start_logs_safe_oauth_metadata(monkeypatch, caplog):
    monkeypatch.setenv("META_APP_ID", "test-app")
    get_settings.cache_clear()
    app.dependency_overrides[current_user] = lambda: object()

    with caplog.at_level(logging.INFO, logger="app.routes"):
        with TestClient(app) as client:
            response = client.get("/auth/instagram/start", follow_redirects=False)

    app.dependency_overrides.clear()
    assert response.status_code == 307
    location = response.headers["location"]
    assert any(
        record.getMessage().startswith("Instagram OAuth authorization started: ")
        for record in caplog.records
    )
    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == ["test-app"]
    assert query["redirect_uri"] == ["https://auto-insta-aeqr.onrender.com/auth/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [",".join(OAUTH_SCOPES)]
