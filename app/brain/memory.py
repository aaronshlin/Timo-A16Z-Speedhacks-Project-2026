"""Memory layer. Read at the start of every run, written at the end.

Facts carry a source and a confidence. What the user said outranks anything Timo found or
inferred. Scrubbed facts are staged as a pending action and only saved once the user confirms.
"""

import logging
import uuid
from datetime import timedelta

from app.brain import llm, state
from app.brain.profile import get_profile, upsert_profile
from app.clock import now
from app.models import Channel, Fact, FactSource, Interaction, Memory, PendingAction

log = logging.getLogger(__name__)
RANK: dict[str, int] = {"user_said": 3, "gmail": 2, "scrub": 1, "inferred": 0}
ONBOARDING_KEYS = ["name", "home", "weekday_destination", "commute_mode", "leave_time"]
PROFILE_KEYS = [*ONBOARDING_KEYS, "role", "employer", "school", "interests", "links"]
# These hold a comma-separated list that grows; every other key holds one value.
LIST_KEYS = {"interests", "skills", "projects", "links"}


def load(phone: str) -> Memory:
    return Memory(
        profile=get_profile(phone),
        facts=state.get_facts(phone),
        recent=state.get_interactions(phone, since=now() - timedelta(hours=24)),
        pending=state.get_pending(phone),
    )


def list_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def remember(
    phone: str, key: str, value: str, source: FactSource, confidence: float = 1.0
) -> Fact:
    existing = next((fact for fact in state.get_facts(phone) if fact.key == key), None)
    if existing is not None and key in LIST_KEYS:
        items = list_items(existing.value)
        known = {item.lower() for item in items}
        added = [item for item in dict.fromkeys(list_items(value)) if item.lower() not in known]
        if not added:
            return existing
        value = ", ".join(items + added)
        if RANK[existing.source] > RANK[source]:
            source, confidence = existing.source, existing.confidence
    elif existing is not None and RANK[existing.source] > RANK[source]:
        log.info("keep fact phone=%s key=%s source=%s over %s", phone, key, existing.source, source)
        return existing
    fact = Fact(key=key, value=value, source=source, confidence=confidence, updated_at=now())
    state.save_fact(phone, fact)
    if key == "name":
        upsert_profile(phone, name=value)
    log.info("fact phone=%s key=%s source=%s", phone, key, source)
    return fact


def learn(phone: str, text: str, keys: list[str]) -> list[Fact]:
    """Extract the user's stated facts from one free-text answer and save them.

    Without the LLM, a single-key question stores the raw answer. An empty result means
    the answer didn't parse and the caller should ask a short follow-up.
    """
    extracted = llm.extract_facts(text, keys)
    if extracted is None and len(keys) == 1 and text.strip():
        extracted = {keys[0]: text.strip()}
    return [remember(phone, key, value, "user_said") for key, value in (extracted or {}).items()]


def log_interaction(
    phone: str,
    direction: str,
    channel: Channel,
    text: str,
    *,
    proactive: bool = False,
    pick_id: str | None = None,
    event_id: str | None = None,
) -> Interaction:
    item = Interaction(
        ts=now(),
        direction=direction,
        channel=channel,
        text=text,
        proactive=proactive,
        pick_id=pick_id,
    )
    if event_id:
        state.add_interaction_once(phone, item, event_id)
    else:
        state.add_interaction(phone, item)
    return item


def add_pending(phone: str, kind: str, summary: str, payload: dict | None = None) -> PendingAction:
    action = PendingAction(
        id=f"pa-{uuid.uuid4().hex[:8]}",
        kind=kind,
        summary=summary,
        payload=payload or {},
        created_at=now(),
    )
    state.add_pending(phone, action)
    return action


def latest_pending(phone: str) -> PendingAction | None:
    pending = state.get_pending(phone)
    return pending[-1] if pending else None


def resolve_pending(action_id: str, status: str = "done") -> None:
    state.set_pending_status(action_id, status)


def stage_facts(
    phone: str,
    facts: dict[str, str],
    source: FactSource,
    confidence: float = 0.7,
    summary: str | None = None,
) -> PendingAction:
    """Hold facts (e.g. from the profile scrub) until the user replies Y."""
    summary = summary or ", ".join(f"{key}: {value}" for key, value in facts.items())
    return add_pending(
        phone,
        "confirm_facts",
        summary,
        {"facts": facts, "source": source, "confidence": confidence},
    )


def confirm_facts(phone: str, action: PendingAction) -> list[Fact]:
    saved = [
        remember(phone, key, value, action.payload["source"], action.payload["confidence"])
        for key, value in action.payload["facts"].items()
    ]
    resolve_pending(action.id)
    return saved


def reject_facts(phone: str, action: PendingAction) -> None:
    """The user said N. The staged facts stay on file as ruled out so nobody suggests them again."""
    resolve_pending(action.id, "rejected")
    log.info("ruled out phone=%s action=%s", phone, action.id)


def ruled_out(phone: str) -> list[PendingAction]:
    return [action for action in state.get_pending(phone, "rejected") if action.kind == "confirm_facts"]
