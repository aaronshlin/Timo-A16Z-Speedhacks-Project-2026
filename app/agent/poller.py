"""One 60-second producer: complete calendar read -> actual current gap -> typed receiver."""
import asyncio
import logging
import uuid
from datetime import datetime, time

from app.agent.calling import real_clock_required, validate_clock
from app.agent.calendar_trigger import CalendarGap, trigger_calendar_gap
from app import clock
from app.config import settings
from app.context import calendar
from app.models import FreeWindow, Recommendation

log = logging.getLogger(__name__)


def compute_gap(current, events):
    if current.tzinfo is None:
        raise ValueError('Calendar clock must have a timezone')
    if not 0 <= settings.CALENDAR_ACTIVE_START_HOUR < settings.CALENDAR_ACTIVE_END_HOUR <= 24:
        raise ValueError('Invalid allowed calling hours')
    from datetime import timedelta
    start = datetime.combine(current.date(), time.min, current.tzinfo) + timedelta(hours=settings.CALENDAR_ACTIVE_START_HOUR)
    end = datetime.combine(current.date(), time.min, current.tzinfo) + timedelta(hours=settings.CALENDAR_ACTIVE_END_HOUR)
    if not start <= current < end:
        return None
    busy = []
    for event in sorted(events, key=lambda e: e.start):
        if event.start.tzinfo is None or event.end.tzinfo is None or event.start >= event.end:
            raise ValueError('Calendar contains invalid event bounds')
        if event.end <= start or event.start >= end:
            continue
        left, right = max(start, event.start), min(end, event.end)
        if busy and left <= busy[-1][1]:
            busy[-1] = (busy[-1][0], max(right, busy[-1][1]), busy[-1][2])
        else:
            busy.append((left, right, event.title))
    cursor = start
    for left, right, title in busy + [(end, end, None)]:
        if cursor <= current < left:
            usable = int((left - current).total_seconds() // 60) - settings.CALENDAR_BUFFER_MINUTES
            if usable >= 10:
                return FreeWindow(start=cursor, end=left, minutes=usable, next_event_title=title)
            return None
        cursor = max(cursor, right)
    return None


def current_time(value=None):
    validate_clock(settings)
    if value is not None and real_clock_required(settings):
        raise ValueError('Fake-time ticks are disabled for real calendar reads and live calls')
    current = datetime.fromisoformat(value) if isinstance(value, str) else value or clock.now()
    if current.tzinfo is None:
        raise ValueError('Tick time must include a timezone')
    return current


def find_gap(now, *, ignore_fired=False):
    current = current_time(now if settings.USE_MOCKS and not settings.LIVE_CALLS_ENABLED else None)
    events = calendar.get_events(current.date())
    return compute_gap(current, events)


def run_tick_detailed(value=None):
    current = current_time(value)
    try:
        events = calendar.get_events(current.date())
    except Exception as exc:
        result = {'disposition': 'suppressed', 'reason': 'calendar_read_failed',
                  'snapshot_complete': False, 'error_type': type(exc).__name__, 'observed_at': current.isoformat()}
        log.warning('calendar trigger suppressed: %s', result['reason'])
        return result
    observed = clock.now() if real_clock_required(settings) else current
    window = compute_gap(observed, events)
    if window is None:
        return {'disposition': 'suppressed', 'reason': 'no_eligible_current_gap',
                'snapshot_complete': True, 'events_count': len(events), 'observed_at': observed.isoformat()}
    body = CalendarGap(event_id=str(uuid.uuid4()), source='mock_calendar' if settings.USE_MOCKS else 'google_calendar',
                       observed_at=current, start=window.start, end=window.end,
                       calendars_checked=settings.GOOGLE_CALENDAR_IDS, snapshot_complete=True,
                       next_event_title=window.next_event_title)
    _, result = trigger_calendar_gap(body, current=observed if settings.USE_MOCKS and not settings.LIVE_CALLS_ENABLED else None)
    result.update(snapshot_complete=True, events_count=len(events), observed_at=observed.isoformat(),
                  gap={'start': window.start.isoformat(), 'end': window.end.isoformat(),
                       'usable_minutes_remaining': window.minutes, 'buffer_minutes': settings.CALENDAR_BUFFER_MINUTES})
    log.info('calendar trigger disposition=%s usable_minutes=%s calls_enabled=%s', result['disposition'], window.minutes, settings.LIVE_CALLS_ENABLED)
    return result


def run_tick(now=None) -> Recommendation | None:
    result = run_tick_detailed(now)
    if 'sessionId' not in result or result['disposition'] != 'accepted':
        return None
    from app.agent.calling import service
    return Recommendation.model_validate(service().get_session(result['sessionId'])['recommendation'])


async def poll_loop():
    while True:
        try:
            await asyncio.to_thread(run_tick_detailed)
        except Exception:
            log.exception('calendar poller tick failed; calls suppressed')
        await asyncio.sleep(60)
