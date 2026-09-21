import logging
import asyncio
import io
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from supabase import create_client
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import AutomationRule, BotEvent, InstagramAccount
from .security import decrypt_token
from .utils import parse_spintax

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


def _event_transaction(value: dict) -> dict:
    transaction = value.get("transaction")
    return transaction if isinstance(transaction, dict) else {}


def _event_amount(value: dict, transaction: dict) -> float:
    raw_amount = value.get(
        "value",
        value.get("amount", value.get("price", transaction.get("amount", 0))),
    )
    try:
        return float(raw_amount or 0)
    except (TypeError, ValueError):
        logger.warning("Valor inválido recebido pelo Sharkbot: %r", raw_amount)
        return 0.0


def _event_timestamp(value: dict) -> datetime:
    raw_timestamp = value.get("timestamp")
    if isinstance(raw_timestamp, (int, float)):
        return datetime.fromtimestamp(raw_timestamp, tz=timezone.utc)
    if isinstance(raw_timestamp, str):
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Timestamp inválido recebido pelo Sharkbot: %s", raw_timestamp)
    return datetime.now(timezone.utc)


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


async def _resolve_automation_media(rule: AutomationRule) -> str | None:
    if rule.media_url:
        return rule.media_url
    if not rule.drive_media_url or not rule.drive_credentials_encrypted:
        return rule.drive_media_url
    file_id = parse_qs(urlparse(rule.drive_media_url).query).get("id", [None])[0]
    if not file_id:
        return None
    settings = get_settings()
    storage_key = settings.supabase_service_role or settings.supabase_key
    if not settings.supabase_url or not storage_key:
        raise RuntimeError("Supabase Storage não configurado para resgatar a mídia da automação")
    credentials = Credentials.from_authorized_user_info(
        json.loads(decrypt_token(rule.drive_credentials_encrypted)),
        ["https://www.googleapis.com/auth/drive.readonly"],
    )
    def download_and_upload() -> str:
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        metadata = service.files().get(fileId=file_id, fields="name,mimeType").execute()
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, service.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        path = f"automation-rescue/{rule.id}/{metadata.get('name', f'media-{rule.id}')}"
        bucket = create_client(settings.supabase_url, storage_key).storage.from_(
            settings.supabase_storage_bucket
        )
        bucket.upload(path, buffer.getvalue(), file_options={
            "content-type": metadata.get("mimeType", "application/octet-stream"),
            "upsert": True,
        })
        return bucket.get_public_url(path)
    return await asyncio.to_thread(download_and_upload)


def _incoming_text(event: dict) -> str:
    candidates = [
        event.get("text"),
        event.get("message", {}).get("text") if isinstance(event.get("message"), dict) else None,
        event.get("comment", {}).get("text") if isinstance(event.get("comment"), dict) else None,
        event.get("message") if isinstance(event.get("message"), str) else None,
    ]
    return next((str(value).strip() for value in candidates if value), "")


def _rule_targets_account(rule: AutomationRule, account_id: int) -> bool:
    try:
        targets = json.loads(rule.target_account_ids or "[]")
    except (TypeError, json.JSONDecodeError):
        targets = [rule.account_id] if rule.account_id else []
    return not targets or account_id in {int(value) for value in targets}


def _keyword_matches(rule: AutomationRule, text: str) -> bool:
    keywords = [item.strip().casefold() for item in (rule.trigger_keywords or "").split(",") if item.strip()]
    return bool(keywords) and any(keyword in text.casefold() for keyword in keywords)


def _is_comment_event(event: dict, comment_id: str | None) -> bool:
    if comment_id or event.get("field") == "comments":
        return True
    change = event.get("value")
    return isinstance(change, dict) and change.get("field") == "comments"


async def _send_auto_reply(account: InstagramAccount, event: dict) -> None:
    sender_id = (event.get("sender") or {}).get("id") or (event.get("from") or {}).get("id")
    settings = get_settings()
    token = decrypt_token(account.access_token_encrypted)
    comment_id = event.get("comment_id") or event.get("comment", {}).get("id")
    is_comment = _is_comment_event(event, comment_id)
    rule_type = "comment_reply" if is_comment else "dm_reply"
    async with SessionLocal() as db:
        rules = (await db.scalars(select(AutomationRule).where(
            AutomationRule.owner_id == account.owner_id,
            AutomationRule.rule_type == rule_type,
            AutomationRule.is_active.is_(True),
        ).order_by(AutomationRule.account_id.is_(None), AutomationRule.created_at.desc()))).all()
        incoming_text = _incoming_text(event)
        rule = next((
            candidate for candidate in rules
            if _rule_targets_account(candidate, account.id)
            and (not is_comment or _keyword_matches(candidate, incoming_text))
        ), None)
    reply_text = rule.message_text if rule else (
        account.comment_reply_text if is_comment else account.direct_reply_text
    )
    dm_followup_text = rule.dm_followup_text if rule else ""
    reply_enabled = bool(rule or (
        account.comment_reply_enabled if is_comment else account.direct_reply_enabled
    ))
    if not reply_text and account.auto_reply_enabled:
        reply_enabled, reply_text = True, account.auto_reply_text
    if not reply_enabled or (not reply_text and not rule):
        return
    reply_text = parse_spintax(reply_text)
    if not comment_id and is_comment:
        comment_id = event.get("id")
    media_url = await _resolve_automation_media(rule) if rule else None
    async with httpx.AsyncClient(timeout=20) as client:
        if comment_id:
            url = f"https://graph.instagram.com/{settings.graph_api_version}/{comment_id}/replies"
            if media_url:
                media_response = await client.post(
                    url, params={"access_token": token},
                    json={"message": {"attachment": {"type": "image", "payload": {"url": media_url}}}},
                )
                media_response.raise_for_status()
            if reply_text:
                response = await client.post(
                    url,
                    params={"access_token": token, "message": reply_text},
                )
                response.raise_for_status()
            if dm_followup_text:
                private_url = f"https://graph.instagram.com/{settings.graph_api_version}/{comment_id}/private_replies"
                try:
                    private_response = await client.post(
                        private_url,
                        params={
                            "access_token": token,
                            "message": parse_spintax(dm_followup_text),
                        },
                    )
                    private_response.raise_for_status()
                except httpx.HTTPError:
                    logger.warning(
                        "Meta recusou private reply para comentário %s; resposta pública foi mantida",
                        comment_id,
                        exc_info=True,
                    )
            return
        elif sender_id:
            url = f"https://graph.instagram.com/{settings.graph_api_version}/{account.instagram_user_id}/messages"
            if media_url:
                media_response = await client.post(
                    url, params={"access_token": token},
                    json={"recipient": {"id": sender_id}, "message": {
                        "attachment": {"type": "image", "payload": {"url": media_url}}
                    }},
                )
                media_response.raise_for_status()
            if reply_text:
                response = await client.post(
                    url, params={"access_token": token},
                    json={"recipient": {"id": sender_id}, "message": {"text": reply_text}},
                )
                response.raise_for_status()
        else:
            return


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
                event.get("event")
                or event.get("event_type")
                or event.get("event_name")
                or value.get("event_type")
                or value.get("event_name")
                or value.get("type")
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
                transaction = _event_transaction(value)
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
                event_value = _event_amount(value, transaction)
                timestamp = _event_timestamp(event)
                webhook_id = str(event.get("webhook_id")) if event.get("webhook_id") else None
                transaction_id = str(
                    transaction.get("id") or transaction.get("external_id")
                ) if transaction.get("id") or transaction.get("external_id") else None
                duplicate_query = select(BotEvent).where(
                    BotEvent.webhook_id == webhook_id,
                    BotEvent.event_type == event_type,
                )
                if transaction_id:
                    duplicate_query = duplicate_query.where(
                        BotEvent.transaction_id == transaction_id
                    )
                else:
                    duplicate_query = duplicate_query.where(
                        BotEvent.timestamp == timestamp,
                        BotEvent.value == event_value,
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
