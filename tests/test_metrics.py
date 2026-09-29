import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import pytest

from app import routes
from app.db import Base
from app.models import BotEvent, InstagramAccount, InstagramMetric, User


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
        assert 'id="profile-account-engagement-sort"' in html
        assert 'id="profile-account-sort-status"' in html

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
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 10),
            include_media=False,
            include_account_insights=True,
            user=owner,
            db=db,
        )
        assert payload["accounts"][0]["followers"] == 125
        assert payload["accounts"][0]["following"] == 88
        assert payload["accounts"][0]["media_count"] == 12
        assert payload["media"] == []
        assert payload["period_days"] == 10
        assert payload["start_date"] == "2026-09-01"
        assert payload["end_date"] == "2026-09-10"
        assert len(client.calls) == 2
        assert "follows_count" in client.calls[0][1]["fields"]
        insight_call = next(call for call in client.calls if call[0].endswith("/insights"))
        assert insight_call[1]["since"] == "2026-09-01"
        assert insight_call[1]["until"] == "2026-09-10"

    await engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_total_period_includes_all_historical_metric_snapshots():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="period@example.com", username="period_owner", password_hash="hash")
        db.add(owner)
        await db.flush()
        account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-period",
            username="period_profile",
        )
        db.add(account)
        await db.flush()
        now = datetime.now(timezone.utc)
        db.add_all([
            InstagramMetric(
                account_id=account.id,
                metric_date=now - timedelta(days=100),
                impressions=100,
            ),
            InstagramMetric(
                account_id=account.id,
                metric_date=now - timedelta(days=1),
                impressions=10,
            ),
        ])
        await db.commit()

        total_response = await routes.api_status(period_days=0, user=owner, db=db)
        recent_response = await routes.api_status(period_days=7, user=owner, db=db)
        total_metrics = json.loads(total_response.body)["metrics"]
        recent_metrics = json.loads(recent_response.body)["metrics"]

        assert total_metrics["total_views"] == 110
        assert recent_metrics["total_views"] == 10

    await engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_cards_and_paid_chart_follow_selected_period():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="dashboard-cards@example.com", username="dashboard_cards", password_hash="hash")
        db.add(owner)
        await db.flush()
        connected_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-dashboard-connected",
            username="dashboard_connected",
            access_token_encrypted="encrypted",
            connection_status="connected",
            followers_count=1200,
        )
        error_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-dashboard-error",
            username="dashboard_error",
            access_token_encrypted="encrypted",
            connection_status="error",
            followers_count=9000,
        )
        db.add_all([connected_account, error_account])
        await db.flush()
        today = datetime.now(routes.LOCAL_TIMEZONE).date()
        today_start, _ = routes._local_day_bounds(today)
        yesterday_start, _ = routes._local_day_bounds(today - timedelta(days=1))
        old_timestamp = datetime(2000, 1, 1, tzinfo=timezone.utc)
        db.add_all([
            BotEvent(
                account_id=connected_account.id,
                event_type="pix_generated",
                timestamp=old_timestamp,
                created_at=today_start + timedelta(hours=1),
            ),
            BotEvent(
                account_id=connected_account.id,
                event_type="pix_generated",
                timestamp=old_timestamp,
                created_at=today_start + timedelta(hours=2),
            ),
            BotEvent(
                account_id=connected_account.id,
                event_type="pix_paid",
                value=25.5,
                timestamp=old_timestamp,
                created_at=today_start + timedelta(hours=3),
            ),
            BotEvent(
                account_id=connected_account.id,
                event_type="pix_paid",
                value=15,
                timestamp=old_timestamp,
                created_at=yesterday_start + timedelta(hours=3),
            ),
        ])
        await db.commit()

        today_response = await routes.api_status(period_days=1, user=owner, db=db)
        week_response = await routes.api_status(period_days=7, user=owner, db=db)
        today_payload = json.loads(today_response.body)
        week_payload = json.loads(week_response.body)

        assert today_payload["metrics"]["net_followers"] == 1200
        assert today_payload["funnel_rates"]["pix_to_paid"] == 50
        assert today_payload["volume_days"][-1]["label"] == today.strftime("%d/%m/%Y")
        assert today_payload["volume_days"][-1]["revenue"] == 25.5
        assert week_payload["metrics"]["net_followers"] == 1200
        assert week_payload["funnel_rates"]["pix_to_paid"] == 100
        assert sum(day["revenue"] for day in week_payload["volume_days"]) == 40.5

    await engine.dispose()


def test_dashboard_period_bounds_use_local_calendar_days():
    now = datetime(2026, 9, 29, 15, 30, tzinfo=timezone.utc)
    today = now.astimezone(routes.LOCAL_TIMEZONE).date()

    today_start, today_end, today_chart_start, today_chart_end, today_days = (
        routes._dashboard_period_bounds(1, now)
    )
    yesterday_start, yesterday_end, yesterday_chart_start, yesterday_chart_end, yesterday_days = (
        routes._dashboard_period_bounds(2, now)
    )

    assert (today_start, today_end) == routes._local_day_bounds(today)
    assert (yesterday_start, yesterday_end) == routes._local_day_bounds(
        today - timedelta(days=1)
    )
    assert (today_chart_start, today_chart_end, today_days) == (today, today, 1)
    assert (yesterday_chart_start, yesterday_chart_end, yesterday_days) == (
        today - timedelta(days=1),
        today - timedelta(days=1),
        1,
    )
    range_start = today - timedelta(days=3)
    range_end = today - timedelta(days=1)
    custom_start, custom_end, custom_chart_start, custom_chart_end, custom_days = (
        routes._dashboard_period_bounds(
            7,
            now,
            start_date=range_start,
            end_date=range_end,
        )
    )
    assert (custom_start, custom_end) == (
        routes._local_day_bounds(range_start)[0],
        routes._local_day_bounds(range_end)[1],
    )
    assert (custom_chart_start, custom_chart_end, custom_days) == (
        range_start,
        range_end,
        3,
    )


@pytest.mark.asyncio
async def test_dashboard_period_filters_metrics_and_sharkbot_by_received_at():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        owner = User(email="period-filter@example.com", username="period_filter", password_hash="hash")
        db.add(owner)
        await db.flush()
        account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-period-filter",
            username="period_filter",
            access_token_encrypted="encrypted",
            connection_status="connected",
        )
        disconnected_account = InstagramAccount(
            owner_id=owner.id,
            instagram_user_id="ig-period-filter-disconnected",
            username="period_filter_disconnected",
            access_token_encrypted="encrypted",
            connection_status="disconnected",
        )
        db.add_all([account, disconnected_account])
        await db.flush()
        today = datetime.now(routes.LOCAL_TIMEZONE).date()
        today_start, today_end = routes._local_day_bounds(today)
        yesterday_start, _ = routes._local_day_bounds(today - timedelta(days=1))
        db.add_all([
            InstagramMetric(
                account_id=account.id,
                metric_date=today_start + timedelta(hours=1),
                impressions=11,
            ),
            InstagramMetric(
                account_id=account.id,
                metric_date=today_end - timedelta(microseconds=1),
                impressions=7,
            ),
            InstagramMetric(
                account_id=account.id,
                metric_date=today_start - timedelta(days=8),
                impressions=100,
            ),
            InstagramMetric(
                account_id=account.id,
                metric_date=yesterday_start + timedelta(hours=2),
                impressions=5,
            ),
        ])
        old_payload_timestamp = datetime(2000, 1, 1, tzinfo=timezone.utc)
        db.add_all([
            BotEvent(
                account_id=account.id,
                event_type="lead_initiated",
                timestamp=old_payload_timestamp,
                created_at=today_start + timedelta(hours=1),
            ),
            BotEvent(
                account_id=account.id,
                event_type="lead_initiated",
                timestamp=old_payload_timestamp,
                created_at=today_end - timedelta(microseconds=1),
            ),
            BotEvent(
                account_id=account.id,
                event_type="lead_initiated",
                timestamp=old_payload_timestamp,
                created_at=yesterday_start + timedelta(hours=1),
            ),
            BotEvent(
                account_id=account.id,
                event_type="lead_initiated",
                timestamp=old_payload_timestamp,
                created_at=today_start - timedelta(days=8),
            ),
        ])
        await db.commit()

        today_response = await routes.api_status(period_days=1, user=owner, db=db)
        yesterday_response = await routes.api_status(period_days=2, user=owner, db=db)
        week_response = await routes.api_status(period_days=7, user=owner, db=db)
        custom_response = await routes.api_status(
            period_days=7,
            start_date=today - timedelta(days=1),
            end_date=today,
            user=owner,
            db=db,
        )
        today_payload = json.loads(today_response.body)
        yesterday_payload = json.loads(yesterday_response.body)
        week_payload = json.loads(week_response.body)
        custom_payload = json.loads(custom_response.body)

        assert today_payload["metrics"]["total_views"] == 18
        assert today_payload["sharkbot"]["lead_initiated"] == 2
        assert today_payload["period_label"] == "HOJE"
        assert yesterday_payload["metrics"]["total_views"] == 5
        assert yesterday_payload["sharkbot"]["lead_initiated"] == 1
        assert yesterday_payload["period_label"] == "ONTEM"
        assert week_payload["metrics"]["total_views"] == 23
        assert week_payload["sharkbot"]["lead_initiated"] == 3
        assert today_payload["metrics"]["active_accounts"] == 1
        assert custom_payload["metrics"]["total_views"] == 23
        assert custom_payload["sharkbot"]["lead_initiated"] == 3
        assert custom_payload["period_key"] == "custom"
        assert custom_payload["period_label"] == (
            f"{(today - timedelta(days=1)).strftime('%d/%m/%Y')} – {today.strftime('%d/%m/%Y')}"
        )

        request = Request({
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/dashboard",
            "raw_path": b"/dashboard",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testserver", 50000),
            "session": {},
        })
        dashboard_response = await routes.dashboard(
            request,
            period_days=2,
            user=owner,
            db=db,
        )
        dashboard_html = dashboard_response.body.decode()
        assert 'data-dashboard-period="2" aria-pressed="true">Ontem</button>' in dashboard_html
        assert "ONTEM" in dashboard_html
        assert '<strong data-metric="total_views">5</strong>' in dashboard_html
        assert '<strong data-sharkbot-metric="lead_initiated">1</strong>' in dashboard_html

    await engine.dispose()
