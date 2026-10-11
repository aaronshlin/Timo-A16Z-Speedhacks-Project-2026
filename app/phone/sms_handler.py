"""Inbound texts. Dial's message.received webhook comes in through handle_dial_message;
handle_reply is the legacy one-word loop (go, next, done, or a situation)."""

import logging

from app.agent.actions import execute
from app.agent.poller import find_gap
from app.brain.profile import get_profile, record_feedback
from app.brain.recommend import recommend
from app.brain.state import get_last_pick, set_last_pick
from app.clock import now
from app.config import settings
from app.context.router import dispatch
from app.context.todoist import update_status
from app.models import Situation, TriggerEvent
from app.phone import test_user
from app.phone.dial import send_sms
from app.phone.notify import notify_user

log = logging.getLogger(__name__)

_SITUATIONS: dict[str, Situation] = {
    "scooter": "phone",
    "walk": "phone",
    "bus": "phone",
    "laptop": "laptop",
}
_situation: dict[str, Situation] = {}
_handled: set[str] = set()


def handle_dial_message(event: dict) -> None:
    """One signed message.received webhook. Dial retries, so each event id is handled once.

    A text to Timo's number becomes a user_message trigger. A text to the test user's number is
    Timo writing to the simulated user, who answers from its persona.
    """
    if event["id"] in _handled:
        return
    _handled.add(event["id"])
    data = event.get("data", {})
    sender, to, body = str(data.get("from", "")), data.get("to"), str(data.get("body", ""))
    if to and to == settings.DIAL_NUMBER:
        dispatch(TriggerEvent(user_id=sender, type="user_message", payload={"body": body}, ts=now()))
    elif to and to == settings.DEMO_PHONE:
        test_user.on_text(sender, body)
    else:
        log.info("inbound text to an unknown number, ignored")


def handle_reply(from_number: str, body: str) -> str:
    word = body.strip().lower()
    profile = get_profile(from_number)
    last = get_last_pick(from_number)

    if word == "go":
        if last is None:
            reply = "No pick to run."
        else:
            record_feedback(from_number, last, "go")
            reply = execute(last)
        send_sms(from_number, reply)
        return reply

    if word == "done":
        if last is None:
            reply = "Nothing in progress."
        else:
            record_feedback(from_number, last, "done")
            if last.pick.kind == "task":
                update_status(last.pick.id, "done")
            reply = f"Marked done: {last.pick.title}"
        send_sms(from_number, reply)
        return reply

    if word != "next" and word not in _SITUATIONS:
        reply = "Reply go / next / done / scooter / bus / laptop"
        send_sms(from_number, reply)
        return reply

    window = find_gap(now(), ignore_fired=True)
    if window is None:
        reply = "No free window right now."
        send_sms(from_number, reply)
        return reply

    if word in _SITUATIONS:
        situation = _SITUATIONS[word]
        _situation[from_number] = situation
        exclude: list[str] = []
    else:
        situation = _situation.get(from_number, "laptop")
        exclude = [last.pick.id] if last is not None else []

    if last is not None:
        record_feedback(from_number, last, word)
    rec = recommend(window, situation, profile, exclude_ids=exclude)
    set_last_pick(from_number, rec)
    notify_user(from_number, rec, "sms")
    return rec.sms_text
