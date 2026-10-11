"""Mock calendar. Replace with Google Calendar."""

import json
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.models import Event

log = logging.getLogger(__name__)
_LA = ZoneInfo("America/Los_Angeles")
_PATH = Path(__file__).resolve().parents[2] / "data" / "mock_calendar.json"


def _at(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_LA)


def get_events(day: date) -> list[Event]:
    raw = json.loads(_PATH.read_text())
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


def create_block(start: datetime, end: datetime, title: str) -> str:
    block_id = f"block-{int(start.timestamp())}"
    log.info("create_block id=%s title=%s start=%s end=%s", block_id, title, start, end)
    print(f"create_block {block_id} {title}")
    return block_id
