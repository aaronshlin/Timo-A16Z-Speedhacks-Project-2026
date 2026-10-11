"""One-word replies. go, next, done, or a situation."""

from app.agent.actions import execute
from app.agent.poller import find_gap
from app.brain.profile import get_profile, record_feedback
from app.brain.recommend import recommend
from app.brain.state import get_last_pick, set_last_pick
from app.clock import now
from app.context.todoist import update_status
from app.models import Situation
from app.phone.dial import send_sms
from app.phone.notify import notify_user

_SITUATIONS: dict[str, Situation] = {
    "scooter": "phone",
    "walk": "phone",
    "bus": "phone",
    "laptop": "laptop",
}
_situation: dict[str, Situation] = {}


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
