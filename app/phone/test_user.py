"""Plays the user from the second Dial number, so the loop runs without anyone's phone.

Dial won't connect a person's own phone to Timo, so the demo user is a second Dial number on the
account (DIAL_USER_NUMBER_ID; DEMO_PHONE is its E.164):
- Calls: Dial's own voice agent answers that number, using instruction() as its prompt.
- Texts: Dial doesn't answer SMS, so this module does. It texts Timo as the user and answers
  Timo's questions from PERSONA by fixed rules (no LLM), so every run goes the same way.

    uv run python -m app.phone.test_user setup        # put the persona on the user number
    uv run python -m app.phone.test_user seed         # pre-seed the profile and a drafted email
    uv run python -m app.phone.test_user text "hi"    # text Timo as the user

setup and real texts need DIAL_API_KEY in the environment; real texts also need SMS_ENABLED=1.
"""

import argparse
import re
from urllib.parse import quote

from app.brain import memory, state
from app.clock import now
from app.config import settings
from app.context.router import dispatch
from app.models import TriggerEvent
from app.phone import dial

# Fictional. Keys match the profile fact keys, so seed() can store them as they are.
PERSONA = {
    "name": "Sam",
    "home": "the Mission",
    "weekday_destination": "the Berkeley campus",
    "commute_mode": "BART",
    "leave_time": "8:15am",
    "interests": "climbing, jazz",
}
MAX_TURNS = 10
# (what Timo's text asks, which persona answer). Checked in order; None answers "Y".
_ANSWERS = [
    (r"\breply y\b", None),
    (r"\bname\b", "name"),
    (r"\bwhat time\b|\bwhen do you\b.*\bleave\b", "leave_time"),
    (r"\bcommute\b|\bget there\b|\bhow do you get\b", "commute_mode"),
    (r"\bwhere do you live\b|\bhome\b", "home"),
    (r"\bwhere\b.*\b(work|school|study|head)\b", "weekday_destination"),
]


def instruction() -> str:
    """The prompt Dial's voice agent uses when Timo calls the user number."""
    p = PERSONA
    return "\n".join(
        [
            f"You are {p['name']}, answering your own phone. The caller is Timo, your AI assistant.",
            "Talk like a real, busy person: short casual answers, one sentence at a time.",
            "If Timo asks whether now is a good time, say yes.",
            "If Timo offers to send something or do something it prepared, say yes, go ahead.",
            "Once, after Timo gives you the plan, ask it to text you the details.",
            f"If asked how you're getting there: {p['commute_mode']} to {p['weekday_destination']}, "
            f"leaving around {p['leave_time']}.",
            "Never use tools, never act as Timo, and don't make up other requests.",
            "When Timo wraps up, say thanks and goodbye.",
        ]
    )


def answer(text: str) -> str | None:
    """The user's reply to one of Timo's texts, or None to stay quiet."""
    for pattern, key in _ANSWERS:
        if re.search(pattern, text, re.I):
            return "Y" if key is None else PERSONA[key]
    return None


def setup() -> str:
    """Put the persona on the user number. Uses the number the team already has; buys nothing."""
    number_id = settings.DIAL_USER_NUMBER_ID
    if not number_id or number_id == settings.DIAL_FROM_NUMBER_ID:
        raise ValueError("Set DIAL_USER_NUMBER_ID to the second Dial number, not Timo's.")
    path = "/numbers/" + quote(number_id, safe="")
    found = dial.api("GET", path)
    if found is None:
        raise ValueError("Couldn't read the user number from Dial.")
    e164 = found.get("number", {}).get("number")
    if e164 != settings.DEMO_PHONE:
        raise ValueError("DEMO_PHONE must be the user number's E.164, so Timo calls the agent.")
    if dial.api("PATCH", path, {"inboundInstruction": instruction(), "nickname": "Timo test user"}) is None:
        raise ValueError("Dial didn't accept the persona prompt.")
    return e164


def seed() -> None:
    """Pre-seed the demo account: the persona's profile and one drafted email waiting for a yes."""
    phone = settings.DEMO_PHONE
    for key, value in PERSONA.items():
        memory.remember(phone, key, value, "user_said")
    if not any(action.payload.get("demo") for action in state.get_pending(phone)):
        memory.add_pending(
            phone,
            "send_email",
            "Send your drafted reply to Sarah saying you'll be 10 minutes late",
            {"draft_id": "demo-draft-sarah", "demo": True},
        )


def on_text(sender: str, body: str) -> None:
    """Timo texted the user number through real Dial: answer as the user if the persona can."""
    if sender != settings.DIAL_NUMBER:
        return
    reply = answer(body)
    if reply:
        dial.send_sms_now(settings.DIAL_NUMBER, reply, from_number_id=settings.DIAL_USER_NUMBER_ID)


def _first_answer(lines: list[str]) -> str | None:
    for line in lines:
        to, _, body = line.removeprefix("SMS ").partition(": ")
        if to == settings.DEMO_PHONE and (reply := answer(body)):
            return reply
    return None


def text(body: str) -> None:
    """Text Timo as the user. With mocks, Timo's replies are answered here until it goes quiet."""
    if not settings.USE_MOCKS and not (settings.SMS_ENABLED and settings.DIAL_NUMBER):
        raise ValueError("Real texts need SMS_ENABLED=1 and DIAL_NUMBER (Timo's E.164).")
    for _ in range(MAX_TURNS):
        dial.send_sms_now(settings.DIAL_NUMBER or "timo", body, from_number_id=settings.DIAL_USER_NUMBER_ID)
        if not settings.USE_MOCKS:
            return  # Dial delivers it; Timo's replies come back through the webhook to on_text
        seen = len(dial.outbox)
        dispatch(TriggerEvent(user_id=settings.DEMO_PHONE, type="user_message", payload={"body": body}, ts=now()))
        body = _first_answer(dial.outbox[seen:])
        if body is None:
            return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Play the demo user from the second Dial number.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="put the persona on the user number (buys nothing)")
    commands.add_parser("seed", help="pre-seed the demo profile and one drafted email")
    commands.add_parser("persona", help="print the persona prompt")
    commands.add_parser("text", help="text Timo as the user").add_argument("body")
    args = parser.parse_args(argv)
    if args.command == "setup":
        print("Persona set on " + setup())
    elif args.command == "seed":
        seed()
        print("Seeded the demo profile and a drafted email for " + settings.DEMO_PHONE)
    elif args.command == "persona":
        print(instruction())
    else:
        text(args.body)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error))
