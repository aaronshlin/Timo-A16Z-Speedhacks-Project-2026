"""Dial Context MCP. Three tools, all calling the same brain."""

from fastmcp import FastMCP

from app.agent.poller import find_gap
from app.brain.profile import get_profile
from app.brain.recommend import recommend as make_recommendation
from app.clock import now
from app.config import settings
from app.context.todoist import get_tasks as load_tasks

mcp = FastMCP("Timo")


@mcp.tool
def recommend() -> dict:
    """Pick one task or piece of content for the current free window."""
    window = find_gap(now(), ignore_fired=True)
    if window is None:
        return {"error": "no free window"}
    profile = get_profile(settings.DEMO_PHONE)
    rec = make_recommendation(window, "laptop", profile)
    return rec.model_dump(mode="json")


@mcp.tool
def get_tasks() -> list[dict]:
    """Return the student's tasks."""
    return [task.model_dump(mode="json") for task in load_tasks()]


@mcp.tool
def get_free_window() -> dict | None:
    """Return the free block that starts within two minutes of now."""
    window = find_gap(now(), ignore_fired=True)
    if window is None:
        return None
    return window.model_dump(mode="json")
