"""Shared notification entrypoint for mocked or enabled Dial calls."""

from app.models import Recommendation
from app.agent.calling import service


def place_call(phone: str, rec: Recommendation) -> bool:
    return service().place(phone, rec)


def call_user(phone: str, script: str) -> bool:
    """Call with a briefing script; the voice agent acts mid-call through Timo's MCP tools."""
    return service().place_script(phone, script)
