"""Dry-run action proposals; calendar writes, SMS and external execution are disabled."""

import logging

from app.models import Recommendation

log = logging.getLogger(__name__)


def run_auto_actions(rec: Recommendation) -> list[str]:
    """Calendar trigger integration never executes writes, even with real reads."""
    return []


def execute(rec: Recommendation) -> str:
    log.info("would execute %s", rec.pick.title)
    print(f"would execute {rec.pick.title}")
    return f"Demo only: prepared {rec.pick.title}; no external work executed."


def prepare(rec: Recommendation) -> dict:
    return {"item_id": rec.pick.id, "title": rec.pick.title, "status": "simulated", "external_effect": False,
            "proposed_actions": list(rec.auto_actions), "summary": "Demo only; no external work executed."}
