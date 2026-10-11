"""Dial SMS over REST. MOCK_DIAL=1 never touches the network.

Send path confirmed in the Dial docs (Send an SMS):
POST https://api.getdial.ai/v1/messages
JSON fields to, fromNumberId, body. Bearer auth. Sending is not idempotent,
so a failed send is never retried.

send_sms_now(to, body) is the brain's sync path: real only with USE_MOCKS=0 and SMS_ENABLED=1,
otherwise it lands in the outbox. Every text is recorded in the outbox either way.
"""

import asyncio
import logging
import threading
import time
import uuid

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Docs curl is https://api.getdial.ai/v1/messages. Prose also mentions /api/v1.
DIAL_BASE = "https://api.getdial.ai/v1"
# The numbers API (and the call engine) use /api/v1.
API = "https://api.getdial.ai/api/v1"

outbox: list[str] = []
thread: list[dict] = []

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_client: httpx.AsyncClient | None = None
_last_ts = 0.0


def user_e164() -> str:
    return settings.USER_NUMBER or settings.DEMO_PHONE


def timo_e164() -> str:
    return settings.TIMO_NUMBER


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is not None:
        return _loop
    with _lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="dial", daemon=True).start()
            _loop = loop
        return _loop


def _run(coro):
    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=8)


async def get_async_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=DIAL_BASE,
            timeout=5.0,
            headers={"Authorization": f"Bearer {settings.DIAL_API_KEY}"},
        )
    return _client


def get_client():
    if settings.MOCK_DIAL:
        return None
    return _client


def _stamp() -> float:
    global _last_ts
    ts = max(time.time(), _last_ts + 0.001)
    _last_ts = ts
    return ts


def _append(direction: str, body: str, status: str) -> dict:
    entry = {
        "id": uuid.uuid4().hex,
        "direction": direction,
        "body": body,
        "ts": _stamp(),
        "status": status,
    }
    with _lock:
        thread.append(entry)
    return entry


def remember_user_message(body: str) -> dict | None:
    """Append a user bubble unless this body was just recorded (webhook echo)."""
    now_ts = time.time()
    with _lock:
        if thread:
            last = thread[-1]
            if (
                last["direction"] == "user"
                and last["body"] == body
                and now_ts - last["ts"] < 15
            ):
                return None
    return _append("user", body, "delivered")


def mark_timo_delivered(body: str) -> dict:
    with _lock:
        for entry in reversed(thread):
            if entry["direction"] == "timo" and entry["body"] == body and entry["status"] != "delivered":
                entry["status"] = "delivered"
                return entry
    return _append("timo", body, "delivered")


def entries_since(since: float) -> list[dict]:
    with _lock:
        return [dict(entry) for entry in thread if entry["ts"] > since]


def append_local(direction: str, body: str) -> dict:
    """Add a bubble without sending it or running a reply."""
    return _append(direction, body, "sent")


def clear_messages() -> None:
    global _last_ts
    with _lock:
        thread.clear()
        outbox.clear()
        _last_ts = 0.0


def api(method: str, path: str, body: dict | None = None) -> dict | None:
    """One Dial REST call. None on any failure: logged, never raised."""
    try:
        response = httpx.request(
            method,
            API + path,
            json=body,
            headers={"Authorization": f"Bearer {settings.DIAL_API_KEY}"},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("dial %s %s failed: %s", method, path, type(exc).__name__)
        return None


def send_sms_now(to: str, body: str, from_number_id: str | None = None) -> bool:
    """Text `to` from Timo's number, or from `from_number_id` (the test user's number)."""
    line = f"SMS {to}: {body}"
    with _lock:
        outbox.append(line)
    print(line)
    if settings.USE_MOCKS or not settings.SMS_ENABLED:
        return True
    sent = api(
        "POST",
        "/messages",
        {"to": to, "fromNumberId": from_number_id or settings.DIAL_FROM_NUMBER_ID, "body": body},
    )
    return sent is not None


async def send_sms(from_number_id: str, to: str, body: str) -> bool:
    if settings.MOCK_DIAL:
        return True
    try:
        client = await get_async_client()
        response = await client.post(
            "/messages",
            json={"to": to, "fromNumberId": from_number_id, "body": body},
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        log.warning("send_sms failed %s status=%s", type(exc).__name__, status)
        return False


async def timo_says(body: str) -> dict:
    entry = _append("timo", body, "pending")
    ok = await send_sms(settings.DIAL_FROM_NUMBER_ID, user_e164(), body)
    entry["status"] = "sent" if ok else "failed"
    line = f"SMS {user_e164()}: {body}"
    with _lock:
        outbox.append(line)
    print(line)
    return entry


async def user_says(body: str) -> dict:
    entry = _append("user", body, "pending")
    if settings.MOCK_DIAL:
        entry["status"] = "sent"
        threading.Thread(
            target=_deliver_user_reply,
            args=(body,),
            name="dial-mock-reply",
            daemon=True,
        ).start()
        return entry
    ok = await send_sms(settings.USER_NUMBER_ID, timo_e164(), body)
    entry["status"] = "sent" if ok else "failed"
    return entry


def _deliver_user_reply(body: str) -> None:
    from app.phone.sms_handler import handle_reply

    handle_reply(user_e164(), body)


def timo_says_now(body: str) -> dict:
    return _run(timo_says(body))


def user_says_now(body: str) -> dict:
    return _run(user_says(body))
