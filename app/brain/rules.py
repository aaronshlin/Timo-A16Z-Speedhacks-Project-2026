"""Deterministic decision rules: whether to reach out, text vs call, and reading Y/N. No LLM."""

import re
from datetime import datetime, timedelta

from app.models import Channel, Memory, TriggerEvent, TriggerType, UserProfile

TEXT_GAP = timedelta(hours=1)
MAX_CALLS_PER_DAY = 2
_YES = re.compile(r"^\s*(y|yes|yep|yeah|yup|ya|sure|correct|that'?s me)\b[\s,.!-]*", re.I)
_NO = re.compile(r"^\s*(n|no|nope|nah|not me|wrong)\b[\s,.!-]*", re.I)


def parse_yes_no(text: str) -> tuple[bool | None, str]:
    """Read a reply to a Y/N ask. None means it was neither; the rest is anything said after."""
    for answer, pattern in ((True, _YES), (False, _NO)):
        if match := pattern.match(text):
            return answer, text[match.end() :].strip()
    return None, text.strip()


def should_ignore(event: TriggerEvent) -> bool:
    """Calendar changes Timo made itself must never wake the agent."""
    return event.type == "new_event" and event.payload.get("source") == "timo"


def in_quiet_hours(profile: UserProfile, at: datetime) -> bool:
    if profile.quiet_hours is None:
        return False
    start, end = profile.quiet_hours
    if start <= end:
        return start <= at.hour < end
    return at.hour >= start or at.hour < end


def _skips_calls(profile: UserProfile) -> bool:
    missed = profile.affinity.get("signal:call_missed", 0.0)
    answered = profile.affinity.get("signal:call_answered", 0.0)
    return missed >= 2 and missed > answered


def choose_channel(
    memory: Memory, trigger: TriggerType, at: datetime, urgent: bool = False
) -> Channel | None:
    """None means stay quiet this run."""
    if trigger == "user_message":
        return "sms"
    profile = memory.profile
    if in_quiet_hours(profile, at):
        return "sms" if urgent else None
    sent = [item for item in memory.recent if item.direction == "out"]
    calls_today = sum(1 for item in sent if item.channel == "call" and item.ts.date() == at.date())
    wants_call = urgent or trigger == "pre_commute" or profile.channel == "call"
    if wants_call and not (_skips_calls(profile) and not urgent):
        if urgent or calls_today < MAX_CALLS_PER_DAY:
            return "call"
    last_text = next(
        (item for item in reversed(sent) if item.proactive and item.channel == "sms"), None
    )
    if not urgent and last_text is not None and at - last_text.ts < TEXT_GAP:
        return None
    return "sms"
