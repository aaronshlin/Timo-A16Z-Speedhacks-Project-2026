"""Event router: every trigger source enters the agent here.

The one caller of brain.runner.run. Inbound SMS, the calendar webhook and the
scheduler all build a TriggerEvent and hand it to dispatch, so there is exactly
one agent loop (rule 1).

    from app.context.router import dispatch
    from app.models import TriggerEvent
    from app.clock import now

    dispatch(TriggerEvent(user_id=phone, type="user_message",
                          payload={"body": text}, ts=now()))

dispatch never raises. A trigger source is usually a webhook, and a failed run
must not return a 500 to Google or Dial, which would make them retry or drop
the subscription (rule 12).

Import direction is one way: router -> runner -> everything else. Nothing the
runner imports may import this module at module level, or the import cycles.
Trigger sources import router; router imports them never.
"""

import logging
import threading
from datetime import timedelta

from app.clock import now
from app.models import TriggerEvent

log = logging.getLogger(__name__)

# Calendar pushes fire several times for one edit, so the same event is
# debounced for this long (rule 8). Keyed per user and per underlying thing,
# never globally, so two users never silence each other.
DEBOUNCE = timedelta(seconds=30)

_seen: dict[tuple[str, str, str], object] = {}
_lock = threading.Lock()


def _dedupe_key(event: TriggerEvent) -> tuple[str, str, str]:
    """What counts as "the same trigger again"."""
    payload = event.payload
    detail = ""
    if event.type == "new_event":
        detail = str(payload.get("event_id", ""))
    elif event.type == "free_slot":
        window = payload.get("window") or {}
        detail = str(window.get("end", "")) if isinstance(window, dict) else ""
    elif event.type == "pre_commute":
        detail = str(payload.get("depart_at", ""))
    elif event.type == "morning_plan":
        detail = now().date().isoformat()
    # user_message is never debounced: every text the user sends is intentional,
    # including the same word twice.
    return (event.user_id, event.type, detail)


def _is_duplicate(event: TriggerEvent) -> bool:
    if event.type == "user_message":
        return False
    key = _dedupe_key(event)
    at = now()
    with _lock:
        last = _seen.get(key)
        if last is not None and at - last < DEBOUNCE:
            return True
        _seen[key] = at
    return False


def reset() -> None:
    """Clear the debounce window. For tests and between demo runs."""
    with _lock:
        _seen.clear()


def dispatch(event: TriggerEvent) -> None:
    """Route one trigger into the agent. Never raises."""
    if _is_duplicate(event):
        log.info("trigger user=%s type=%s dropped: debounced", event.user_id, event.type)
        return

    # Imported here, not at module scope: the runner reaches sms_handler, which
    # a trigger source may also import, and a top-level import would cycle.
    from app.brain import runner

    try:
        result = runner.run(event)
    except Exception:
        log.exception("trigger user=%s type=%s failed", event.user_id, event.type)
        return

    log.info(
        "trigger user=%s type=%s outcome=%s",
        event.user_id,
        event.type,
        "nothing" if result is None else getattr(getattr(result, "pick", None), "id", result),
    )
