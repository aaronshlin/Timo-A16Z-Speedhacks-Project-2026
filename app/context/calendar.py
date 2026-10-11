"""Calendar events and blocks. Google when USE_MOCKS=0, data/mock_calendar.json otherwise."""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote

import httpx

from app.config import settings
from app.context.oauth import get_access_token
from app.models import Event

log = logging.getLogger(__name__)
_LA = ZoneInfo("America/Los_Angeles")
_PATH = Path(__file__).resolve().parents[2] / "data" / "mock_calendar.json"
_API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_TIMEOUT = 5.0


class CalendarReadError(RuntimeError):
    """No complete calendar snapshot is available; callers must suppress triggers."""


def _at(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_LA)


def _mock_events(day: date) -> list[Event]:
    path = Path(settings.MOCK_CALENDAR_PATH) if settings.MOCK_CALENDAR_PATH else _PATH
    raw = json.loads(path.read_text())
    if settings.MOCK_CALENDAR_PATH:
        if (not isinstance(raw, dict) or raw.get('date') != day.isoformat()
                or raw.get('snapshot_complete') is not True or not isinstance(raw.get('events'), list)):
            raise CalendarReadError('Incomplete or wrong-day demo calendar snapshot')
        raw = raw['events']
    events = [
        Event(
            id=item["id"],
            title=item["title"],
            start=_at(day, item["start"]),
            end=_at(day, item["end"]),
        )
        for item in raw
    ]
    events.sort(key=lambda event: event.start)
    return events


def _to_event(item: dict) -> Event | None:
    """None for anything that should not use up a gap."""
    if item.get("status") == "cancelled":
        return None
    if item.get("transparency") == "transparent":  # marked Free
        return None
    if any(a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", [])):
        return None
    if "dateTime" in item.get("start", {}):
        start = datetime.fromisoformat(item["start"]["dateTime"])
        end = datetime.fromisoformat(item["end"]["dateTime"])
        if start.tzinfo is None or end.tzinfo is None:
            raise CalendarReadError("Calendar event has no timezone")
        start, end = start.astimezone(_LA), end.astimezone(_LA)
    else:
        # Google's all-day end date is exclusive, in the calendar's timezone.
        zone = ZoneInfo(item.get("start", {}).get("timeZone") or "America/Los_Angeles")
        start = datetime.combine(date.fromisoformat(item["start"]["date"]), datetime.min.time(), zone).astimezone(_LA)
        end = datetime.combine(date.fromisoformat(item["end"]["date"]), datetime.min.time(), zone).astimezone(_LA)
    if end <= start:
        raise CalendarReadError("Calendar event has invalid bounds")
    return Event(id=item["id"], title=item.get("summary") or "(untitled)", start=start, end=end)


def _merge(events: list[Event]) -> list[Event]:
    """Overlapping events become one block, so find_gap never sees a phantom gap inside one."""
    merged: list[Event] = []
    for event in sorted(events, key=lambda e: e.start):
        if merged and event.start <= merged[-1].end:
            merged[-1] = merged[-1].model_copy(update={"end": max(merged[-1].end, event.end)})
        else:
            merged.append(event)
    return merged


def _request(method: str, url: str, **kw) -> httpx.Response:
    """Retry once on timeout.

    Warm calls take ~0.3s, but the first one after a cold start pays DNS and a
    TLS handshake and can blow the 5s budget. Without the retry that tick sees
    an empty day and Timo silently skips the gap.
    """
    for attempt in (1, 2):
        try:
            return httpx.request(method, url, timeout=_TIMEOUT, **kw)
        except httpx.TimeoutException:
            if attempt == 2:
                raise
            log.warning("google %s timed out, retrying once", method)
    raise AssertionError("unreachable")


def _google_events(day: date) -> list[Event]:
    start = datetime(day.year, day.month, day.day, tzinfo=_LA)
    scopes = sorted(set(settings.GOOGLE_CALENDAR_IDS))
    if not scopes or any(not scope.strip() for scope in scopes):
        raise CalendarReadError("No calendar scope selected")
    token = get_access_token()
    events = []
    for calendar_id in scopes:
        params = {
            "timeMin": start.isoformat(),
            "timeMax": (start + timedelta(days=1)).isoformat(),
            "singleEvents": "true",  # expand recurring lectures
            "orderBy": "startTime",
            "maxResults": 250,
        }
        seen_pages = set()
        while True:
            resp = _request("GET", "https://www.googleapis.com/calendar/v3/calendars/" + quote(calendar_id, safe="") + "/events",
                            params=params, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload.get("items"), list):
                raise CalendarReadError("Calendar response is incomplete")
            zone = payload.get("timeZone") or "America/Los_Angeles"
            for item in payload["items"]:
                if "date" in item.get("start", {}):
                    item = {**item, "start": {**item["start"], "timeZone": zone}}
                event = _to_event(item)
                if event:
                    events.append(event.model_copy(update={"id": calendar_id + ":" + event.id}))
            page = payload.get("nextPageToken")
            if not page:
                break
            if not isinstance(page, str) or page in seen_pages or len(seen_pages) >= 1000:
                raise CalendarReadError("Calendar pagination is incomplete")
            seen_pages.add(page)
            params = {**params, "pageToken": page}
    return _merge(events)


def get_events(day: date) -> list[Event]:
    if settings.USE_MOCKS:
        return _mock_events(day)
    try:
        events = _google_events(day)
    except Exception as exc:
        log.warning("google calendar read failed; triggers suppressed (%s)", type(exc).__name__)
        raise CalendarReadError("Google calendar snapshot unavailable") from exc
    log.info("get_events day=%s count=%d", day, len(events))
    return events


def create_block(start: datetime, end: datetime, title: str) -> str:
    if settings.USE_MOCKS:
        block_id = f"block-{int(start.timestamp())}"
        log.info("create_block id=%s title=%s start=%s end=%s", block_id, title, start, end)
        print(f"create_block {block_id} {title}")
        return block_id
    try:
        resp = _request(
            "POST",
            _API,
            headers={"Authorization": f"Bearer {get_access_token()}"},
            json={
                "summary": f"Timo: {title}",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "extendedProperties": {"private": {"timo": "1"}},
            },
        )
        resp.raise_for_status()
    except Exception:
        log.exception("google calendar block failed")
        return ""
    block_id = resp.json()["id"]
    log.info("create_block id=%s title=%s start=%s end=%s", block_id, title, start, end)
    return block_id
