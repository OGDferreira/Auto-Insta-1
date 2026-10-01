import asyncio
import secrets

from sqlalchemy import select
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.db import SessionLocal
from app.models import BotEvent, User
from app.jobs import _account_status_from_error
from httpx import Response


def _webhook_url_and_owner():
    async def create_owner():
        async with SessionLocal() as db:
            owner = User(
                email=f"webhook-{secrets.token_hex(8)}@example.com",
                username=f"webhook{secrets.token_hex(4)}",
                password_hash="test",
            )
            db.add(owner)
            await db.commit()
            return owner.id, owner.sharkbot_webhook_token

    owner_id, token = asyncio.run(create_owner())
    return f"/webhook/sharkbot/{token}", owner_id


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


def test_account_status_classifies_meta_challenge_as_connection_error():
    response = Response(
        403,
        json={"error": {"message": "Instagram checkpoint challenge required"}},
    )
    assert _account_status_from_error(response) == (
        "error",
        "Instagram checkpoint challenge required",
    )


def test_sharkbot_requires_individual_token():
    with TestClient(app) as client:
        response = client.post("/webhook/sharkbot", json={"event": "payment_created"})
    assert response.status_code == 401


def test_sharkbot_events_store_real_payload_data():
    webhook_url, owner_id = _webhook_url_and_owner()
    webhook_id = f"abc-{secrets.token_hex(5)}"
    payloads = [
        {
            "timestamp": 1789578257,
            "webhook_id": webhook_id,
            "event": "payment_approved",
            "data": {
                "customer": {"first_name": "João", "last_name": "Silva", "username": "joaosilva"},
                "bot": {"name": "Meu Bot"},
                "transaction": {"id": "payment_id_123", "amount": 97, "plan_name": "Plano Premium"},
            },
        },
        {
            "event": "payment_created",
            "timestamp": 1789578257,
            "webhook_id": webhook_id,
            "data": {
                "customer": {"first_name": "João", "last_name": "Silva"},
                "transaction": {"id": "payment_id_124", "amount": 97},
            },
        },
        {
            "event": "user_joined",
            "timestamp": 1789578257,
            "webhook_id": webhook_id,
            "data": {"customer": {"first_name": "Maria", "last_name": "Souza"}},
        },
    ]
    with TestClient(app) as client:
        responses = [client.post(webhook_url, json=payload) for payload in payloads]
    assert [response.status_code for response in responses] == [200, 200, 200]

    async def read_events():
        async with SessionLocal() as db:
            return (await db.scalars(
                select(BotEvent)
                .where(BotEvent.webhook_id == webhook_id, BotEvent.owner_id == owner_id)
                .order_by(BotEvent.id.desc())
                .limit(3)
            )).all()

    events = asyncio.run(read_events())
    assert {event.event_type for event in events} == {"pix_paid", "pix_generated", "lead_initiated"}
    paid = next(event for event in events if event.event_type == "pix_paid")
    assert paid.value == 97
    assert paid.customer_name == "João Silva"
    assert paid.transaction_id == "payment_id_123"
    assert paid.plan_name == "Plano Premium"
    assert all(event.source_event_key for event in events)


def test_sharkbot_accepts_new_events_from_same_webhook_without_transaction_id():
    webhook_url, owner_id = _webhook_url_and_owner()
    webhook_id = f"same-{secrets.token_hex(5)}"
    payloads = [
        {
            "event": "payment_created",
            "timestamp": 1789754430,
            "webhook_id": webhook_id,
            "data": {"transaction": {"amount": 10}},
        },
        {
            "event": "payment_created",
            "timestamp": 1789754431,
            "webhook_id": webhook_id,
            "data": {"transaction": {"amount": 20}},
        },
    ]
    with TestClient(app) as client:
        responses = [client.post(webhook_url, json=payload) for payload in payloads]
    assert [response.status_code for response in responses] == [200, 200]

    async def read_events():
        async with SessionLocal() as db:
            return (await db.scalars(
                select(BotEvent)
                .where(BotEvent.webhook_id == webhook_id, BotEvent.owner_id == owner_id)
                .order_by(BotEvent.id)
            )).all()

    events = asyncio.run(read_events())
    assert [event.value for event in events] == [10, 20]
