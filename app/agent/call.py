"""Shared notification entrypoint for mocked or enabled Dial calls."""

from app.models import Recommendation
from app.agent.calling import service


def place_call(phone: str, rec: Recommendation) -> bool:
    return service().place(phone, rec)
