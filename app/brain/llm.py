"""One structured LLM call per job. Every function returns None on failure, a timeout (3s, or
60s for web research), or an invalid answer, so callers keep their deterministic fallback."""

import json
import logging

import anthropic
from pydantic import BaseModel, Field, create_model

from app.config import settings
from app.models import Candidate, Fact, FreeWindow, Situation, UserProfile

log = logging.getLogger(__name__)
_client: anthropic.Anthropic | None = None
_research_client: anthropic.Anthropic | None = None
RESEARCH_TIMEOUT = 60.0
MAX_CONTINUATIONS = 2

PICK_SYSTEM = """You are Timo. You text or call a student with ONE exact thing to do in a free block.
You get the free window, the student's situation, what Timo remembers about them, and up to 5
candidates that already passed every hard rule. Choose exactly one candidate by its id.
- Prefer the ranked order unless the memory gives a clear reason to pick another.
- reason: one line saying why this item fits right now. Name the exact item, never a category.
- speech_text: 1-2 short spoken sentences that name the item and how long it takes.
Never invent items, links, or facts that are not in the input."""

EXTRACT_SYSTEM = """You extract facts a user stated about themselves in a text message.
Fill a field only when the message states it. Use null for anything not stated. Never guess."""

RESEARCH_SYSTEM = """You research a new user of Timo, an assistant that plans their day around
their commute, so it can personalize their day. You get identity hints. Use web search to find who
this person most likely is, and use the hints (email domain, school, employer, links, signature)
to tell them apart from people with the same name.

Privacy rules (strict):
- Only public, professional information: role, employer, school, program, public projects,
  skills, interests, public profile links.
- Never look for or report a home address, phone number, family, health, finances, or anything
  sensitive.
- If you are unsure, return several matches with lower match_confidence instead of guessing.

Finish with ONLY a JSON object:
{"matches": [{"summary": "<short description, e.g. CS student at UC Berkeley who builds AI agents>",
              "match_confidence": <0-1>,
              "facts": [{"key": "...", "value": "..."}],
              "sources": ["<url>", ...]}]}
Fact keys: role, employer, school, program, skills, projects, interests, links.
One fact per value; skills, projects, interests and links can repeat."""


class Choice(BaseModel):
    pick_id: str
    reason: str
    speech_text: str


class Found(BaseModel):
    key: str
    value: str


class Match(BaseModel):
    summary: str
    match_confidence: float
    facts: list[Found] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Research(BaseModel):
    matches: list[Match] = Field(default_factory=list)


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if not settings.ANTHROPIC_API_KEY:
        return None
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY, timeout=3.0, max_retries=0
        )
    return _client


def _parse(system: str, prompt: str, output_format: type[BaseModel]) -> BaseModel | None:
    client = _get_client()
    if client is None:
        return None
    try:
        response = client.messages.parse(
            model=settings.LLM_MODEL,
            max_tokens=1024,
            system=system,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
            output_format=output_format,
        )
    except anthropic.APIError as exc:
        log.warning("llm call failed: %s", type(exc).__name__)
        return None
    except ValueError as exc:
        log.warning("llm output invalid: %s", exc)
        return None
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        log.warning("llm stopped early: %s", response.stop_reason)
        return None
    return response.parsed_output


def choose(
    window: FreeWindow,
    situation: Situation,
    candidates: list[Candidate],
    profile: UserProfile,
    facts: list[Fact],
) -> Choice | None:
    prompt = json.dumps(
        {
            "window_minutes": window.minutes,
            "next_event": window.next_event_title,
            "situation": situation,
            "name": profile.name,
            "facts": {fact.key: fact.value for fact in facts},
            "reply_counts": profile.affinity,
            "candidates": [
                candidate.model_dump(mode="json", exclude={"url", "source"})
                for candidate in candidates
            ],
        }
    )
    choice = _parse(PICK_SYSTEM, prompt, Choice)
    if choice is None:
        return None
    if choice.pick_id not in {candidate.id for candidate in candidates}:
        log.warning("llm picked unknown id %s", choice.pick_id)
        return None
    return choice


def extract_facts(text: str, keys: list[str]) -> dict[str, str] | None:
    schema = create_model("Extracted", **{key: (str | None, ...) for key in keys})
    extracted = _parse(EXTRACT_SYSTEM, f"Fields: {keys}\nMessage: {text}", schema)
    if extracted is None:
        return None
    return {key: value.strip() for key, value in extracted.model_dump().items() if value}


def research(hints: dict, max_searches: int = 5) -> Research | None:
    """Who is this person? Claude with server-side web search. Slow: run it in the background."""
    global _research_client
    if not settings.ANTHROPIC_API_KEY:
        return None
    if _research_client is None:
        _research_client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY, timeout=RESEARCH_TIMEOUT, max_retries=0
        )
    # Haiku takes the basic web search tool; Sonnet and Opus take the dynamic-filtering one.
    tool = "web_search_20250305" if "haiku" in settings.LLM_MODEL else "web_search_20260209"
    messages: list[dict] = [{"role": "user", "content": "Identity hints:\n" + json.dumps(hints)}]
    try:
        for _ in range(1 + MAX_CONTINUATIONS):
            response = _research_client.messages.create(
                model=settings.LLM_MODEL,
                max_tokens=16000,
                system=RESEARCH_SYSTEM,
                output_config={"effort": "medium"},
                tools=[{"type": tool, "name": "web_search", "max_uses": max_searches}],
                messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            # The server paused its search loop; sending the turn back resumes it.
            messages.append({"role": "assistant", "content": response.content})
    except anthropic.APIError as exc:
        log.warning("research call failed: %s", type(exc).__name__)
        return None
    if response.stop_reason != "end_turn":
        log.warning("research stopped early: %s", response.stop_reason)
        return None
    text = "".join(block.text for block in response.content if block.type == "text")
    start, end = text.find("{"), text.rfind("}")
    try:
        return Research.model_validate(json.loads(text[start : end + 1]))
    except ValueError as exc:
        log.warning("research output invalid: %s", type(exc).__name__)
        return None
