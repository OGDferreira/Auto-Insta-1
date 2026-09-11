from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_webhook_verification_returns_plain_text_challenge(monkeypatch):
    monkeypatch.setenv("WEBHOOK_VERIFY_TOKEN", "test-token")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test-token",
                "hub.challenge": "123456",
            },
        )

    assert response.status_code == 200
    assert response.text == "123456"
    assert response.headers["content-type"].startswith("text/plain")


def test_webhook_verification_rejects_invalid_token(monkeypatch):
    monkeypatch.setenv("WEBHOOK_VERIFY_TOKEN", "test-token")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "123456",
            },
        )

    assert response.status_code == 403
