from fastapi.testclient import TestClient

from app.main import app
from app.oauth import new_oauth_state, read_oauth_state
from app.security import csrf_token_matches, ensure_csrf_token


def test_csrf_token_is_stable_per_session_and_constant_time_comparison():
    session = {}
    token = ensure_csrf_token(session)
    assert token == ensure_csrf_token(session)
    assert csrf_token_matches(token, token)
    assert not csrf_token_matches(token, token + "x")
    assert not csrf_token_matches(token, None)


def test_oauth_state_is_signed_and_rejects_tampering():
    state = new_oauth_state(42, reconnect_account_id=7, nonce="nonce-value")
    assert read_oauth_state(state) == {
        "user_id": 42,
        "reconnect_account_id": 7,
        "nonce": "nonce-value",
    }
    assert read_oauth_state(state + "tampered") is None


def test_security_headers_are_present():
    with TestClient(app) as client:
        response = client.get("/login")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
