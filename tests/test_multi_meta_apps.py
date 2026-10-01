from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import InstagramAccount, MetaApp, User
from app.oauth import authorization_url, new_oauth_state, read_oauth_state
from app.routes import _selected_meta_app
from app.security import encrypt_token
from cryptography.fernet import Fernet


def test_oauth_state_keeps_selected_meta_app_id():
    state = new_oauth_state(9, meta_app_id=27, nonce="multi-app-nonce")
    assert read_oauth_state(state) == {
        "user_id": 9,
        "reconnect_account_id": None,
        "nonce": "multi-app-nonce",
        "meta_app_id": 27,
    }


def test_authorization_url_accepts_custom_client_id(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "legacy-app")
    from app.config import get_settings
    get_settings.cache_clear()
    query = parse_qs(urlparse(authorization_url("state", client_id="custom-app")).query)
    assert query["client_id"] == ["custom-app"]


@pytest.mark.asyncio
async def test_meta_apps_are_scoped_and_default_selection_uses_owner(monkeypatch):
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        owner_a = User(email="meta-a@example.com", password_hash="hash")
        owner_b = User(email="meta-b@example.com", password_hash="hash")
        db.add_all([owner_a, owner_b])
        await db.flush()
        app_a = MetaApp(
            owner_id=owner_a.id,
            name="App A",
            app_id="app-a",
            app_secret_encrypted=encrypt_token("secret-a"),
            is_default=True,
        )
        app_b = MetaApp(
            owner_id=owner_b.id,
            name="App B",
            app_id="app-b",
            app_secret_encrypted=encrypt_token("secret-b"),
            is_default=True,
        )
        db.add_all([app_a, app_b])
        await db.flush()
        account = InstagramAccount(
            owner_id=owner_a.id,
            meta_app_id=app_a.id,
            instagram_user_id="ig-a",
            username="account-a",
            access_token_encrypted="encrypted-token",
        )
        db.add(account)
        await db.commit()
        selected, app_id, secret = await _selected_meta_app(db, owner_a.id)
        assert selected.id == app_a.id
        assert app_id == "app-a"
        assert secret == "secret-a"
        owner_apps = (await db.scalars(select(MetaApp).where(MetaApp.owner_id == owner_a.id))).all()
        assert [item.app_id for item in owner_apps] == ["app-a"]
    await engine.dispose()
