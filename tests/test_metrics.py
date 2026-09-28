from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import pytest

from app import routes
from app.db import Base
from app.models import InstagramAccount, User


class FakeResponse:
    is_error = False

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeAnalyticsClient:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, params):
        self.calls.append((url, params))
        if url.endswith("/insights"):
            return FakeResponse({"data": []})
        return FakeResponse({
            "id": "ig-one",
            "username": "first_profile",
            "followers_count": 125,
            "follows_count": 88,
            "media_count": 12,
        })


def request_for_metrics():
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/metrics",
        "raw_path": b"/metrics",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "session": {},
    })


@pytest.mark.asyncio
async def test_instagram_metrics_page_renders_only_owned_connected_profiles():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="metrics@example.com", username="metrics_owner", password_hash="hash")
        other_owner = User(email="other-metrics@example.com", username="metrics_other", password_hash="hash")
        db.add_all([owner, other_owner])
        await db.flush()
        db.add_all([
            InstagramAccount(
                owner_id=owner.id,
                instagram_user_id="ig-one",
                username="first_profile",
                access_token_encrypted="encrypted",
            ),
            InstagramAccount(
                owner_id=owner.id,
                instagram_user_id="ig-two",
                username="second_profile",
                access_token_encrypted="encrypted",
            ),
            InstagramAccount(
                owner_id=other_owner.id,
                instagram_user_id="ig-other",
                username="private_profile",
                access_token_encrypted="encrypted",
            ),
        ])
        await db.commit()

        response = await routes.instagram_metrics_page(
            request_for_metrics(), user=owner, db=db
        )
        html = response.body.decode()
        assert 'data-metrics-account="1"' in html
        assert 'data-metrics-account="2"' in html
        assert "private_profile" not in html
        assert 'data-profile-stat="followers"' in html
        assert 'id="profile-feed"' in html

    await engine.dispose()


@pytest.mark.asyncio
async def test_analytics_can_load_profile_counters_without_fetching_media(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    client = FakeAnalyticsClient()
    monkeypatch.setattr(routes.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(routes, "decrypt_token", lambda _token: "access-token")

    async with session_factory() as db:
        owner = User(email="analytics@example.com", username="analytics_owner", password_hash="hash")
        db.add(owner)
        await db.flush()
        account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-one",
            username="first_profile",
            access_token_encrypted="encrypted",
        )
        db.add(account)
        await db.commit()

        payload = await routes.analytics(
            account_ids=[account.id],
            period_days=30,
            include_media=False,
            user=owner,
            db=db,
        )
        assert payload["accounts"][0]["followers"] == 125
        assert payload["accounts"][0]["following"] == 88
        assert payload["accounts"][0]["media_count"] == 12
        assert payload["media"] == []
        assert len(client.calls) == 1
        assert "follows_count" in client.calls[0][1]["fields"]

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 123, None, "unavailable"])
async def test_account_engagement_is_available_without_loading_media(monkeypatch, value):
    class EngagementClient(FakeAnalyticsClient):
        async def get(self, url, params):
            response = await super().get(url, params)
            if params.get("metric") == "total_interactions":
                assert params["metric_type"] == "total_value"
                assert params["period"] == "day"
                assert params["until"] - params["since"] == 30 * 86400
                if value == "unavailable":
                    raise routes.httpx.ConnectError("offline")
                return FakeResponse({"data": [{"name": "total_interactions", "total_value": {"value": value}}]})
            return response
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = EngagementClient()
    monkeypatch.setattr(routes.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(routes, "decrypt_token", lambda token: "test-token")
    async with factory() as db:
        owner = User(username="ranking_owner", password_hash="hash")
        other = User(username="ranking_other", password_hash="hash")
        db.add_all([owner, other])
        await db.flush()
        db.add_all([InstagramAccount(owner_id=owner.id, username="own", instagram_user_id="own", access_token_encrypted="token"), InstagramAccount(owner_id=other.id, username="other", instagram_user_id="other", access_token_encrypted="token")])
        await db.commit()
        result = await routes.analytics(account_ids=[], period_days=30, include_media=False, include_account_engagement=True, user=owner, db=db)
        assert len(result["accounts"]) == 1
        assert result["accounts"][0]["engagement"] == (None if value == "unavailable" else value)
        assert result["media"] == [] and len(client.calls) == 2
    await engine.dispose()
