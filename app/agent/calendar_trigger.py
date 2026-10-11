"""Typed receiver shared by HTTP and the in-process calendar poller."""
import hashlib
import json
from datetime import timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field
from app.agent.calling import service, validate_clock
from app import clock
from app.config import settings
from app.brain.profile import get_profile
from app.brain.recommend import recommend
from app.models import FreeWindow


class CalendarGap(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    source: Literal['google_calendar', 'mock_calendar'] = 'google_calendar'
    observed_at: AwareDatetime
    start: AwareDatetime
    end: AwareDatetime
    calendars_checked: list[str] = Field(min_length=1, max_length=50)
    snapshot_complete: bool
    next_event_title: str | None = Field(default=None, max_length=300)


def trigger_calendar_gap(body, current=None):
    calls = service()
    validate_clock(calls.config)
    if current is not None and (calls.config.LIVE_CALLS_ENABLED or not calls.config.USE_MOCKS):
        raise ValueError("Fake calendar time is disabled")
    current = current or clock.now()
    age = (current - body.observed_at).total_seconds()
    reason = None
    if not body.snapshot_complete or any(not name.strip() for name in body.calendars_checked):
        reason = 'incomplete_calendar_snapshot'
    elif calls.config.LIVE_CALLS_ENABLED and body.source != 'google_calendar':
        reason = 'mock_calendar_live_call_disabled'
    elif sorted(set(body.calendars_checked)) != sorted(set(settings.GOOGLE_CALENDAR_IDS)):
        reason = 'calendar_scope_mismatch'
    elif not -5 <= age <= 90:
        reason = 'stale_calendar_snapshot'
    elif not body.start <= current < body.end:
        reason = 'outside_free_window'
    elif (body.end - current).total_seconds() - settings.CALENDAR_BUFFER_MINUTES * 60 < 600:
        reason = 'less_than_10_usable_minutes'
    if reason:
        return 200, {'disposition': 'suppressed', 'reason': reason}
    identity = json.dumps([settings.DEMO_PHONE, body.start.astimezone(timezone.utc).isoformat(),
                           body.end.astimezone(timezone.utc).isoformat(), sorted(set(body.calendars_checked))], separators=(',', ':'))
    key = 'gcal:' + hashlib.sha256(identity.encode()).hexdigest()
    usable = int((body.end - current).total_seconds() // 60) - settings.CALENDAR_BUFFER_MINUTES
    window = FreeWindow(start=current, end=body.end, minutes=min(120, usable),
                        next_event_title=body.next_event_title or 'the end of this free block')
    try:
        rec = recommend(window, 'audio', get_profile(settings.DEMO_PHONE))
    except RuntimeError:
        return 200, {'disposition': 'suppressed', 'reason': 'no_suitable_recommendation'}
    result = calls.submit(settings.DEMO_PHONE, rec, key, 'gcal:event:' + body.event_id,
                          {'observedAt': body.observed_at.isoformat(), 'start': body.start.isoformat(),
                           'end': body.end.isoformat(), 'calendarsChecked': body.calendars_checked,
                           'usableMinutes': usable, 'source': body.source})
    return (202 if result['disposition'] == 'accepted' else 200), result
