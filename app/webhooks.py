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
    sender_id = event.get("sender", {}).get("id")
    settings = get_settings()
    token = decrypt_token(account.access_token_encrypted)
    comment_id = event.get("comment_id")
    is_comment = bool(comment_id or event.get("from"))
    reply_enabled = (
        account.comment_reply_enabled if is_comment else account.direct_reply_enabled
    )
    reply_text = account.comment_reply_text if is_comment else account.direct_reply_text
    # Legacy accounts continue using the original single-message setting.
    if not reply_text and account.auto_reply_enabled:
        reply_enabled, reply_text = True, account.auto_reply_text
    if not reply_enabled or not reply_text:
        return
    if not comment_id and is_comment:
        comment_id = event.get("id")
    async with httpx.AsyncClient(timeout=20) as client:
        if comment_id:
            url = f"https://graph.instagram.com/{settings.graph_api_version}/{comment_id}/replies"
            response = await client.post(
                url, params={"access_token": token}, json={"message": reply_text}
            )
        elif sender_id:
            url = f"https://graph.instagram.com/{settings.graph_api_version}/{account.instagram_user_id}/messages"
            response = await client.post(
                url,
                params={"access_token": token},
                json={"recipient": {"id": sender_id}, "message": {"text": reply_text}},
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
