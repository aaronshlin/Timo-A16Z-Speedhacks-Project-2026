"""One agent run per trigger: load memory, read context, decide, act, tell the user, save.
Doing nothing is a valid outcome."""

import logging

from app.agent.actions import run_auto_actions
from app.agent.call import call_user
from app.brain import briefing, memory, pending, planner, rules, state
from app.brain.recommend import recommend
from app.clock import now
from app.models import FreeWindow, Memory, Recommendation, Situation, TriggerEvent
from app.phone.dial import send_sms
from app.phone.notify import notify_user

log = logging.getLogger(__name__)
# These reach out with a briefing (the plan, what's done, what Timo can do next).
BRIEFED = ("pre_commute", "new_event", "morning_plan")


def _window(event: TriggerEvent) -> FreeWindow | None:
    if "window" in event.payload:
        return FreeWindow.model_validate(event.payload["window"])
    from app.agent.poller import find_gap

    return find_gap(now())


def _situation(event: TriggerEvent) -> Situation:
    return event.payload.get("situation") or "laptop"


def _answer_pending(phone: str, body: str) -> str | None:
    """A Y or N finishes the latest pending action. None means the message is about something else."""
    action = memory.latest_pending(phone)
    if action is None:
        return None
    yes, rest = rules.parse_yes_no(body)
    if yes is None:
        return None
    _, reply = pending.answer(phone, action, yes)
    if rest:
        memory.learn(phone, rest, memory.PROFILE_KEYS)
    return reply


def _brief(event: TriggerEvent, mem: Memory) -> str | None:
    """Call or text the briefing. On a call, the user's yes runs mid-call through the MCP tools."""
    phone = event.user_id
    urgent = bool(event.payload.get("urgent", False))
    channel = rules.choose_channel(mem, event.type, now(), urgent)
    if channel is None:
        log.info("run type=%s holding: quiet hours or rate limit", event.type)
        return None
    if channel == "call" and not call_user(phone, briefing.script(phone, event)):
        channel = "sms"  # the call didn't go out, so text instead
    # A call is logged as text without the ask: text_details adds whatever still waits for a yes.
    text = briefing.text(phone, event, ask=channel == "sms")
    if channel == "sms":
        send_sms(phone, text)
    memory.log_interaction(phone, "out", channel, text, proactive=True)
    log.info(
        "run type=%s channel=%s offers=%d facts=%d",
        event.type,
        channel,
        len(briefing.offers(mem)),
        len(mem.facts),
    )
    return f"briefed by {channel}"


def run(event: TriggerEvent) -> Recommendation | str | None:
    phone = event.user_id
    if event.type == "user_message":
        from app.phone.sms_handler import handle_reply

        body = str(event.payload.get("body", ""))
        memory.log_interaction(phone, "in", "sms", body)
        reply = _answer_pending(phone, body)
        if reply is not None:
            send_sms(phone, reply)
            memory.log_interaction(phone, "out", "sms", reply)
            log.info("run type=user_message answered pending action")
            return None
        handle_reply(phone, body)
        return state.get_last_pick(phone)

    if rules.should_ignore(event):
        log.info("run type=%s ignored: source=timo", event.type)
        return None
    mem = memory.load(phone)
    if event.type in BRIEFED:
        event = planner.enrich(event)
        if not briefing.plan(event):
            log.info("run type=%s nothing to plan, doing nothing", event.type)
            return None
        return _brief(event, mem)
    window = _window(event)
    if window is None:
        log.info("run type=%s no window, doing nothing", event.type)
        return None
    situation = _situation(event)
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
