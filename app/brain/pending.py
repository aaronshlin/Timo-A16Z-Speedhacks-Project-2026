"""Pending actions: run or drop one after the user's yes or no, by text or on a call.

Anything that affects other people (sending email, accepting invites, booking) waits here until
the user says yes (rule 6). A Y by text reaches this module through runner.run and a yes on a
call through the MCP tools, so both take the same path and the same checks. Claiming the action
first means a tool call that arrives twice still runs it once.
"""

import logging
from collections.abc import Callable

from app.brain import memory, scrub, state
from app.models import PendingAction

log = logging.getLogger(__name__)


def find(phone: str, action_id: str) -> PendingAction | None:
    return next((action for action in state.get_pending(phone) if action.id == action_id), None)


def answer(phone: str, action: PendingAction, yes: bool) -> tuple[bool, str]:
    """Run or drop one pending action. Returns whether it is finished, and what to tell the user."""
    if not state.claim_pending(action.id):
        return True, "That's already taken care of."
    if action.kind == "confirm_facts":
        return True, scrub.answer(phone, action, yes)
    if not yes:
        memory.resolve_pending(action.id, "declined")
        log.info("pending phone=%s action=%s kind=%s declined", phone, action.id, action.kind)
        return True, "Okay, I won't."
    run = _RUNNERS.get(action.kind)
    done, say = run(phone, action) if run else (False, "I can't do that one yet, so I've left it for you.")
    memory.resolve_pending(action.id, "done" if done else "pending")
    log.info("pending phone=%s action=%s kind=%s done=%s", phone, action.id, action.kind, done)
    return done, say


def _send_email(phone: str, action: PendingAction) -> tuple[bool, str]:
    if action.payload.get("demo"):
        # Seeded by phone/test_user.py for the demo: there is no real draft to send.
        log.info("send_email phone=%s action=%s simulated", phone, action.id)
        return True, "Simulation finished. No email was sent."
    try:
        from app.context import gmail
    except ImportError:
        return False, "I couldn't send it: Gmail isn't connected yet, so it's still in your drafts."
    if not gmail.send_email(phone, action.payload["draft_id"]):
        return False, "The email didn't go through, so it's still in your drafts."
    return True, "Done, I sent it."


_RUNNERS: dict[str, Callable[[str, PendingAction], tuple[bool, str]]] = {
    "send_email": _send_email,
}
