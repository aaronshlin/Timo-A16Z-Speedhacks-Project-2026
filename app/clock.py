"""The only place that reads the current time."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings

_LA = ZoneInfo("America/Los_Angeles")
_override: datetime | None = None


def set_override(iso: str | None) -> None:
    global _override
    if not iso:
        _override = None
        return
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_LA)
    _override = parsed


def now() -> datetime:
    if _override is not None:
        return _override
    if settings.NOW_OVERRIDE:
        parsed = datetime.fromisoformat(settings.NOW_OVERRIDE)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_LA)
        return parsed
    return datetime.now(_LA)
