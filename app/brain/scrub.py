"""Profile scrub: work out who a new user is so Timo can personalize their day.

Gmail first (work or school email domain, signature), then web search. Whatever research finds
is staged as a pending action and saved only after the user replies Y; a Y or N arrives as a
fresh user_message run and comes back here through runner.run and brain/pending.

Hard rules, in code:
- Research only fills RESEARCH_KEYS. Where someone lives and how they commute come from the user.
- What the user said outranks research, so research never asks about what they already told us.
- A match the user said N to stays on file as ruled out and is never suggested again.
"""

import logging
import re

from app.brain import llm, memory, state
from app.config import settings
from app.models import Fact, PendingAction
from app.phone.dial import send_sms_now

log = logging.getLogger(__name__)

RESEARCH_KEYS = {"role", "employer", "school", "program", "skills", "projects", "interests", "links"}
HINT_KEYS = ["name", "role", "employer", "school", "weekday_destination"]
ASK_THRESHOLD = 0.4
CONFIRMED_CONFIDENCE = 0.85
MAX_SEARCHES = 5
_FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}
_LINK = re.compile(r"https?://\S+|(?:linkedin|github)\.com/\S+")
_EMAIL = re.compile(r"\S+@\S+\.\w+")

# Fictional, for USE_MOCKS. The home fact shows the privacy rule: it never gets staged.
_MOCK = llm.Research(
    matches=[
        llm.Match(
            summary="CS student at UC Berkeley who builds AI agents",
            match_confidence=0.82,
            facts=[
                llm.Found(key="school", value="UC Berkeley"),
                llm.Found(key="program", value="Computer Science"),
                llm.Found(key="skills", value="Python"),
                llm.Found(key="interests", value="hackathons"),
                llm.Found(key="home", value="123 Example St"),
            ],
            sources=["https://example.edu/directory"],
        ),
        llm.Match(summary="product manager in Austin", match_confidence=0.21),
    ]
)


def build_hints(facts: list[Fact], email: str | None = None, signature: str = "") -> dict:
    """What tells this user apart from namesakes. Never includes where they live."""
    said = {fact.key: fact.value for fact in facts}
    domain = email.rsplit("@", 1)[-1].lower() if email and "@" in email else None
    lines = [line.strip(" -|") for line in signature.splitlines()]
    hints = {key: said[key] for key in HINT_KEYS if said.get(key)}
    hints["email_domain"] = None if domain in _FREE_MAIL else domain
    hints["links"] = [link.rstrip(").,") for link in _LINK.findall(f"{said.get('links', '')}\n{signature}")]
    # Signature lines like "CS @ UC Berkeley". Skip contact lines and anything with a number in it.
    hints["signature"] = [
        line
        for line in lines
        if line
        and len(line) < 80
        and not _LINK.search(line)
        and not _EMAIL.search(line)
        and sum(ch.isdigit() for ch in line) < 3
        and line.lower() != said.get("name", "").lower()
    ]
    return {key: value for key, value in hints.items() if value}


def start(phone: str, email: str | None = None, signature: str = "") -> str | None:
    """Research a user and text them one Y/N question about the best match.

    Slow (web search), so run it in a background task once onboarding has saved their name,
    ideally after Google consent so the email domain and signature can narrow the search.
    Returns the question it sent, or None when there is nothing worth asking.
    """
    if any(action.kind == "confirm_facts" for action in state.get_pending(phone)):
        return None
    facts = state.get_facts(phone)
    hints = build_hints(facts, email, signature)
    if "name" not in hints:
        return None
    result = _MOCK if settings.USE_MOCKS else llm.research(hints, MAX_SEARCHES)
    staged = _pick(result, facts, memory.ruled_out(phone))
    if staged is None:
        log.info("scrub phone=%s nothing to ask", phone)
        return None
    summary, found = staged
    action = memory.stage_facts(phone, found, "scrub", CONFIRMED_CONFIDENCE, summary=summary)
    question = (
        f"Quick check so I don't mix you up with someone else: {hints['name']}, {summary}. "
        "Is that you? Reply Y to add it to your profile, N if not."
    )
    send_sms_now(phone, question)
    memory.log_interaction(phone, "out", "sms", question)
    log.info("scrub phone=%s staged=%s pending=%s", phone, sorted(found), action.id)
    return question


def answer(phone: str, action: PendingAction, yes: bool) -> str:
    """Finish a staged scrub after the user's Y or N."""
    if yes:
        memory.confirm_facts(phone, action)
        return "Saved to your profile. I'll use it to plan around your day."
    memory.reject_facts(phone, action)
    return "No problem, I've dropped that and won't suggest it again."


def _pick(
    result: llm.Research | None, facts: list[Fact], ruled_out: list[PendingAction]
) -> tuple[str, dict[str, str]] | None:
    """The best match's new, allowed facts, or None if it is too unsure or already ruled out."""
    if result is None or not result.matches:
        return None
    best = max(result.matches, key=lambda match: match.match_confidence)
    if best.match_confidence < ASK_THRESHOLD or best.summary in {a.summary for a in ruled_out}:
        return None
    rejected = {
        (key, item.lower())
        for action in ruled_out
        for key, value in action.payload["facts"].items()
        for item in memory.list_items(value)
    }
    known = {fact.key: fact for fact in facts}
    found: dict[str, str] = {}
    for item in best.facts:
        key, value = item.key, item.value.strip()
        if key not in RESEARCH_KEYS or not value or (key, value.lower()) in rejected:
            continue
        if not _is_new(known.get(key), key, value):
            continue
        if key in memory.LIST_KEYS and key in found:
            found[key] = f"{found[key]}, {value}"
        else:
            found[key] = value
    return (best.summary, found) if found else None


def _is_new(existing: Fact | None, key: str, value: str) -> bool:
    if existing is None:
        return True
    if key in memory.LIST_KEYS:
        return value.lower() not in {item.lower() for item in memory.list_items(existing.value)}
    outranked = memory.RANK[existing.source] <= memory.RANK["scrub"]
    return outranked and existing.value.lower() != value.lower()
