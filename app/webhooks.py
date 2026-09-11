from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
import httpx
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import InstagramAccount
from .security import decrypt_token

router = APIRouter(prefix="/webhook")


@router.get("")
async def verify_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
):
    settings = get_settings()
    if mode == "subscribe" and token == settings.webhook_verify_token and challenge:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


async def _send_auto_reply(account: InstagramAccount, event: dict) -> None:
    if not account.auto_reply_enabled or not account.auto_reply_text:
        return
    sender_id = event.get("sender", {}).get("id")
    settings = get_settings()
    token = decrypt_token(account.access_token_encrypted)
    comment_id = event.get("comment_id") or event.get("id") if event.get("from") else None
    async with httpx.AsyncClient(timeout=20) as client:
        if comment_id:
            url = f"https://graph.instagram.com/{settings.graph_api_version}/{comment_id}/replies"
            response = await client.post(
                url, params={"access_token": token}, json={"message": account.auto_reply_text}
            )
        elif sender_id:
            url = f"https://graph.instagram.com/{settings.graph_api_version}/{account.instagram_user_id}/messages"
            response = await client.post(
                url,
                params={"access_token": token},
                json={"recipient": {"id": sender_id}, "message": {"text": account.auto_reply_text}},
            )
        else:
            return
        response.raise_for_status()


@router.post("")
async def receive_webhook(request: Request):
    payload = await request.json()
    events = []
    for entry in payload.get("entry", []):
        events.extend(entry.get("messaging", []))
        events.extend(
            {"entry_id": entry.get("id"), **change.get("value", change)}
            for change in entry.get("changes", [])
        )
    async with SessionLocal() as db:
        for event in events:
            value = event.get("value", event)
            account_id = (
                event.get("entry_id")
                or value.get("instagram_user_id")
                or value.get("recipient", {}).get("id")
            )
            if account_id:
                account = await db.scalar(
                    select(InstagramAccount).where(
                        InstagramAccount.instagram_user_id == str(account_id)
                    )
                )
                if account:
                    try:
                        await _send_auto_reply(account, value)
                    except Exception:
                        # A webhook must acknowledge quickly; delivery can be retried by Meta.
                        pass
    return {"received": True}
