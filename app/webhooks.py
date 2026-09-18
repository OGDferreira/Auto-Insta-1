import logging
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
import httpx
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import BotEvent, InstagramAccount
from .security import decrypt_token

router = APIRouter(prefix="/webhook")
logger = logging.getLogger(__name__)

EVENT_TYPE_ALIASES = {
    "lead": "lead_initiated",
    "novo_lead": "lead_initiated",
    "new_lead": "lead_initiated",
    "lead_iniciado": "lead_initiated",
    "user_joined": "lead_initiated",
    "user_join": "lead_initiated",
    "clique": "link_click",
    "link_click": "link_click",
    "click": "link_click",
    "link_clicked": "link_click",
    "pagamento_criado": "pix_generated",
    "payment_created": "pix_generated",
    "pix_created": "pix_generated",
    "pix_gerado": "pix_generated",
    "pix_gerado_com_sucesso": "pix_generated",
    "pix": "pix_generated",
    "pagamento_aprovado": "pix_paid",
    "payment_approved": "pix_paid",
    "payment_paid": "pix_paid",
    "paid": "pix_paid",
    "pix_pago": "pix_paid",
    "pix_pendente": "pix_pending",
    "payment_pending": "pix_pending",
}


def normalize_event_type(value: object) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("-", "_").replace(" ", "_")
    return EVENT_TYPE_ALIASES.get(normalized, normalized)


def _webhook_events(payload: dict) -> list[dict]:
    """Accept Sharkbot's flat events as well as nested `data`/`payload` bodies."""
    if isinstance(payload.get("data"), dict):
        data = payload["data"]
        return [{
            **payload,
            **data,
            "data": data,
            "event": (
                payload.get("event")
                or payload.get("event_type")
                or payload.get("event_name")
                or data.get("event")
                or data.get("event_type")
            ),
        }]
    if isinstance(payload.get("payload"), dict):
        return [{**payload, **payload["payload"]}]
    if any(payload.get(key) for key in ("event_type", "event_name", "type", "event", "name")):
        return [payload]
    events = []
    for entry in payload.get("entry", []):
        events.extend({"entry_id": entry.get("id"), **event} for event in entry.get("messaging", []))
        events.extend(
            {"entry_id": entry.get("id"), **change.get("value", change)}
            for change in entry.get("changes", [])
        )
    return events


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
    sender_id = (event.get("sender") or {}).get("id") or (event.get("from") or {}).get("id")
    settings = get_settings()
    token = decrypt_token(account.access_token_encrypted)
    comment_id = event.get("comment_id")
    is_comment = bool(comment_id or (event.get("from") and event.get("text")))
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
                url, params={"access_token": token, "message": reply_text}
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


async def _delayed_auto_reply(account_id: int, event: dict) -> None:
    await asyncio.sleep(30)
    async with SessionLocal() as db:
        account = await db.get(InstagramAccount, account_id)
        if account:
            try:
                await _send_auto_reply(account, event)
            except Exception:
                logger.exception(
                    "Falha ao enviar automação atrasada para conta Instagram %s",
                    account.instagram_user_id,
                )


@router.post("")
@router.post("/sharkbot")
@router.post("/sharkbot/")
async def receive_webhook(request: Request):
    try:
        payload = await request.json()
    except ValueError as exc:
        logger.warning("Payload Sharkbot inválido: %s", exc)
        raise HTTPException(status_code=400, detail="Payload JSON inválido") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload Sharkbot deve ser um objeto JSON")
    events = _webhook_events(payload)
    if not events:
        raise HTTPException(status_code=422, detail="Nenhum evento reconhecível no payload")
    async with SessionLocal() as db:
        for event in events:
            value = event.get("value", event)
            if not isinstance(value, dict):
                value = event
            event_type = normalize_event_type(
                value.get("event_type")
                or value.get("event_name")
                or value.get("type")
                or value.get("event")
                or value.get("name")
            )
            logger.info("Evento Sharkbot recebido: tipo=%s", event_type)
            if event_type in {"link_click", "lead_initiated", "pix_generated", "pix_paid", "pix_pending"}:
                account = None
                account_key = (
                    value.get("account_id")
                    or value.get("instagram_user_id")
                    or event.get("entry_id")
                    or value.get("recipient", {}).get("id")
                )
                if account_key:
                    account = await db.scalar(
                        select(InstagramAccount).where(
                            InstagramAccount.instagram_user_id == str(account_key)
                        )
                    )
                transaction = value.get("transaction")
                if not isinstance(transaction, dict):
                    transaction = {}
                customer = value.get("customer")
                if not isinstance(customer, dict):
                    customer = {}
                bot = value.get("bot")
                if not isinstance(bot, dict):
                    bot = {}
                customer_name = " ".join(
                    str(part).strip()
                    for part in (customer.get("first_name"), customer.get("last_name"))
                    if part
                ) or None
                try:
                    event_value = float(
                        value.get(
                            "value",
                            value.get(
                                "amount",
                                value.get("price", transaction.get("amount", 0)),
                            ),
                        )
                        or 0
                    )
                except (TypeError, ValueError):
                    event_value = 0.0
                raw_timestamp = value.get("timestamp")
                timestamp = datetime.now(timezone.utc)
                if isinstance(raw_timestamp, (int, float)):
                    timestamp = datetime.fromtimestamp(raw_timestamp, tz=timezone.utc)
                elif isinstance(raw_timestamp, str):
                    try:
                        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                    except ValueError:
                        logger.warning("Timestamp inválido recebido pelo Sharkbot: %s", raw_timestamp)
                webhook_id = str(event.get("webhook_id")) if event.get("webhook_id") else None
                transaction_id = str(transaction.get("id")) if transaction.get("id") else None
                duplicate_query = select(BotEvent).where(
                    BotEvent.webhook_id == webhook_id,
                    BotEvent.event_type == event_type,
                )
                if transaction_id:
                    duplicate_query = duplicate_query.where(
                        BotEvent.transaction_id == transaction_id
                    )
                duplicate = await db.scalar(duplicate_query) if webhook_id else None
                if duplicate:
                    logger.info(
                        "Evento Sharkbot duplicado ignorado: webhook_id=%s tipo=%s",
                        webhook_id,
                        event_type,
                    )
                    continue
                db.add(BotEvent(
                    account_id=account.id if account else None,
                    event_type=event_type,
                    value=event_value,
                    webhook_id=webhook_id,
                    customer_name=customer_name,
                    customer_username=str(customer.get("username")) if customer.get("username") else None,
                    bot_name=str(bot.get("name")) if bot.get("name") else None,
                    transaction_id=transaction_id,
                    plan_name=str(transaction.get("plan_name")) if transaction.get("plan_name") else None,
                    timestamp=timestamp,
                ))
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
                    asyncio.create_task(_delayed_auto_reply(account.id, value))
        await db.commit()
    return {"received": True}
