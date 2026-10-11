"""Reversible actions run immediately. Darwin stays behind an explicit go."""

import logging
from datetime import timedelta

from app.clock import now
from app.context.calendar import create_block
from app.context.todoist import update_status
from app.models import Recommendation

log = logging.getLogger(__name__)


def run_auto_actions(rec: Recommendation) -> list[str]:
    if rec.pick.kind != "task":
        return []
    ran: list[str] = []
    start = now()
    end = start + timedelta(minutes=rec.pick.minutes)
    if "calendar_block" in rec.auto_actions:
        create_block(start, end, rec.pick.title)
        ran.append("calendar_block")
    if "todoist_in_progress" in rec.auto_actions:
        update_status(rec.pick.id, "in_progress")
        ran.append("todoist_in_progress")
    return ran


def execute(rec: Recommendation) -> str:
    log.info("would execute %s", rec.pick.title)
    print(f"would execute {rec.pick.title}")
    return "would execute"
