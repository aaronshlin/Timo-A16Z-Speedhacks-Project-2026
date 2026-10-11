"""Tell the user about a pick. The poller calls this synchronously, so the async Dial send is run to completion before returning."""

import inspect
import logging

from app.config import settings
from app.models import Recommendation
from app.phone.dial import _run, timo_says
from app.phone.messages import format_pick

log = logging.getLogger(__name__)


async def _notify(phone: str, rec: Recommendation, channel: str) -> bool:
    from app.phone.sms_handler import canonical_user, note_fresh_pick

    who = canonical_user(phone)
    note_fresh_pick(who, rec.pick.id)

    called = False
    if channel == "call":
        try:
            from app.agent.call import place_call
            result = place_call(settings.DEMO_PHONE, rec)
            if inspect.isawaitable(result):
                result = await result
            called = bool(result)
        except Exception:
            log.exception("place_call failed; falling back to message only")

    entry = await timo_says(format_pick(rec))
    sent = entry.get("status") != "failed"

    log.info(
        "notify phone=%s channel=%s called=%s sent=%s pick=%s",
        who[-4:], channel, called, sent, rec.pick.id,
    )
    return called or sent


def notify_user(phone: str, rec: Recommendation, channel: str = "sms") -> bool:
    return _run(_notify(phone, rec, channel))