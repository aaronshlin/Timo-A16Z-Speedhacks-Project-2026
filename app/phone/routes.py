"""Dial SMS webhook and the Timo inbox."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.brain.profile import upsert_profile
from app.phone.dial import (
    append_local,
    clear_messages,
    entries_since,
    mark_timo_delivered,
    remember_user_message,
    timo_e164,
    user_e164,
    user_says_now,
)
from app.phone.sms_handler import canonical_user, handle_dial_message, handle_reply, reset_memory

log = logging.getLogger(__name__)
router = APIRouter()

_seen_events: set[str] = set()
_logged_payload = False
_INBOX = Path(__file__).resolve().parent / "static" / "inbox.html"
_RECIPIENT_KEYS = ("to", "numberId", "phoneNumberId")


class Inbound(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_number: str = Field(alias="from")
    body: str


class SendIn(BaseModel):
    body: str


def _ids(kind: str) -> set[str]:
    if kind == "timo":
        return {value for value in (timo_e164(), settings.DIAL_FROM_NUMBER_ID) if value}
    return {value for value in (user_e164(), settings.USER_NUMBER_ID) if value}


def _who(value: str | None) -> str | None:
    if not value:
        return None
    if value in _ids("timo"):
        return "timo"
    if value in _ids("user"):
        return "user"
    return None


def _recipient(data: dict) -> str:
    for key in _RECIPIENT_KEYS:
        value = data.get(key)
        if value:
            return str(value)
    return ""


def _verify(raw: bytes, header: str | None) -> bool:
    secret = settings.DIAL_SMS_WEBHOOK_SECRET or settings.DIAL_WEBHOOK_SECRET
    if not secret:
        return settings.USE_MOCKS and settings.MOCK_DIAL
    if not header:
        return False
    parts: dict[str, str] = {}
    for piece in header.split(","):
        if "=" not in piece:
            return False
        key, value = piece.split("=", 1)
        parts[key.strip()] = value.strip()
    stamp = parts.get("t")
    given = parts.get("v1")
    if not stamp or not given:
        return False
    try:
        age = abs(time.time() - float(stamp))
    except ValueError:
        return False
    if age > 300:
        return False
    signed = f"{stamp}.".encode() + raw
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if len(expected) != len(given):
        return False
    return hmac.compare_digest(expected, given)


def _log_first_payload(payload: dict) -> None:
    global _logged_payload
    if _logged_payload:
        return
    _logged_payload = True
    log.info("dial inbound event id=%s type=%s", payload.get("id"), payload.get("type"))


@router.post("/sms/inbound")
async def inbound(request: Request, background: BackgroundTasks):
    raw = await request.body()
    event_type = request.headers.get("x-dial-event-type")
    event_id = request.headers.get("x-dial-event-id")
    signature = request.headers.get("x-dial-signature")
    if event_type is None and event_id is None and signature is None:
        try:
            payload = Inbound.model_validate_json(raw)
        except Exception:
            return JSONResponse({"reply": "bad request"}, status_code=400)
        if not settings.USE_MOCKS:
            expected = 'Bearer ' + settings.CALL_TRIGGER_TOKEN
            if not settings.CALL_TRIGGER_TOKEN or not hmac.compare_digest(request.headers.get('authorization', ''), expected):
                return JSONResponse({'reply': 'Private operator credential required'}, status_code=401)
        event = {'id': 'local:' + uuid.uuid4().hex, 'type': 'message.received',
                 'data': {'from': payload.from_number, 'to': settings.DIAL_NUMBER or settings.TIMO_NUMBER, 'body': payload.body}}
        reply = await asyncio.to_thread(handle_dial_message, event, return_reply=True)
        return {'reply': reply}

    if not _verify(raw, signature):
        return JSONResponse({"ok": False}, status_code=401)
    if event_type != "message.received":
        return {"ok": True}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"ok": False}, status_code=400)
    if (not isinstance(payload, dict) or not event_id
            or payload.get("id") != event_id or payload.get("type") != event_type
            or not isinstance(payload.get("data"), dict)):
        return JSONResponse({"ok": False}, status_code=400)
    _log_first_payload(payload)
    background.add_task(handle_dial_message, payload)
    return {'ok': True}


@router.get("/inbox")
def inbox() -> FileResponse:
    return FileResponse(_INBOX)


@router.get("/inbox/thread")
def inbox_thread(since: float = 0) -> dict:
    return {"entries": entries_since(since)}


@router.post("/inbox/send")
def inbox_send(payload: SendIn) -> dict:
    return user_says_now(payload.body)


def _clear_fired_gaps() -> None:
    import app.brain.state as state

    for name in ("clear_fired_gaps", "clear_gaps"):
        if hasattr(state, name) and callable(getattr(state, name)):
            getattr(state, name)()
            return
    with state._conn() as conn:
        conn.execute("DELETE FROM fired_gaps")


def _clear_timo_blocks() -> None:
    import app.context.calendar as calendar

    for name in ("delete_todays_timo_blocks", "clear_timo_blocks", "delete_timo_blocks"):
        if hasattr(calendar, name) and callable(getattr(calendar, name)):
            getattr(calendar, name)()
            return
    log.info("calendar has no function to delete today's Timo blocks")


@router.post("/inbox/reset")
def inbox_reset() -> dict:
    clear_messages()
    reset_memory()
    _seen_events.clear()
    _clear_fired_gaps()
    _clear_timo_blocks()
    return {"ok": True}


_ONBOARDING = (
    ("timo", "Hi, I'm Timo. I turn the gaps in your day into one thing worth doing. What's your name?"),
    ("user", "Aaron"),
    ("timo", "Nice to meet you, Aaron. Which classes are you in?"),
    ("user", "CS 61B, Physics 7A, Math 54"),
    ("timo", "When a gap opens, should I text you or call you?"),
    ("user", "Text me"),
    ("timo", "You're set. Your calendar is connected. I'll message you when your next gap starts."),
)


async def _play_onboarding() -> None:
    for index, (direction, body) in enumerate(_ONBOARDING):
        if index:
            await asyncio.sleep(0.9)
        append_local(direction, body)


@router.post("/inbox/demo-onboarding")
def demo_onboarding(background: BackgroundTasks) -> dict:
    inbox_reset()
    try:
        upsert_profile(
            canonical_user(user_e164()),
            name="Aaron",
            courses=["CS 61B", "Physics 7A", "Math 54"],
            channel="sms",
        )
    except Exception:
        log.exception("demo onboarding profile update failed")
    background.add_task(_play_onboarding)
    return {"ok": True}
