"""Mock Todoist. Replace with the real API."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.models import Task

log = logging.getLogger(__name__)
_PATH = Path(__file__).resolve().parents[2] / "data" / "mock_tasks.json"


def get_tasks() -> list[Task]:
    raw = json.loads(_PATH.read_text())
    tasks: list[Task] = []
    for item in raw:
        due = datetime.fromisoformat(item["due"]) if item.get("due") else None
        tasks.append(
            Task(
                id=item["id"],
                title=item["title"],
                due=due,
                est_minutes=item["est_minutes"],
                context=item["context"],
                course=item.get("course"),
                url=item.get("url"),
            )
        )
    return tasks


def update_status(
    task_id: str, status: Literal["in_progress", "done", "rescheduled"]
) -> bool:
    log.info("update_status task_id=%s status=%s", task_id, status)
    print(f"update_status {task_id} {status}")
    return True
