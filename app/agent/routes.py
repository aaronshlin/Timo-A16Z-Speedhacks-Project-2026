import asyncio
import hmac
import json
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent.poller import run_tick
from app.agent.calling import service, verify_signature
from app.agent.calendar_trigger import CalendarGap, trigger_calendar_gap
from app.brain.profile import get_profile
from app.brain.recommend import recommend
from app import clock
from app.config import settings
from app.models import FreeWindow
from app.phone.dial import outbox

router = APIRouter()


class TickIn(BaseModel):
    now: str | None = None


@router.post("/demo/tick")
def demo_tick(body: TickIn | None = Body(default=None)) -> dict:
    when = body.now if body is not None else None
    rec = run_tick(when)
    if rec is None:
        return {"recommendation": None}
    return rec.model_dump(mode="json")


@router.get("/outbox")
def get_outbox() -> dict:
    return {"outbox": list(outbox)}


class VoiceTrigger(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    available_minutes: int = Field(default=10, ge=10, le=120)


@router.post("/voice/trigger")
def voice_trigger(body: VoiceTrigger):
    calls = service()
    current = clock.now()
    window = FreeWindow(start=current, end=current + timedelta(minutes=body.available_minutes), minutes=body.available_minutes)
    try:
        rec = recommend(window, "audio", get_profile(settings.DEMO_PHONE))
    except RuntimeError:
        return JSONResponse({"disposition": "suppressed", "reason": "insufficient_context"}, 200)
    result = calls.submit(settings.DEMO_PHONE, rec, "manual:" + body.event_id, "manual:" + body.event_id)
    return JSONResponse(result, 202 if result["disposition"] == "accepted" else 200)


@router.get("/voice/sessions")
def voice_sessions():
    return JSONResponse(service().sessions(), headers={"Cache-Control": "no-store"})


@router.post("/voice/calendar-gap")
def calendar_gap(body: CalendarGap, request: Request):
    token = settings.CALENDAR_TRIGGER_TOKEN
    if not token or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
        raise HTTPException(401, "A private calendar trigger token is required")
    status, result = trigger_calendar_gap(body)
    return JSONResponse(result, status_code=status)


@router.get("/voice/readiness")
def voice_readiness():
    config = service().config
    from app.context.oauth import token_path
    return {"real_calendar_reads": not settings.USE_MOCKS, "google_token_present": token_path().is_file(),
            "live_calls_enabled": config.LIVE_CALLS_ENABLED, "call_opt_in": config.CALL_OPT_IN, "signed_callback_ready": service().callbacks_ready,
            "dial_credentials_present": bool(config.DIAL_API_KEY),
            "dial_from_number_id": config.DIAL_FROM_NUMBER_ID, "dial_number_e164": config.DIAL_NUMBER or None,
            "calendar_trigger_token_present": bool(settings.CALENDAR_TRIGGER_TOKEN),
            "calendar_trigger_path": "/voice/calendar-gap", "poller_enabled": settings.POLLER_ENABLED,
            "session_limit": config.CALL_SESSION_LIMIT, "duration_cap_seconds": 180,
            "calendar_writes_enabled": False, "sms_enabled": False, "external_execution": "disabled",
            "receipt_mode": "dashboard", "task_context": "mock_data"}



class MockResult(BaseModel):
    session_id: str
    choice: Literal["go", "next", "hold"]
    confirmed: bool


@router.post("/voice/demo/result")
def mock_result(body: MockResult):
    calls = service()
    if not settings.USE_MOCKS or calls.config.LIVE_CALLS_ENABLED:
        raise HTTPException(403, "Mock results are disabled during live calling")
    session = calls.get_session(body.session_id)
    if not session or session["mode"] != "dry_run":
        raise HTTPException(404, "Dry-run session not found")
    if session["status"] == "resolved":
        if session["decision"] != body.choice:
            raise HTTPException(409, "This demo decision is already recorded")
        return {"disposition": "duplicate", "session": session}
    if not body.confirmed:
        raise HTTPException(400, "Explicit demo confirmation is required")
    calls.record(session, body.choice)
    return {"disposition": "recorded", "session": calls.get_session(body.session_id)}


@router.post("/webhooks/dial")
@router.post("/voice/webhooks/dial")
async def dial_webhook(request: Request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 256 * 1024:
            raise HTTPException(413, "Body too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    calls = service()
    if not verify_signature(calls.config.DIAL_WEBHOOK_SECRET, request.headers.get("x-dial-signature", ""), raw):
        raise HTTPException(401, "Invalid signature")
    try:
        event = json.loads(raw)
        if (not isinstance(event, dict) or not request.headers.get("x-dial-event-id")
                or request.headers.get("x-dial-event-id") != event.get("id")
                or event.get("type") not in ("call.ended", "call.transcribed", "webhook.ping")
                or not isinstance(event.get("data", {}), dict)
                or not isinstance(event.get("relatedObject", {}), dict)):
            raise ValueError("Invalid envelope")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid event envelope")
    # Database writes run off the event loop so call-placement I/O cannot delay a callback.
    duplicate = calls.accept(event)
    return {"accepted": True, "duplicate": duplicate}
