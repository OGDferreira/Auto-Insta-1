from types import SimpleNamespace

import pytest

from app.jobs import _refresh_account_status


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def get(self, *_args, **_kwargs):
        return next(self.responses)


def account():
    return SimpleNamespace(
        instagram_user_id="123",
        connection_status="pending",
        status_reason=None,
        status_checked_at=None,
    )


@pytest.mark.asyncio
async def test_refresh_account_status_requires_publish_permission():
    current = account()
    client = FakeClient([
        FakeResponse({"id": "123", "username": "tester"}),
        FakeResponse({"data": [{"permission": "instagram_business_basic", "status": "granted"}]}),
    ])

    result = await _refresh_account_status(
        client,
        current,
        "token",
        SimpleNamespace(graph_api_version="v22.0"),
    )

    assert result is False
    assert current.connection_status == "error"
    assert "não autorizou" in current.status_reason
    assert current.status_checked_at is not None


@pytest.mark.asyncio
async def test_refresh_account_status_accepts_publish_permission():
    current = account()
    client = FakeClient([
        FakeResponse({"id": "123", "username": "tester"}),
        FakeResponse({"data": [{"permission": "instagram_business_content_publish", "status": "granted"}]}),
    ])

    result = await _refresh_account_status(
        client,
        current,
        "token",
        SimpleNamespace(graph_api_version="v22.0"),
    )

    assert result is True
    assert current.connection_status == "connected"
    assert current.status_reason is None
    assert current.status_checked_at is not None
