"""The planner: from the calendar and memory, when Timo reaches out and what it says.

A trip is a change of place: home to the first event of the day, then on to any event somewhere
else. An event's place is its location, or the user's weekday_destination when it has none, and a
video call doesn't move anyone. Leave-by is the event's start minus travel and BUFFER, and Timo
reaches out LEAD before that. Code only, no LLM, so it is also what goes out when the LLM is down
(rule 4).

Travel time is a fixed estimate per commute mode until agent/travel.py lands.

    uv run python -m app.brain.planner                     # the demo user's plan for today
    uv run python -m app.brain.planner --day 2026-10-12 --script
"""

import argparse
import logging
import re
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from app.brain import briefing, memory, rules, state
from app.clock import now
from app.config import settings
from app.context import calendar
from app.models import Channel, Event, Interaction, TriggerEvent

log = logging.getLogger(__name__)
BUFFER = timedelta(minutes=10)  # arrive this early
LEAD = timedelta(minutes=20)  # reach out this long before leaving
DEFAULT_TRAVEL = 30
# (what the commute_mode fact says, minutes per trip, how Timo says it). First match wins, so
# "bike to BART" plans for BART.
_MODES = [
    (re.compile(r"\bbart\b", re.I), 40, "by BART"),
    (re.compile(r"\bcaltrain\b", re.I), 50, "by Caltrain"),
    (re.compile(r"\b(train|muni|subway|metro|bus|transit)\b", re.I), 40, "by transit"),
    (re.compile(r"\b(drive|driving|car|uber|lyft)\b", re.I), 30, "by car"),
    (re.compile(r"\bscooter\b", re.I), 15, "by scooter"),
    (re.compile(r"\b(bike|biking|cycling)\b", re.I), 20, "by bike"),
    (re.compile(r"\b(walk|walking|on foot)\b", re.I), 20, "on foot"),
]
_VIRTUAL = re.compile(r"https?://|\bzoom\b|meet\.google|\bgoogle meet\b|\bteams\b", re.I)
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\b", re.I)
_RUNDOWN = 4


class Trip(BaseModel):
    event: Event
    origin: str
    destination: str
    minutes: int
    depart_at: datetime
    from_home: bool
    after: str | None = None  # the event that runs past the ideal leave time, so they leave as it ends
    late_minutes: int = 0


class Outreach(BaseModel):
    at: datetime
    channel: Channel | None  # None: brain/rules holds it (quiet hours or a rate limit)
    trigger: TriggerEvent


def user_facts(phone: str) -> dict[str, str]:
    return {fact.key: fact.value for fact in state.get_facts(phone)}


def travel(mode: str | None) -> tuple[int, str | None]:
    """Minutes per trip for a commute mode, and how Timo names it."""
    for pattern, minutes, how in _MODES:
        if mode and pattern.search(mode):
            return minutes, how
    return DEFAULT_TRAVEL, None


def events(day: date) -> list[Event]:
    """The day's events worth planning around. Empty when the calendar can't be read (rule 12)."""
    try:
        found = calendar.get_events(day)
    except calendar.CalendarReadError:
        log.warning("planner day=%s calendar unreadable, planning nothing", day)
        return []
    # Timo's own blocks are not plans, and an all-day event has no time to leave by.
    kept = [e for e in found if not e.title.startswith("Timo:") and e.end - e.start < timedelta(hours=20)]
    return sorted(kept, key=lambda e: e.start)


def trips(day_events: list[Event], facts: dict[str, str]) -> list[Trip]:
    """Every change of place in one day's events, in order. The first one leaves from home.

    Nobody leaves before the event they are in ends: then the trip starts as it ends, and says how
    late that makes them.
    """
    home = facts.get("home") or "home"
    minutes, _ = travel(facts.get("commute_mode"))
    place, last, planned = home, None, []
    for event in day_events:
        where = event.location or facts.get("weekday_destination")
        if where and _VIRTUAL.search(where):
            continue  # a video call doesn't move anyone
        if where and where.strip().lower() != place.strip().lower():
            depart = event.start - timedelta(minutes=minutes) - BUFFER
            after = last if last is not None and last.end > depart else None
            if after is not None:
                depart = after.end
            late = (depart + timedelta(minutes=minutes) - event.start).total_seconds() // 60
            planned.append(
                Trip(
                    event=event,
                    origin=place,
                    destination=where,
                    minutes=minutes,
                    depart_at=depart,
                    from_home=place == home,
                    after=after.title if after else None,
                    late_minutes=max(0, int(late)),
                )
            )
            place = where
        if last is None or event.end > last.end:
            last = event
    return planned


def say(trip: Trip, facts: dict[str, str]) -> str:
    """One trip as Timo says it: when to leave, what for, and how long getting there takes."""
    _, how = travel(facts.get("commute_mode"))
    leave = briefing.say_time(trip.depart_at)
    leave = f"Leave right when {trip.after} ends at {leave}" if trip.after else f"Leave at {leave}"
    line = (
        f"{leave} for {trip.event.title} at {briefing.say_time(trip.event.start)}. It's about "
        f"{trip.minutes} minutes to {trip.destination}{' ' + how if how else ''}"
    )
    line += f", so you'll be about {trip.late_minutes} minutes late." if trip.late_minutes else "."
    usual = _usual(facts.get("leave_time"), trip.depart_at) if trip.from_home else None
    if usual is not None and usual - trip.depart_at >= timedelta(minutes=10):
        early = int((usual - trip.depart_at).total_seconds() // 60)
        line += f" That's {early} minutes earlier than usual."
    return line


def rundown(day_events: list[Event]) -> str:
    if not day_events:
        return "Nothing else on your calendar today."
    items = [f"{e.title} {briefing.say_time(e.start)}" for e in day_events[:_RUNDOWN]]
    more = f", and {len(day_events) - _RUNDOWN} more" if len(day_events) > _RUNDOWN else ""
    return "Today: " + ", ".join(items) + more + "."


def enrich(event: TriggerEvent) -> TriggerEvent:
    """Fill a briefing trigger from the calendar: which trip, when to leave, what Timo says.

    A payload that already says what to do (plan or depart_at) is left as it is. No trip means no
    plan, and the runner then does nothing.
    """
    payload = event.payload
    if payload.get("plan") or payload.get("depart_at"):
        return event
    facts = user_facts(event.user_id)
    at = now()
    if event.type == "new_event":
        trip = _trip_to(str(payload.get("event_id", "")), at.date(), facts)
        planned = _payload(trip, facts) if trip else {}
    else:
        today = events(at.date())
        trip = next((t for t in trips(today, facts) if t.depart_at >= at), None)
        planned = _payload(trip, facts) if trip else {}
        if event.type == "morning_plan":
            rest = rundown([e for e in today if e.end > at])
            planned["plan"] = " ".join(part for part in (planned.get("plan"), rest) if part)
    log.info(
        "planner user=%s type=%s event=%s depart=%s",
        event.user_id,
        event.type,
        trip.event.id if trip else None,
        trip.depart_at.isoformat() if trip else None,
    )
    return event.model_copy(update={"payload": {**planned, **payload}})


def day_plan(phone: str, day: date | None = None) -> list[Outreach]:
    """When Timo reaches out on `day`: one pre_commute per trip, LEAD before leaving.

    Channels follow brain/rules as if each earlier outreach went out, so a third call in a day
    shows up as a text.
    """
    day = day or now().date()
    facts = user_facts(phone)
    mem = memory.load(phone)
    plan = []
    for trip in trips(events(day), facts):
        at = trip.depart_at - LEAD
        channel = rules.choose_channel(mem, "pre_commute", at)
        if channel is not None:
            mem.recent.append(Interaction(ts=at, direction="out", channel=channel, text="", proactive=True))
        trigger = TriggerEvent(user_id=phone, type="pre_commute", payload=_payload(trip, facts), ts=at)
        plan.append(Outreach(at=at, channel=channel, trigger=trigger))
    return plan


def preview(phone: str, day: date | None = None, script: bool = False) -> str:
    """The day's plan as Timo will carry it out: when, by call or text, and what it says.

    Offers are what is waiting for a yes right now; one approved on an earlier call is gone by
    the next.
    """
    day = day or now().date()
    mem = memory.load(phone)
    name = mem.profile.name or phone
    minutes, how = travel(user_facts(phone).get("commute_mode"))
    lines = [f"{name}'s plan for {day:%A %b} {day.day} (about {minutes} minutes a trip{' ' + how if how else ''})", ""]
    plan = day_plan(phone, day)
    if not plan:
        lines.append("No trips: nothing on the calendar needs travel, or there's no weekday_destination on file.")
    for item in plan:
        when = briefing.say_time(item.at)
        if item.channel == "call":
            lines.append(f"{when}: Timo calls {name} and says:")
            lines.append(f'  "{briefing.text(phone, item.trigger, ask=False)}"')
            lines += [f"  Then offers: {action.summary} (yes or no on the call)" for action in briefing.offers(mem)]
            if script:
                lines += ["", "  Voice agent prompt:", *("    " + line for line in briefing.script(phone, item.trigger).splitlines())]
        else:
            verb = f"Timo texts {name}" if item.channel == "sms" else "Timo holds (quiet hours or rate limit), would text"
            lines.append(f"{when}: {verb}:")
            lines.append(f'  "{briefing.text(phone, item.trigger)}"')
        lines.append("")
    return "\n".join(lines).rstrip()


def _payload(trip: Trip, facts: dict[str, str]) -> dict:
    return {
        "plan": say(trip, facts),
        "depart_at": trip.depart_at.isoformat(),
        "destination": trip.destination,
        "title": trip.event.title,
        "start": trip.event.start.isoformat(),
        "event_id": trip.event.id,
        "travel_minutes": trip.minutes,
    }


def _matches(event: Event, event_id: str) -> bool:
    # Real calendar ids are prefixed with the calendar they came from ("primary:abc").
    return bool(event_id) and (event.id == event_id or event.id.endswith(":" + event_id))


def _trip_to(event_id: str, today: date, facts: dict[str, str]) -> Trip | None:
    """The trip to a just-added event today or tomorrow. None if it needs none."""
    for day in (today, today + timedelta(days=1)):
        day_events = events(day)
        if any(_matches(e, event_id) for e in day_events):
            return next((t for t in trips(day_events, facts) if _matches(t.event, event_id)), None)
    return None


def _usual(leave_time: str | None, on: datetime) -> datetime | None:
    """The user's usual leave time (a fact like "8:15am") on the same day as `on`."""
    match = _TIME.search(leave_time or "")
    if match is None:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    suffix = (match[3] or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return on.replace(hour=hour, minute=minute, second=0, microsecond=0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Show when Timo will reach out and what it will say.")
    parser.add_argument("--phone", default=settings.DEMO_PHONE)
    parser.add_argument("--day", type=date.fromisoformat, help="YYYY-MM-DD, default today")
    parser.add_argument("--script", action="store_true", help="also print the voice agent's full prompt")
    args = parser.parse_args(argv)
    print(preview(args.phone, args.day, args.script))


if __name__ == "__main__":
    main()
