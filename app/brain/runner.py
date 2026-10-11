"""One agent run per trigger: load memory, read context, decide, act, tell the user, save.
Doing nothing is a valid outcome."""

import logging

from app.agent.actions import run_auto_actions
from app.brain import memory, rules, state
from app.brain.recommend import recommend
from app.clock import now
from app.models import FreeWindow, Memory, Recommendation, Situation, TriggerEvent
from app.phone.notify import notify_user

log = logging.getLogger(__name__)
_COMMUTE_SITUATION: dict[str, Situation] = {
    "drive": "audio",
    "car": "audio",
    "bus": "phone",
    "train": "phone",
    "walk": "phone",
    "scooter": "phone",
    "bike": "audio",
}


def _window(event: TriggerEvent) -> FreeWindow | None:
    if "window" in event.payload:
        return FreeWindow.model_validate(event.payload["window"])
    from app.agent.poller import find_gap

    return find_gap(now())


def _situation(event: TriggerEvent, mem: Memory) -> Situation:
    if event.payload.get("situation"):
        return event.payload["situation"]
    if event.type != "pre_commute":
        return "laptop"
    mode = next((fact.value.lower() for fact in mem.facts if fact.key == "commute_mode"), "")
    return next((s for word, s in _COMMUTE_SITUATION.items() if word in mode), "phone")


def run(event: TriggerEvent) -> Recommendation | None:
    phone = event.user_id
    if event.type == "user_message":
        from app.phone.sms_handler import handle_reply

        body = str(event.payload.get("body", ""))
        memory.log_interaction(phone, "in", "sms", body)
        handle_reply(phone, body)
        return state.get_last_pick(phone)

    if rules.should_ignore(event):
        log.info("run type=%s ignored: source=timo", event.type)
        return None
    mem = memory.load(phone)
    window = _window(event)
    if window is None:
        log.info("run type=%s no window, doing nothing", event.type)
        return None
    situation = _situation(event, mem)
    urgent = bool(event.payload.get("urgent", False))
    channel = rules.choose_channel(mem, event.type, now(), urgent)
    if channel is None:
        log.info("run type=%s holding: quiet hours or rate limit", event.type)
        return None
    try:
        rec = recommend(window, situation, mem.profile)
    except RuntimeError:
        log.info("run type=%s window=%s nothing fits", event.type, window.minutes)
        return None

    ran = run_auto_actions(rec)
    notify_user(phone, rec, channel)
    text = rec.speech_text if channel == "call" else rec.sms_text
    memory.log_interaction(phone, "out", channel, text, proactive=True, pick_id=rec.pick.id)
    state.mark_fired(window)
    state.set_last_pick(phone, rec)
    log.info(
        "run type=%s window=%s situation=%s pick=%s actions=%s channel=%s",
        event.type,
        window.minutes,
        situation,
        rec.pick.id,
        ran,
        channel,
    )
    return rec
