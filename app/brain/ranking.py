"""Deterministic filters and sort. No LLM."""

import json
from datetime import datetime
from pathlib import Path

from app.context.todoist import get_tasks
from app.models import Candidate, FreeWindow, Situation, UserProfile

_INDEX = Path(__file__).resolve().parents[2] / "data" / "course_index.json"
_ALLOWED = {
    "audio": {"noscreen"},
    "phone": {"noscreen", "phone"},
    "laptop": {"noscreen", "phone", "laptop"},
}


def _content() -> list[Candidate]:
    raw = json.loads(_INDEX.read_text())
    return [
        Candidate(
            kind="content",
            id=item["id"],
            title=item["title"],
            url=item["url"],
            minutes=item["minutes"],
            source="course_index",
            due=None,
            course=item.get("course"),
        )
        for item in raw
    ]


def _tasks() -> list[Candidate]:
    return [
        Candidate(
            kind="task",
            id=task.id,
            title=task.title,
            url=task.url,
            minutes=task.est_minutes,
            source="todoist",
            due=task.due,
            course=task.course,
        )
        for task in get_tasks()
    ]


def rank_candidates(
    window: FreeWindow,
    situation: Situation,
    profile: UserProfile,
    exclude_ids: list[str] | None = None,
) -> list[Candidate]:
    exclude = set(exclude_ids or [])
    allowed = _ALLOWED[situation]
    budget = window.minutes - 3
    pool: list[Candidate] = []
    for candidate in _tasks() + _content():
        if candidate.id in exclude:
            continue
        context = "noscreen" if candidate.kind == "content" else _task_context(candidate.id)
        if context not in allowed:
            continue
        if candidate.minutes > budget:
            continue
        pool.append(candidate)

    def sort_key(candidate: Candidate) -> tuple[float, int, float]:
        due = candidate.due.timestamp() if isinstance(candidate.due, datetime) else float("inf")
        slack = budget - candidate.minutes
        affinity = float(profile.affinity.get(candidate.course or candidate.id, 0.0))
        return (due, slack, -affinity)

    pool.sort(key=sort_key)
    return pool[:5]


def _task_context(task_id: str) -> str:
    for task in get_tasks():
        if task.id == task_id:
            return task.context
    return "laptop"
