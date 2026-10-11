"""Inbound texts. Dial's message.received webhook at /webhooks/dial comes in through
handle_dial_message; handle_reply is the legacy loop: one-word replies, plus a free-text
window like '30 min on the bus'."""

import logging
import re
from datetime import timedelta

from app.agent.actions import execute
from app.agent.poller import find_gap
from app.config import settings
from app.brain.profile import get_profile, record_feedback
from app.brain.recommend import recommend
from app.brain import state
from app.brain.state import get_last_pick, set_last_pick
from app.clock import now
from app.context.router import dispatch
from app.models import FreeWindow, Recommendation, Situation, TriggerEvent
from app.phone import test_user
from app.phone.dial import timo_says_now, user_e164
from app.phone.messages import format_done, format_error_fallback, format_go, format_repick

log = logging.getLogger(__name__)

_SITUATIONS: dict[str, Situation] = {
    "scooter": "audio",
    "walk": "audio",
    "bus": "phone",
    "laptop": "laptop",
    "audio": "audio",
}
last_situation: dict[str, Situation] = {}
excluded_ids: dict[str, set[str]] = {}
completed: dict[str, set[str]] = {}
_MINUTES = re.compile(r"(\d+)\s*(?:min|mins|minutes)\b", re.I)
_SITUATION_WORD = re.compile(r"\b(bus|scooter|walk|laptop|audio)\b", re.I)
_HELP = "Reply go / next / done / bus / scooter"
_handled: set[str] = set()


def handle_dial_message(event: dict, *, return_reply: bool = False):
    """Both signed SMS endpoints use one identity, routing and durable dedupe path."""
    from app.phone.dial import mark_timo_delivered, remember_user_message

    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    sender = str(data.get("from") or "")
    to = str(data.get("to") or data.get("numberId") or data.get("phoneNumberId") or "")
    body = str(data.get("body") or "")
    timo = {v for v in (settings.DIAL_NUMBER, settings.TIMO_NUMBER, settings.DIAL_FROM_NUMBER_ID) if v}
    user = {v for v in (settings.DEMO_PHONE, settings.USER_NUMBER, settings.USER_NUMBER_ID, settings.DIAL_USER_NUMBER_ID) if v}
    if not ((to in timo and sender in user) or (to in user and sender in timo)):
        return None
    event_id = str(event.get("id") or "")
    if not event_id or not state.claim_inbound_event(event_id):
        return None
    if to in timo:
        remember_user_message(body)
        return dispatch(TriggerEvent(user_id=canonical_user(sender), type="user_message",
                                     payload={"body": body, "return_reply": return_reply, "event_id": event_id}, ts=now()))
    mark_timo_delivered(body)
    # Only the explicitly configured second-number persona answers automatically.
    if settings.DIAL_USER_NUMBER_ID:
        test_user.on_text(settings.DIAL_NUMBER or settings.TIMO_NUMBER, body)
    return None


def canonical_user(phone: str) -> str:
    """All configured aliases share the enrolled user's profile and task history."""
    aliases = {v for v in (settings.DEMO_PHONE, settings.USER_NUMBER,
                           settings.USER_NUMBER_ID, settings.DIAL_USER_NUMBER_ID) if v}
    return (settings.DEMO_PHONE or user_e164()) if phone in aliases else phone


def reset_memory() -> None:
    last_situation.clear()
    excluded_ids.clear()
    completed.clear()
    _handled.clear()


def note_fresh_pick(phone: str, pick_id: str) -> None:
    """A new gap just fired. Drop ids from the previous gap and keep this pick."""
    excluded_ids[canonical_user(phone)] = {pick_id}


def _situation_for(phone: str) -> Situation:
    return last_situation.get(canonical_user(phone), "laptop")


def _excluded(phone: str) -> list[str]:
    user = canonical_user(phone)
    ids = set(excluded_ids.get(user, set()))
    ids.update(completed.get(user, set()))
    return list(ids)


def _keep_pick(phone: str, pick_id: str) -> None:
    excluded_ids.setdefault(canonical_user(phone), set()).add(pick_id)


def _say(body: str, phone: str) -> str:
    timo_says_now(body, to=phone)
    return body


def handle_reply(from_number: str, body: str) -> str:
    identity = canonical_user(from_number or user_e164())
    word = body.strip().lower()
    profile = get_profile(identity)
    last = get_last_pick(identity)

    if word == "go":
        if last is None:
            return _say(format_go("No pick to run."), identity)
        record_feedback(identity, last, "go")
        return _say(format_go(execute(last)), identity)

    if word == "done":
        if last is None:
            return _say(format_go("Nothing in progress."), identity)
        completed.setdefault(identity, set()).add(last.pick.id)
        record_feedback(identity, last, "done")
        return _say(format_done(last), identity)

    if word == "next" or word in _SITUATIONS:
        return _recommend_reply(identity, profile, last, word)

    minutes, situation = _parse_free_text(body)
    if minutes is None and situation is None:
        return _say(_HELP, identity)

    if situation is not None:
        last_situation[identity] = situation
    else:
        situation = _situation_for(identity)
    window = _window_for(minutes)
    if window is None:
        return _say(format_error_fallback(last) if last is not None else _HELP, identity)
    if last is not None:
        record_feedback(identity, last, situation)
    label = situation if minutes is None or _SITUATION_WORD.search(body) else "next"
    return _send_recommendation(identity, window, situation, profile, label=label)


def _recommend_reply(identity: str, profile, last: Recommendation | None, word: str) -> str:
    window = find_gap(now(), ignore_fired=True)
    if window is None:
        return _say(format_error_fallback(last) if last is not None else _HELP, identity)
    if word in _SITUATIONS:
        situation = _SITUATIONS[word]
        last_situation[identity] = situation
        label = situation
    else:
        situation = _situation_for(identity)
        label = "next"
    if last is not None:
        record_feedback(identity, last, word)
    return _send_recommendation(identity, window, situation, profile, label=label)


def _send_recommendation(identity, window, situation, profile, label: str) -> str:
    last = get_last_pick(identity)
    try:
        rec = recommend(window, situation, profile, exclude_ids=_excluded(identity))
    except Exception:
        log.exception("recommend failed")
        return _say(format_error_fallback(last) if last is not None else _HELP, identity)
    set_last_pick(identity, rec)
    _keep_pick(identity, rec.pick.id)
    text = format_repick(rec, label)
    return _say(text, identity)


def _parse_free_text(body: str) -> tuple[int | None, Situation | None]:
    minutes = None
    found = _MINUTES.search(body)
    if found:
        minutes = int(found.group(1))
    situation = None
    word = _SITUATION_WORD.search(body)
    if word:
        situation = _SITUATIONS[word.group(1).lower()]
    return minutes, situation


def _window_for(minutes: int | None) -> FreeWindow | None:
    if minutes is None:
        return find_gap(now(), ignore_fired=True)
    if minutes < 1:
        return None
    start = now()
    return FreeWindow(
        start=start,
        end=start + timedelta(minutes=minutes),
        minutes=minutes,
        next_event_title=None,
    )
