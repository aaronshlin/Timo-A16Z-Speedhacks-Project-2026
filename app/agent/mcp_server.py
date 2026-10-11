"""Dial Context MCP: tools Dial's voice agent calls during a call, all calling the same brain.

The call tools (approve_action, decline_action, text_details, save_note) are how Timo acts while
the user is still on the line. Dial sends who is on the call in X-Dial-* headers. Context MCP is
account-wide, so the test user's number sees these tools too: they only act on Timo's own calls.
"""

import logging
import re

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from app.agent.poller import find_gap
from app.brain import briefing, memory, pending
from app.brain.profile import get_profile
from app.brain.recommend import recommend as make_recommendation
from app.clock import now
from app.config import settings
from app.context.todoist import get_tasks as load_tasks
from app.brain.state import get_last_pick
from app.agent.actions import prepare
from app.phone.dial import send_sms_now

log = logging.getLogger(__name__)
mcp = FastMCP("Timo")
_E164 = re.compile(r"\+[1-9]\d{7,14}")
_REFUSED = {"ok": False, "say": "I can't do that from this call."}


def _caller(user_id: str) -> str | None:
    """Who a call tool acts for, or None to refuse.

    On a Dial call it is the other party, and only when the agent on the call is Timo's number.
    Off a call (in-process, tests) it is the user_id passed in.
    """
    headers = get_http_headers()
    agent = headers.get("x-dial-agent-number", "")
    other = headers.get("x-dial-user-number", "")
    if not (_E164.fullmatch(agent) or _E164.fullmatch(other)):
        return user_id or None
    if not settings.DIAL_NUMBER or agent != settings.DIAL_NUMBER or not _E164.fullmatch(other):
        log.warning("tool refused: agent=%s is not Timo's number", agent)
        return None
    if user_id and user_id != other:
        log.warning("tool refused: user_id does not match the caller")
        return None
    return other


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


@mcp.tool
def act() -> dict:
    """Preview a demo-only action for the current user's last pick. Never executes external work."""
    rec = get_last_pick(settings.DEMO_PHONE)
    return prepare(rec) if rec is not None else {"error": "no current pick", "external_effect": False}


@mcp.tool
def approve_action(user_id: str, action_id: str) -> dict:
    """The user just said yes to an offer on this call. Runs it now; read back the say text."""
    phone = _caller(user_id)
    action = pending.find(phone, action_id) if phone else None
    if action is None:
        log.info("tool=approve_action user=%s action=%s ok=False", phone, action_id)
        return _REFUSED if phone is None else {"ok": False, "say": "That one's already taken care of."}
    done, say = pending.answer(phone, action, True)
    log.info("tool=approve_action user=%s action=%s ok=%s", phone, action_id, done)
    return {"ok": done, "say": say}


@mcp.tool
def decline_action(user_id: str, action_id: str) -> dict:
    """The user just said no to an offer on this call. Drops it; read back the say text."""
    phone = _caller(user_id)
    action = pending.find(phone, action_id) if phone else None
    if action is None:
        log.info("tool=decline_action user=%s action=%s ok=False", phone, action_id)
        return _REFUSED if phone is None else {"ok": False, "say": "That one's already taken care of."}
    done, say = pending.answer(phone, action, False)
    log.info("tool=decline_action user=%s action=%s ok=%s", phone, action_id, done)
    return {"ok": done, "say": say}


@mcp.tool
def text_details(user_id: str) -> dict:
    """The user wants this call's plan by text. Sends it now."""
    phone = _caller(user_id)
    text = briefing.details(phone) if phone else None
    if text is None:
        return _REFUSED if phone is None else {"ok": False, "say": "I don't have anything to send yet."}
    sent = send_sms_now(phone, text)
    if sent:
        memory.log_interaction(phone, "out", "sms", text)
    log.info("tool=text_details user=%s ok=%s", phone, sent)
    return {"ok": sent, "say": "Texted you the details." if sent else "The text didn't go through."}


@mcp.tool
def save_note(user_id: str, key: str, value: str) -> dict:
    """The user told you something about themselves or their plans. key is one of: name, home,
    weekday_destination, commute_mode, leave_time, role, employer, school, interests, links."""
    phone = _caller(user_id)
    if phone is None:
        return _REFUSED
    if key not in memory.PROFILE_KEYS or not value.strip():
        return {"ok": False, "say": "I couldn't save that one."}
    memory.remember(phone, key, value.strip(), "user_said")
    log.info("tool=save_note user=%s key=%s", phone, key)
    return {"ok": True, "say": "Got it, I'll remember that."}
