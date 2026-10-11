import asyncio
import hmac
import json
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent.poller import run_tick
from app.agent.calling import service, verify_signature
from app.agent.calendar_trigger import CalendarGap, trigger_calendar_gap
from app.brain.profile import get_profile
from app.brain.recommend import recommend
from app import clock
from app.config import settings
from app.context.router import dispatch
from app.models import FreeWindow, TriggerEvent, TriggerType
from app.phone.dial import outbox
from app.phone.sms_handler import handle_dial_message

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


class DemoTrigger(BaseModel):
    type: TriggerType
    payload: dict = Field(default_factory=dict)


@router.post("/demo/trigger")
def demo_trigger(body: DemoTrigger) -> dict:
    """Send one trigger for the demo user through the router, as the scheduler would."""
    dispatch(TriggerEvent(user_id=settings.DEMO_PHONE, type=body.type, payload=body.payload, ts=clock.now()))
    return {"dispatched": body.type}


class VoiceTrigger(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    available_minutes: int = Field(default=10, ge=10, le=120)


@router.post("/voice/trigger")
def voice_trigger(body: VoiceTrigger):
    calls = service()
    result = calls.submit(settings.DEMO_PHONE, None, "manual:" + body.event_id, "manual:" + body.event_id)
    return JSONResponse(result, 202 if result["disposition"] == "accepted" else 200)


@router.get("/voice/sessions")
def voice_sessions():
    return JSONResponse(service().sessions(), headers={"Cache-Control": "no-store"})


@router.get("/voice/preview")
def voice_preview():
    calls = service()
    item = calls.preview_next()
    return {"next_session": item, "places_call": False, "context_storage": "local_only"}


@router.post("/voice/calendar-gap")
def calendar_gap(body: CalendarGap, request: Request):
    token = settings.CALENDAR_TRIGGER_TOKEN
    if not token or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
        raise HTTPException(401, "A private calendar trigger token is required")
    status, result = trigger_calendar_gap(body)
    return JSONResponse(result, status_code=status)


@router.get("/voice/readiness")
def voice_readiness(request: Request):
    config = service().config
    from app.context.oauth import token_path
    return {"real_calendar_reads": not settings.USE_MOCKS, "google_token_present": token_path().is_file(),
            "live_calls_enabled": config.LIVE_CALLS_ENABLED, "call_opt_in": config.CALL_OPT_IN, "signed_callback_ready": service().callbacks_ready,
            "dial_credentials_present": bool(config.DIAL_API_KEY), "enrolled_phone": config.DEMO_PHONE,
            "main_tools_ready": bool(service().engine and service().engine.env.get("MAIN_TOOLS_ENABLED") == "true"),
            "dial_from_number_id": config.DIAL_FROM_NUMBER_ID, "dial_number_e164": config.DIAL_NUMBER or None,
            "calendar_trigger_token_present": bool(settings.CALENDAR_TRIGGER_TOKEN),
            "calendar_trigger_path": "/voice/calendar-gap", "poller_enabled": bool(getattr(request.app.state, "calendar_poller", None)),
            "session_limit": config.CALL_SESSION_LIMIT, "duration_cap_seconds": 180, "max_actions_per_call": 3,
            "voice_tools_ready": bool(service().engine and service().engine.env.get("VOICE_TOOLS_ENABLED") == "true"),
            "calendar_writes_enabled": False, "sms_enabled": settings.SMS_ENABLED and not settings.USE_MOCKS and not settings.MOCK_DIAL, "external_execution": "disabled",
            "demo_calendar_calls_allowed": config.ALLOW_DEMO_CALENDAR_CALLS, "calendar_source": "mock_calendar" if settings.USE_MOCKS else "google_calendar",
            "receipt_mode": "dashboard", "task_context": ["planner", "pending_actions", "task_fixtures"], "context_storage": "local_only"}



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
    if session.get('fixtureId'):
        raise HTTPException(403, "Fixture results require a signed callback and verified task-specific transcript confirmation")
    if not body.confirmed:
        raise HTTPException(400, "Explicit demo confirmation is required")
    calls.record(session, body.choice)
    return {"disposition": "recorded", "session": calls.get_session(body.session_id)}


@router.post("/webhooks/dial")
@router.post("/voice/webhooks/dial")
async def dial_webhook(request: Request, background: BackgroundTasks):
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
                or event.get("type") not in ("call.ended", "call.transcribed", "message.received", "webhook.ping")
                or not isinstance(event.get("data", {}), dict)
                # Dial readiness pings have no resource: relatedObject is null.
                or (not isinstance(event.get("relatedObject", {}), dict)
                    and not (event.get("type") == "webhook.ping"
                             and event.get("relatedObject") is None))):
            raise ValueError("Invalid envelope")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid event envelope")
    if event["type"] == "message.received":
        # A text to Timo or to the test user. Answered after the 2xx so Dial never waits on a run.
        background.add_task(handle_dial_message, event)
        return {"accepted": True}
    # Database writes run off the event loop so call-placement I/O cannot delay a callback.
    duplicate = calls.accept(event)
    return {"accepted": True, "duplicate": duplicate}
