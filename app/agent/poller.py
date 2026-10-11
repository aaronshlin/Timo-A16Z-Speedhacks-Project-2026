"""Every 60s: find a gap, recommend, act, notify."""

import asyncio
import logging
from datetime import datetime

from app.agent.actions import run_auto_actions
from app.brain.profile import get_profile
from app.brain.recommend import recommend
from app.brain.state import has_fired, mark_fired, set_last_pick
from app.clock import now as clock_now
from app.clock import set_override
from app.config import settings
from app.context.calendar import get_events
from app.models import FreeWindow, Recommendation
from app.phone.notify import notify_user

log = logging.getLogger(__name__)


def find_gap(now: datetime, *, ignore_fired: bool = False) -> FreeWindow | None:
    events = get_events(now.date())
    for earlier, later in zip(events, events[1:]):
        minutes = int((later.start - earlier.end).total_seconds() // 60)
        if minutes < 10:
            continue
        if abs((earlier.end - now).total_seconds()) > 120:
            continue
        window = FreeWindow(
            start=earlier.end,
            end=later.start,
            minutes=minutes,
            next_event_title=later.title,
        )
        if not ignore_fired and has_fired(window):
            continue
        return window
    return None


def run_tick(now: datetime | str | None = None) -> Recommendation | None:
    if isinstance(now, datetime):
        set_override(now.isoformat())
    elif isinstance(now, str):
        set_override(now)
    current = clock_now()
    window = find_gap(current)
    if window is None:
        log.info("no gap at %s", current.isoformat())
        return None
    profile = get_profile(settings.DEMO_PHONE)
    rec = recommend(window, "laptop", profile)
    run_auto_actions(rec)
    notify_user(settings.DEMO_PHONE, rec, profile.channel)
    mark_fired(window)
    set_last_pick(settings.DEMO_PHONE, rec)
    return rec


async def poll_loop() -> None:
    while True:
        try:
            run_tick()
        except Exception:
            log.exception("poller tick failed")
        await asyncio.sleep(60)
