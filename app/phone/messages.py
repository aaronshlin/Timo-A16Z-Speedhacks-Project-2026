"""SMS formatters. sms_text is untrusted and may already include a link and a footer."""

import re

from app.models import Recommendation

LIMIT = 300
FOOTER = "Reply go / next / done / bus / scooter"
_URL = re.compile(r"https?://\S+", re.I)
_REPLY = re.compile(r"\bReply\b", re.I)
_PREFIXES = {
    "audio": "Eyes-free for the ride: ",
    "phone": "Phone-friendly for the bus: ",
    "laptop": "Laptop time: ",
    "next": "Swapped: ",
}


def _strip_url_and_reply(text: str) -> str:
    text = _URL.sub("", text)
    match = _REPLY.search(text)
    if match:
        text = text[: match.start()]
    text = text.replace(FOOTER, "")
    return " ".join(text.split()).strip(" -:;,")


def clean_reason(rec: Recommendation) -> str:
    first = ""
    for line in (rec.sms_text or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    cleaned = _strip_url_and_reply(first)
    if cleaned:
        return cleaned
    cleaned = _strip_url_and_reply(rec.reason or "")
    if cleaned:
        return cleaned
    return rec.pick.title


def _assemble(reason: str, link: str) -> str:
    reason = _strip_url_and_reply(reason)
    found = _URL.search(link or "")
    link = found.group(0) if found else ""
    tail = f"{link}\n{FOOTER}" if link else FOOTER
    if len(tail) > LIMIT:
        return tail
    budget = LIMIT - len(tail) - 1
    if budget < 1 or not reason:
        return tail
    if len(reason) > budget:
        reason = reason[:budget].rstrip()
    if not reason:
        return tail
    text = f"{reason}\n{tail}"
    return text


def format_pick(rec: Recommendation) -> str:
    return _assemble(clean_reason(rec), rec.pick.url or "")


def format_repick(rec: Recommendation, situation: str) -> str:
    prefix = _PREFIXES.get(situation, _PREFIXES["next"])
    return _assemble(prefix + clean_reason(rec), rec.pick.url or "")


def format_done(rec: Recommendation) -> str:
    return f"Done. Marked '{rec.pick.title}' complete. Reply next for another."


def format_go(summary: str) -> str:
    return _assemble(_strip_url_and_reply(summary) or summary.strip(), "")


def format_error_fallback(rec: Recommendation) -> str:
    return _assemble(f"Couldn't switch. Still on {rec.pick.title}.", rec.pick.url or "")
