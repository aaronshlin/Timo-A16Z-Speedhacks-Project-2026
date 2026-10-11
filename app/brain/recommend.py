"""One picker. Poller, SMS replies, and the MCP tool all call this."""

import logging

from app.brain.ranking import rank_candidates
from app.models import FreeWindow, Recommendation, Situation, UserProfile

log = logging.getLogger(__name__)
_OPTIONS = "Reply: go / next / done / scooter / bus / laptop"


def recommend(
    window: FreeWindow,
    situation: Situation,
    profile: UserProfile,
    exclude_ids: list[str] = [],
) -> Recommendation:
    candidates = rank_candidates(window, situation, profile, exclude_ids)
    if not candidates:
        raise RuntimeError("no candidates for this window")
    pick = candidates[0]
    nxt = window.next_event_title or "your next class"
    reason = f"{pick.title} fits this {window.minutes}-minute gap before {nxt}."
    speech_text = (
        f"You have {window.minutes} minutes before {nxt}. "
        f"Do {pick.title}, about {pick.minutes} minutes."
    )
    link = pick.url or ""
    sms_text = "\n".join(part for part in (reason, link, _OPTIONS) if part)
    auto_actions = (
        ["calendar_block", "todoist_in_progress"] if pick.kind == "task" else []
    )
    rec = Recommendation(
        pick=pick,
        reason=reason,
        speech_text=speech_text,
        sms_text=sms_text,
        auto_actions=auto_actions,
    )
    log.info(
        "window=%s situation=%s candidates=%s pick=%s actions=%s channel=%s",
        window.minutes,
        situation,
        [item.id for item in candidates],
        pick.id,
        auto_actions,
        profile.channel,
    )
    return rec
