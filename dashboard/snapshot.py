"""Dashboard snapshot: the demo day replayed through Timo's real decision code, offline.

Mocks on, LLM off (templated text), calls and SMS off. Memory and call state are copies in a
fresh temp dir, so data/timo.db and calling-mvp/.timo-state.json are never written. A section
that fails lands in "errors" and the rest still build.

    uv run python -m dashboard.snapshot                                # JSON to stdout
    uv run python -m dashboard.snapshot --out dashboard/snapshot.json
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 11)
REPLY_AFTER = timedelta(minutes=2)  # the user answers the first free-slot pick with "bus"
REPLY_WORD = "bus"
VOICE_USER = "johnson"  # the demo_sessions.json fixtures belong to this mock user
_LA = ZoneInfo("America/Los_Angeles")
_ENV = {
    "USE_MOCKS": "1",
    "LIVE_CALLS_ENABLED": "false",
    "CALL_OPT_IN": "false",
    "POLLER_ENABLED": "false",
    "SMS_ENABLED": "false",
    "MOCK_DIAL": "true",
    "ANTHROPIC_API_KEY": "",
    "NOW_OVERRIDE": "",
    "MOCK_CALENDAR_PATH": "",
    "CALL_DEMO_WINDOW_START": "",
    "CALL_DEMO_WINDOW_END": "",
    "ALLOW_DEMO_CALENDAR_CALLS": "false",
}
_ORDER = {"briefing": 0, "voice_call": 1, "free_slot": 2, "reply": 3}
_NEEDS = {"laptop": "needs a laptop", "phone": "needs a phone screen", "noscreen": "eyes-free"}
_SUPPRESSED = {
    "no_eligible_demo_session": "no voice task is still eligible; every queued task has expired",
    "no_eligible_current_gap": "no gap with 10+ usable minutes right now",
    "less_than_10_usable_minutes": "under 10 usable minutes left in the gap",
    "outside_free_window": "the gap is already over",
    "calendar_read_failed": "the calendar couldn't be read, so triggers are suppressed",
    "incomplete_calendar_snapshot": "the calendar snapshot was incomplete",
    "stale_calendar_snapshot": "the calendar snapshot was stale",
    "calendar_scope_mismatch": "the calendars checked don't match the configured ones",
    "outside_demo_calling_window": "outside the demo calling window",
}
_AUTO = ("calendar_block", "todoist_in_progress")
_DRY_CALL = "LIVE_CALLS_ENABLED=false: CallService records a dry-run session and never dials"
_DRY_SMS = "SMS_ENABLED=false: the text lands in the mock outbox"
_DRY_AUTO = "Proposed by recommend(); agent/actions.run_auto_actions is dry-run in this build and writes nothing"
_PHONES = ("DEMO_PHONE", "USER_NUMBER", "DIAL_NUMBER", "TIMO_NUMBER")
_SECRET = re.compile(r"KEY|TOKEN|SECRET")


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


def _try(errors: list[dict], section: str, fn, default=None):
    try:
        return fn()
    except Exception as exc:
        errors.append({"section": section, "error": _err(exc)})
        return default


def _t(value) -> str:
    from app.brain.briefing import say_time

    return say_time(value)


def _span(start: datetime, end: datetime) -> str:
    return f"{_t(start)}–{_t(end)}"


def _when(due: datetime | None, at: datetime) -> str:
    if due is None:
        return "no due date"
    when = f"due {_t(due)}" if due.date() == at.date() else f"due {due:%a} {_t(due)}"
    return when + (", already past" if due <= at else "")


def _before(window) -> str:
    return f"before {window.next_event_title}" if window.next_event_title else f"until {_t(window.end)}"


def _action(kind: str, label: str, status: str, reversible: bool, detail: str) -> dict:
    return {"kind": kind, "label": label, "status": status, "reversible": reversible, "detail": detail}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _mask(phone: str) -> str:
    return phone[:2] + "X" * max(0, len(phone) - 6) + phone[-4:]


def _isolate() -> Path:
    """Env and every SQLite/JSON state path point into a fresh temp dir before any app code runs."""
    tmp = Path(tempfile.mkdtemp(prefix="timo-dashboard-"))
    os.environ.update(_ENV, CALL_STATE_PATH=str(tmp / "calls.db"), CALL_ENGINE_STATE_PATH=str(tmp / "engine.json"))
    from app.config import Settings, settings

    fresh = Settings()  # the module-level settings may predate this call
    for name in Settings.model_fields:
        setattr(settings, name, getattr(fresh, name))
    from app import clock
    from app.agent import calling
    from app.brain import state
    from app.context import router

    db = ROOT / "data" / "timo.db"
    if db.exists():
        shutil.copyfile(db, tmp / "timo.db")
    state._DB = tmp / "timo.db"
    calling._service = None
    router.reset()
    clock.set_override(None)
    paths = (state._DB, Path(settings.CALL_STATE_PATH), Path(settings.CALL_ENGINE_STATE_PATH))
    if settings.LIVE_CALLS_ENABLED or settings.SMS_ENABLED or not settings.USE_MOCKS or settings.ANTHROPIC_API_KEY:
        raise RuntimeError("isolation failed: live settings still on")
    if any(tmp not in path.parents for path in paths):
        raise RuntimeError("isolation failed: a state path is outside the temp dir")
    return tmp


def _redact(snap: dict) -> dict:
    from app.config import Settings, settings

    text = json.dumps(snap, ensure_ascii=False)
    for name in Settings.model_fields:
        value = getattr(settings, name)
        if _SECRET.search(name) and isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    for name in _PHONES:
        value = getattr(settings, name)
        if value:
            text = text.replace(value, _mask(value))
    return json.loads(text)


def _channel_reason(mem, trigger: str, at: datetime, urgent: bool, channel: str | None) -> str:
    """rules.choose_channel in words, from the same inputs it reads."""
    from app.brain import rules

    if trigger == "user_message":
        return "A reply to the user's own text always goes by SMS."
    profile = mem.profile
    if rules.in_quiet_hours(profile, at):
        start, end = profile.quiet_hours
        return f"Quiet hours ({start}:00–{end}:00): " + ("urgent, so a text." if urgent else "holding.")
    sent = [item for item in mem.recent if item.direction == "out"]
    calls = sum(1 for item in sent if item.channel == "call" and item.ts.date() == at.date())
    wants_call = urgent or trigger == "pre_commute" or profile.channel == "call"
    if urgent:
        parts = ["urgent, so a call"]
    elif trigger == "pre_commute":
        parts = ["pre_commute prefers a call"]
    elif profile.channel == "call":
        parts = ["profile prefers calls"]
    else:
        parts = [f"{trigger} goes by text (profile channel is {profile.channel})"]
    if wants_call:
        missed = int(profile.affinity.get("signal:call_missed", 0))
        if rules._skips_calls(profile) and not urgent:
            answered = int(profile.affinity.get("signal:call_answered", 0))
            parts.append(f"missed {missed} calls vs {answered} answered, so no calls")
        parts.append(f"{calls} of {rules.MAX_CALLS_PER_DAY} calls used today")
    if channel == "call":
        return "; ".join(parts)
    last = next((item for item in reversed(sent) if item.proactive and item.channel == "sms"), None)
    limit = int(rules.TEXT_GAP.total_seconds() // 60)
    if last is None:
        parts.append("no proactive text in the last 24 hours")
    else:
        ago = int((at - last.ts).total_seconds() // 60)
        parts.append(f"last proactive text {ago} min ago (at most 1 per {limit} min)")
    if channel is None:
        parts.append("so it holds")
    return "; ".join(parts)


def _candidates(window, situation: str, profile, pick_id: str | None, at: datetime, exclude=()) -> list[dict]:
    """The real ranking output as picked/ranked, and every other item as filtered with its rule."""
    from app.brain import ranking

    ranked = ranking.rank_candidates(window, situation, profile, list(exclude))
    rank = {item.id: n for n, item in enumerate(ranked, 1)}
    allowed = ranking._ALLOWED[situation]
    budget = window.minutes - 3  # rank_candidates' fit rule
    rows = []
    for item in ranking._tasks() + ranking._content():
        context = "noscreen" if item.kind == "content" else ranking._task_context(item.id)
        row = {
            "id": item.id,
            "title": item.title,
            "kind": item.kind,
            "minutes": item.minutes,
            "due": _iso(item.due),
            "course": item.course,
            "context": context,
            "url": item.url,
            "source": item.source,
            "rank": rank.get(item.id),
        }
        if item.id in rank:
            row["status"] = "picked" if item.id == pick_id else "ranked"
            row["reason"] = f"{_when(item.due, at)}; {item.minutes} of {budget} min"
        elif item.id in exclude:
            row.update(status="filtered", excluded=True, reason="excluded: the pick this reply replaces")
        elif context not in allowed:
            row.update(status="filtered", reason=f"{_NEEDS[context]}; situation is {situation}")
        elif item.minutes > budget:
            row.update(status="filtered", reason=f"{item.minutes} min > {budget} min budget")
        else:
            row.update(status="filtered", reason="passed the rules; outside the top 5 sent to the picker")
        rows.append(row)
    return sorted(rows, key=lambda row: (row["rank"] is None, row["rank"] or 0))


def _queue_items(at: datetime) -> list[dict]:
    from app.agent.demo_sessions import eligible, executable
    from app.config import settings

    path = Path(settings.CALL_ENGINE_STATE_PATH)
    items = json.loads(path.read_text()).get("items", []) if path.exists() else []
    items = sorted((i for i in items if i.get("user") == VOICE_USER), key=lambda i: (i["priority"], i["id"]))
    for item in items:
        item["eligible"] = eligible(item, at)
        item["ineligible_reason"] = None
        if item["eligible"]:
            continue
        if item.get("status") != "pending" or item.get("decision") != "pending":
            item["ineligible_reason"] = f"status {item.get('status')}, decision {item.get('decision')}"
        elif not executable(item):
            item["ineligible_reason"] = "not a simulated fixture"
        elif item.get("expires_at") and at >= datetime.fromisoformat(item["expires_at"]):
            item["ineligible_reason"] = f"expired at {_t(item['expires_at'])}"
        else:
            item["ineligible_reason"] = "not eligible"
    return items


class Replay:
    """Steps the clock through the day; each proactive decision sees the outreach before it."""

    def __init__(self, phone: str, day: date, errors: list[dict]):
        from app import clock
        from app.brain import memory, planner
        from app.config import settings
        from app.context import calendar

        self.phone, self.day, self.errors, self.settings = phone, day, errors, settings
        self.start = datetime.combine(day, time.min, _LA)
        clock.set_override(self.start.isoformat())
        base = memory.load(phone)
        # Profile, facts and pending from data/timo.db, plus what was logged before this day. The
        # replay then adds its own outreach, so a later test run's log can't leak into the morning.
        self.base = base.model_copy(update={"recent": [i for i in base.recent if i.ts < self.start]})
        self.name = base.profile.name or "the user"
        self.facts = {fact.key: fact for fact in base.facts}
        self.events = calendar.get_events(day)
        self.planned_events = planner.events(day)
        self.trips = planner.trips(self.planned_events, planner.user_facts(phone))
        self.log = []
        self.timeline: list[dict] = []
        self.first_pick = None

    def memory_at(self, at: datetime):
        recent = [i for i in self.base.recent + self.log if at - timedelta(hours=24) <= i.ts <= at]
        return self.base.model_copy(update={"recent": recent})

    def remember(self, at: datetime, channel: str, text: str, direction: str = "out", proactive: bool = True, pick_id=None):
        from app.models import Interaction

        self.log.append(
            Interaction(ts=at, direction=direction, channel=channel, text=text, proactive=proactive, pick_id=pick_id)
        )

    def fact_line(self, key: str) -> str | None:
        fact = self.facts.get(key)
        return f"Memory: {key} = {fact.value} ({fact.source}, {fact.confidence})" if fact else None

    def gaps(self) -> list:
        from app.agent.poller import compute_gap

        start = datetime.combine(self.day, time.min, _LA) + timedelta(hours=self.settings.CALENDAR_ACTIVE_START_HOUR)
        seen, found = set(), []
        for at in sorted({start, *(event.end for event in self.events)}):
            window = compute_gap(at, self.events)
            if window is not None and (window.start, window.end) not in seen:
                seen.add((window.start, window.end))
                found.append(window)
        return found

    def add(self, at: datetime, kind: str, **fields) -> dict:
        entry = {
            "at": at.isoformat(),
            "kind": kind,
            "candidates": [],
            "actions": [],
            "messages": [],
            "pending": [],
            **fields,
        }
        self.timeline.append(entry)
        return entry

    def briefing(self, item) -> None:
        from app import clock
        from app.brain import briefing, memory, planner, rules

        at = item.at
        clock.set_override(at.isoformat())
        event = planner.enrich(item.trigger)
        payload = event.payload
        mem = self.memory_at(at)
        plan = briefing.plan(event)
        channel = rules.choose_channel(mem, event.type, at) if plan else None
        trip = next((t for t in self.trips if t.event.id == payload.get("event_id")), None)
        _, how = planner.travel(self.facts["commute_mode"].value if "commute_mode" in self.facts else None)
        context = [f"Calendar: {len(self.events)} events today (mock calendar)"]
        context += [line for key in ("home", "weekday_destination", "commute_mode", "leave_time") if (line := self.fact_line(key))]
        context.append(
            f"Planner: arrive {int(planner.BUFFER.total_seconds() // 60)} min early, reach out "
            f"{int(planner.LEAD.total_seconds() // 60)} min before leaving"
        )
        offers = briefing.offers(mem)
        context.append(f"Pending asks to offer: {len(offers)}")
        text = briefing.text(self.phone, event, ask=channel != "call") if plan else ""
        outcome = "nothing" if not plan else ("hold" if channel is None else "reach_out")
        entry = self.add(
            at,
            "briefing",
            trigger={
                "type": event.type,
                "label": f"Pre-commute: {payload.get('title')} at {_t(payload.get('start', ''))}",
                "payload": payload,
            },
            context=context,
            decision={
                "outcome": outcome,
                "channel": channel,
                "planner_channel": item.channel,
                "channel_reason": _channel_reason(mem, event.type, at, False, channel) if plan else "no trip to plan",
                "summary": plan.split(". ")[0].rstrip(".") if plan else "Nothing to plan",
            },
            trip={
                "event_title": payload.get("title"),
                "origin": trip.origin if trip else None,
                "destination": payload.get("destination"),
                "minutes": payload.get("travel_minutes"),
                "depart_at": payload.get("depart_at"),
                "leave_by": _t(payload["depart_at"]) if payload.get("depart_at") else None,
                "mode": how,
                "late_minutes": trip.late_minutes if trip else 0,
            },
            text=text,
            script=briefing.script(self.phone, event) if channel == "call" else None,
            code_path="brain/planner.day_plan -> router.dispatch -> brain/runner._brief",
            fired_by="POST /demo/trigger (pre_commute); day_plan sets the time, no scheduler sends it yet",
        )
        if channel is None:
            return
        if channel == "call":
            entry["actions"].append(_action("call", f"Call {self.name} with the plan", "simulated", False, _DRY_CALL))
        else:
            entry["actions"].append(_action("sms", f"Text {self.name} the plan", "simulated", False, _DRY_SMS))
            # A text asks about the latest pending action only (briefing._ask).
            latest = memory.latest_pending(self.phone)
            offers = [latest] if latest is not None and latest.kind != "confirm_facts" else []
        entry["messages"].append({"channel": channel, "direction": "out", "text": text})
        entry["pending"] = [{"id": a.id, "kind": a.kind, "summary": a.summary} for a in offers]
        self.remember(at, channel, text)

    def voice_call(self, window) -> None:
        from app import clock
        from app.agent import poller
        from app.agent.calling import service

        at = window.start
        clock.set_override(at.isoformat())
        result = poller.run_tick_detailed(at.isoformat())
        session = service().get_session(result["sessionId"]) if result.get("sessionId") else None
        fixture = (session or {}).get("fixture")
        accepted = result.get("disposition") == "accepted" and fixture is not None
        queue = _queue_items(at)
        gap = result.get("gap") or {}
        usable = gap.get("usable_minutes_remaining", window.minutes)
        s = self.settings
        if accepted:
            reason = (
                "A calendar gap starts a voice task call through the poller and CallService, not brain/rules. "
                f"Dry run (LIVE_CALLS_ENABLED=false): the session is stored as {result.get('status')} and never "
                f"dialed. Live, it would also need call opt-in, no quiet hours, no active call, the "
                f"{s.CALL_SESSION_LIMIT}-session budget and a {s.CALL_COOLDOWN_SECONDS}s cooldown."
            )
            summary = f"Voice session: {fixture['title']} (dry run, not dialed)"
        else:
            code = result.get("reason") or result.get("disposition")
            reason = f"Suppressed ({code}): {_SUPPRESSED.get(code, code)}."
            summary = f"No call: {_SUPPRESSED.get(code, code)}"
        eligible = [item for item in queue if item["eligible"]]
        entry = self.add(
            at,
            "voice_call",
            trigger={
                "type": "calendar_gap",
                "label": f"Calendar gap {_span(window.start, window.end)}: {usable} usable min {_before(window)}",
                "payload": {
                    "start": _iso(window.start),
                    "end": _iso(window.end),
                    "usable_minutes": usable,
                    "buffer_minutes": s.CALENDAR_BUFFER_MINUTES,
                    "next_event_title": window.next_event_title,
                    "disposition": result.get("disposition"),
                    "reason": result.get("reason"),
                    "session_status": result.get("status"),
                    "mode": result.get("mode"),
                },
            },
            context=[
                f"Calendar: {result.get('events_count', len(self.events))} events, snapshot complete (mock calendar)",
                f"Gap: {usable} usable min after the {s.CALENDAR_BUFFER_MINUTES}-min buffer (needs 10+)",
                f"Voice queue: {len(eligible)} of {len(queue)} tasks eligible at {_t(at)}",
                "Calls: off (LIVE_CALLS_ENABLED=false), so any session is a dry run",
            ],
            decision={
                "outcome": "reach_out" if accepted else "nothing",
                "channel": "call" if accepted else None,
                "channel_reason": reason,
                "summary": summary,
            },
            session={"id": result.get("sessionId"), "status": result.get("status"), "mode": result.get("mode")},
            code_path="agent/poller.run_tick_detailed -> calendar_trigger.trigger_calendar_gap -> calling.CallService.submit",
            fired_by="agent/poller every 60s when POLLER_ENABLED (off here, so replayed)",
        )
        picked = fixture["id"] if accepted else None
        rank = 1
        for item in queue:
            row = {
                "id": item["id"],
                "title": item["title"],
                "kind": "voice_task",
                "minutes": None,
                "due": None,
                "expires_at": item.get("expires_at"),
                "course": None,
                "context": item.get("source"),
                "tool": item.get("action", {}).get("tool"),
                "screen_minutes_saved": item.get("estimated_screen_minutes_saved"),
                "priority": item["priority"],
            }
            if item["id"] == picked:
                row.update(status="picked", rank=1, reason=f"first eligible by priority ({item['priority']})")
            elif item["eligible"] and accepted:
                rank += 1
                row.update(status="ranked", rank=rank, reason="in this call's catalog if the user asks for more")
            elif item["eligible"]:
                row.update(status="filtered", rank=None, reason="eligible, but the call was suppressed")
            else:
                row.update(status="filtered", rank=None, reason=item["ineligible_reason"] or "not eligible")
            entry["candidates"].append(row)
        if not accepted:
            return
        entry["actions"] = [
            _action("call", f"Call about {fixture['title']}", "simulated", False,
                    f"Dry run: session stored as {result.get('status')}, never dialed"),
            _action("simulated_action", f"{fixture['title']}: {fixture['action']['tool']}", "pending_confirmation", False,
                    "Runs only after a clear yes to the task's confirmation on the call; execution_mode is simulate"),
        ]
        entry["messages"] = [{"channel": "call", "direction": "out", "text": fixture["say"]}]
        entry["pending"] = [{"id": fixture["id"], "kind": fixture["action"]["tool"], "summary": fixture["confirmation"]}]
        entry["screen_minutes_saved"] = fixture.get("estimated_screen_minutes_saved")

    def free_slot(self, window) -> None:
        from app import clock
        from app.agent import poller
        from app.brain import rules
        from app.brain.recommend import recommend
        from app.phone.messages import format_pick

        at = window.start
        clock.set_override(at.isoformat())
        window = poller.find_gap(at) or window  # runner._window with no window in the payload
        situation = "laptop"  # runner._situation default: the trigger names none
        mem = self.memory_at(at)
        channel = rules.choose_channel(mem, "free_slot", at)
        budget = window.minutes - 3
        entry = self.add(
            at,
            "free_slot",
            trigger={
                "type": "free_slot",
                "label": f"Free slot {_span(window.start, window.end)}: {window.minutes} usable min {_before(window)}",
                "payload": {"window": window.model_dump(mode="json")},
            },
            context=[
                f"Calendar gap: {_span(window.start, window.end)}, {window.minutes} usable min",
                f"Situation: {situation} (runner default when the trigger names none)",
                f"Budget: {budget} min (window minus 3)",
                f"Memory: profile channel = {mem.profile.channel}; affinity: {mem.profile.affinity or 'none'}",
                "LLM: off, so the top-ranked item and templated text are used",
            ],
            decision={
                "outcome": "hold" if channel is None else "reach_out",
                "channel": channel,
                "channel_reason": _channel_reason(mem, "free_slot", at, False, channel),
                "summary": "" if channel else ("Held: quiet hours" if rules.in_quiet_hours(mem.profile, at)
                                               else "Held: one proactive text per hour"),
            },
            situation=situation,
            code_path="router.dispatch -> brain/runner.run -> brain/recommend.recommend",
            fired_by="POST /demo/trigger (free_slot); no scheduler emits it yet",
        )
        if channel is None:
            return
        try:
            rec = recommend(window, situation, mem.profile)
        except RuntimeError:
            entry["decision"].update(outcome="nothing", channel=None, summary=f"Nothing fits {window.minutes} min")
            return
        entry["candidates"] = _candidates(window, situation, mem.profile, rec.pick.id, at)
        entry["decision"]["summary"] = f"{rec.pick.title}, about {rec.pick.minutes} min"
        entry["recommendation"] = rec.model_dump(mode="json", include={"reason", "speech_text", "sms_text", "auto_actions"})
        sms = format_pick(rec)
        if channel == "call":  # notify_user calls and also texts
            entry["actions"].append(_action("call", f"Call {self.name} with the pick", "simulated", False, _DRY_CALL))
            entry["messages"].append({"channel": "call", "direction": "out", "text": rec.speech_text})
        entry["actions"].append(_action("sms", f"Text {self.name} the pick", "simulated", False, _DRY_SMS))
        entry["messages"].append({"channel": "sms", "direction": "out", "text": sms})
        end = at + timedelta(minutes=rec.pick.minutes)
        labels = {
            "calendar_block": f"Block {_span(at, end)} on calendar for {rec.pick.title}",
            "todoist_in_progress": f"Mark '{rec.pick.title}' in progress in Todoist",
        }
        entry["actions"] += [_action(kind, labels.get(kind, kind), "simulated", True, _DRY_AUTO) for kind in rec.auto_actions]
        self.remember(at, channel, rec.speech_text if channel == "call" else rec.sms_text, pick_id=rec.pick.id)
        if self.first_pick is None:
            self.first_pick = (at, rec)

    def reply(self) -> None:
        from app import clock
        from app.agent import poller
        from app.brain import rules
        from app.brain.recommend import recommend
        from app.phone.messages import format_repick
        from app.phone.sms_handler import _SITUATIONS

        if self.first_pick is None:
            raise RuntimeError("no free-slot pick to reply to")
        picked_at, last = self.first_pick
        at = picked_at + REPLY_AFTER
        clock.set_override(at.isoformat())
        situation = _SITUATIONS[REPLY_WORD]
        window = poller.find_gap(at, ignore_fired=True)
        if window is None:
            raise RuntimeError("the gap closed before the reply")
        mem = self.memory_at(at)
        channel = rules.choose_channel(mem, "user_message", at)
        exclude = [last.pick.id]  # notify_user's note_fresh_pick: the pick just sent
        rec = recommend(window, situation, mem.profile, exclude_ids=exclude)
        text = format_repick(rec, situation)
        self.remember(at, "sms", REPLY_WORD, direction="in", proactive=False)
        self.add(
            at,
            "reply",
            trigger={
                "type": "user_message",
                "label": f'{self.name} replied "{REPLY_WORD}"',
                "payload": {"body": REPLY_WORD, "in_reply_to": last.pick.id},
            },
            context=[
                f'Reply: "{REPLY_WORD}" means situation {situation} (sms_handler)',
                f"Gap: {_span(window.start, window.end)}, {window.minutes} usable min left",
                f"Budget: {window.minutes - 3} min (window minus 3)",
                f"Excluded: {last.pick.title} (the pick it replaces)",
            ],
            decision={
                "outcome": "reach_out",
                "channel": channel,
                "channel_reason": _channel_reason(mem, "user_message", at, False, channel),
                "summary": f"Swapped to {rec.pick.title}, about {rec.pick.minutes} min",
            },
            candidates=_candidates(window, situation, mem.profile, rec.pick.id, at, exclude),
            actions=[
                _action("feedback", f"Count reply signal '{REPLY_WORD}' on {last.pick.title}", "done", True,
                        f"profile.record_feedback adds signal:{REPLY_WORD}; ranking reads course affinity, so the order is unchanged"),
                _action("sms", f"Text {self.name} the new pick", "simulated", False, _DRY_SMS),
            ],
            messages=[
                {"channel": "sms", "direction": "in", "text": REPLY_WORD},
                {"channel": "sms", "direction": "out", "text": text},
            ],
            situation=situation,
            replaces=last.pick.id,
            recommendation=rec.model_dump(mode="json", include={"reason", "speech_text", "sms_text", "auto_actions"}),
            code_path="router.dispatch -> brain/runner.run -> phone/sms_handler._recommend_reply -> recommend",
            fired_by="inbound SMS webhook",
        )

    def run(self) -> None:
        from app.brain import planner

        jobs = []
        plan = _try(self.errors, "timeline.briefing", lambda: planner.day_plan(self.phone, self.day), [])
        jobs += [(item.at, "briefing", item) for item in plan]
        gaps = _try(self.errors, "timeline.gaps", self.gaps, [])
        jobs += [(window.start, kind, window) for window in gaps for kind in ("voice_call", "free_slot")]
        replied = False
        for at, kind, item in sorted(jobs, key=lambda job: (job[0], _ORDER[job[1]])):
            _try(self.errors, f"timeline.{kind}@{_t(at)}", lambda: getattr(self, kind)(item))
            if kind == "free_slot" and self.first_pick is not None and not replied:
                replied = True
                _try(self.errors, "timeline.reply", self.reply)
        self.timeline.sort(key=lambda e: (e["at"], _ORDER[e["kind"]]))
        for n, entry in enumerate(self.timeline, 1):
            entry["id"] = f"t{n:02d}"


def _mode(replay: Replay | None, ingested_at: str | None) -> dict:
    from app.config import settings

    return {
        "use_mocks": settings.USE_MOCKS,
        "live_calls_enabled": settings.LIVE_CALLS_ENABLED,
        "sms_enabled": settings.SMS_ENABLED,
        "mock_dial": settings.MOCK_DIAL,
        "poller_enabled": settings.POLLER_ENABLED,
        "calendar_source": "mock_calendar" if settings.USE_MOCKS else "google_calendar",
        "external_execution": "disabled",
        "llm": settings.LLM_MODEL if settings.ANTHROPIC_API_KEY else "fallback",
        "replay": {
            "day": replay.day.isoformat() if replay else None,
            "state": "temp copies of data/timo.db and the call stores, deleted after the run",
            "memory": "profile, facts and pending from data/timo.db plus interactions before the day; then the replay's own outreach",
            "voice_queue_ingested_at": ingested_at,
            "reply": f'"{REPLY_WORD}", {int(REPLY_AFTER.total_seconds() // 60)} min after the first free-slot pick',
        },
    }


def _user(phone: str) -> dict:
    from app import clock
    from app.brain import memory

    clock.set_override(None)
    mem = memory.load(phone)
    profile = mem.profile
    return {
        "phone": phone,
        "name": profile.name,
        "channel": profile.channel,
        "quiet_hours": list(profile.quiet_hours) if profile.quiet_hours else None,
        "courses": profile.courses,
        "facts": [fact.model_dump(mode="json") for fact in mem.facts],
        "affinity": profile.affinity,
        "recent": [item.model_dump(mode="json") for item in mem.recent],
        "recent_source": "data/timo.db, last 24 hours (logged before this snapshot, not part of the replay)",
        "pending": [
            {"id": a.id, "kind": a.kind, "summary": a.summary, "created_at": _iso(a.created_at)} for a in mem.pending
        ],
    }


def _day(replay: Replay) -> dict:
    from app.brain import planner
    from app.config import settings

    day = replay.day
    _, how = planner.travel(replay.facts["commute_mode"].value if "commute_mode" in replay.facts else None)
    gaps = replay.gaps()
    return {
        "date": day.isoformat(),
        "label": f"{day:%A %b} {day.day}",
        "active_hours": {
            "start": f"{settings.CALENDAR_ACTIVE_START_HOUR:02d}:00",
            "end": f"{settings.CALENDAR_ACTIVE_END_HOUR:02d}:00",
        },
        "buffer_minutes": settings.CALENDAR_BUFFER_MINUTES,
        "events": [event.model_dump(mode="json") for event in replay.events],
        "gaps": [
            {
                "start": _iso(w.start),
                "end": _iso(w.end),
                "minutes": int((w.end - w.start).total_seconds() // 60),
                "usable_minutes": w.minutes,
                "next_event_title": w.next_event_title,
            }
            for w in gaps
        ],
        "trips": [
            {
                "event_title": trip.event.title,
                "origin": trip.origin,
                "destination": trip.destination,
                "minutes": trip.minutes,
                "depart_at": _iso(trip.depart_at),
                "mode": how,
                "after": trip.after,
                "late_minutes": trip.late_minutes,
                "outreach_at": _iso(trip.depart_at - planner.LEAD),
            }
            for trip in replay.trips
        ],
    }


def _voice_queue(as_of: datetime, timeline: list[dict]) -> dict:
    from app import clock
    from app.agent.conversation import MAX_ACTIONS
    from app.agent.engine import Engine
    from app.config import settings

    clock.set_override(as_of.isoformat())
    path = Path(settings.CALL_ENGINE_STATE_PATH)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # As calling-mvp/preview_sessions.py: a disabled Engine with no HTTP client.
        first = Engine({"LIVE_CALLS_ENABLED": "false"}, path, client=None).next_pending(VOICE_USER)
    picked: dict[str, list[str]] = {}
    for entry in timeline:
        for row in entry["candidates"] if entry["kind"] == "voice_call" else []:
            if row["status"] == "picked":
                picked.setdefault(row["id"], []).append(entry["at"])
    keys = ("id", "priority", "title", "source", "context", "say", "confirmation", "action", "receipt",
            "expires_minutes", "created_at", "expires_at", "decision", "status", "estimated_screen_minutes_saved",
            "eligible", "ineligible_reason")
    return {
        "as_of": as_of.isoformat(),
        "user": VOICE_USER,
        "first_pick_id": first["id"] if first else None,
        "max_actions_per_call": MAX_ACTIONS,
        "items": [{**{key: item.get(key) for key in keys}, "picked_at": picked.get(item["id"], [])} for item in _queue_items(as_of)],
    }


def _rules() -> list[dict]:
    from app.agent.conversation import MAX_ACTIONS
    from app.brain import planner, rules
    from app.config import settings

    text_gap = int(rules.TEXT_GAP.total_seconds() // 60)
    modes = ", ".join(f"{how.removeprefix('by ')} {minutes}" for _, minutes, how in planner._MODES)
    return [
        {"name": "Text vs call", "source": "app/brain/rules.py",
         "detail": f"Call for a pre-commute briefing, an urgent trigger, or a user who prefers calls; at most "
                   f"{rules.MAX_CALLS_PER_DAY} calls a day unless urgent. Everything else is a text."},
        {"name": "Text rate limit", "source": "app/brain/rules.py",
         "detail": f"At most one proactive text per {text_gap} minutes; a non-urgent reach-out inside that window holds."},
        {"name": "Quiet hours", "source": "app/brain/rules.py",
         "detail": "Nothing during the user's quiet hours; an urgent trigger goes out as a text."},
        {"name": "Missed calls", "source": "app/brain/rules.py",
         "detail": "After 2+ missed calls (more missed than answered), Timo stops calling unless it's urgent."},
        {"name": "Replies", "source": "app/brain/rules.py", "detail": "A reply to the user's own text always goes by SMS."},
        {"name": "Own changes", "source": "app/brain/rules.py",
         "detail": "Calendar events Timo created itself never wake the agent."},
        {"name": "Yes / no", "source": "app/brain/rules.py + runner.py",
         "detail": "Y, yes, yep, sure... is a yes and N, no, nope... a no; it answers only the latest pending action."},
        {"name": "Leave-by", "source": "app/brain/planner.py",
         "detail": f"Leave at event start minus travel minus {int(planner.BUFFER.total_seconds() // 60)} min; Timo reaches "
                   f"out {int(planner.LEAD.total_seconds() // 60)} min before that. One trip per change of place; video calls, "
                   "all-day events and Timo's own blocks don't count."},
        {"name": "Travel time", "source": "app/brain/planner.py",
         "detail": f"Fixed minutes per commute mode: {modes}; {planner.DEFAULT_TRAVEL} if unknown."},
        {"name": "Free gaps", "source": "app/agent/poller.py compute_gap",
         "detail": f"Only between {settings.CALENDAR_ACTIVE_START_HOUR}:00 and {settings.CALENDAR_ACTIVE_END_HOUR}:00; a gap "
                   f"needs 10+ usable minutes after a {settings.CALENDAR_BUFFER_MINUTES}-min buffer; overlapping events merge."},
        {"name": "Situation filter", "source": "app/brain/ranking.py",
         "detail": "audio allows eyes-free only; phone allows eyes-free and phone; laptop allows everything."},
        {"name": "Fit and order", "source": "app/brain/ranking.py",
         "detail": "An item must fit the window minus 3 minutes. Sort by nearest due date, then tightest fit, then "
                   "affinity; the top 5 go to the picker."},
        {"name": "Picker", "source": "app/brain/recommend.py + llm.py",
         "detail": "The LLM picks one of those 5 and writes the text; on failure, a 3s timeout or a bad id, the #1 "
                   "item with templated text."},
        {"name": "Reversible actions", "source": "app/brain/recommend.py + agent/actions.py",
         "detail": "A task pick proposes a calendar block and a Todoist in-progress mark; this build runs them as a dry run."},
        {"name": "Ask first", "source": "app/brain/pending.py",
         "detail": "Anything that affects other people (email, invites, bookings) waits as a pending action until the user says yes."},
        {"name": "Voice tasks", "source": "app/agent/demo_sessions.py + calling.py",
         "detail": "A calendar gap opens a voice conversation on the first eligible task by priority; expired tasks "
                   "are skipped and every task action is simulated."},
        {"name": "Approvals per call", "source": "app/agent/conversation.py + engine.py",
         "detail": f"At most {MAX_ACTIONS} task approvals per call, each needing a clear yes to that task's confirmation."},
        {"name": "Live call gate", "source": "app/agent/calling.py",
         "detail": f"A live call needs opt-in, no quiet hours, no active call, a {settings.CALL_SESSION_LIMIT}-session "
                   f"budget and a {settings.CALL_COOLDOWN_SECONDS}s cooldown. Off here."},
    ]


def _stats(timeline: list[dict]) -> dict:
    out = [e for e in timeline if e["kind"] != "reply" and e["decision"]["outcome"] == "reach_out"]
    return {
        "reachouts": len(out),
        "calls": sum(1 for e in out if e["decision"]["channel"] == "call"),
        "texts": sum(1 for e in out if e["decision"]["channel"] == "sms"),
        "replies": sum(1 for e in timeline if e["kind"] == "reply"),
        "auto_actions": sum(1 for e in timeline for a in e["actions"] if a["kind"] in _AUTO),
        "pending_asks": sum(len(e["pending"]) for e in timeline),
        "screen_minutes_saved": sum(e.get("screen_minutes_saved") or 0 for e in timeline),
        "held": sum(1 for e in timeline if e["decision"]["outcome"] == "hold"),
        "nothing": sum(1 for e in timeline if e["decision"]["outcome"] == "nothing"),
    }


def _ingested_at() -> datetime | None:
    """When the replay first loaded the voice queue; its TTLs count from here."""
    from app.config import settings

    path = Path(settings.CALL_ENGINE_STATE_PATH)
    created = [item["created_at"] for item in json.loads(path.read_text())["items"]] if path.exists() else []
    return datetime.fromisoformat(min(created)) if created else None


def _build(day: date, errors: list[dict]) -> dict:
    from app import clock
    from app.config import settings

    phone = settings.DEMO_PHONE
    snap: dict = {"generated_at": clock.now().isoformat()}
    replay = _try(errors, "replay", lambda: Replay(phone, day, errors))
    snap["user"] = _try(errors, "user", lambda: _user(phone))
    snap["day"] = _try(errors, "day", lambda: _day(replay)) if replay else None
    if replay:
        _try(errors, "timeline", replay.run)
    timeline = replay.timeline if replay else []
    snap["timeline"] = timeline
    as_of = _try(errors, "voice_queue.as_of", _ingested_at) or datetime.combine(
        day, time(settings.CALENDAR_ACTIVE_START_HOUR), _LA
    )
    snap["voice_queue"] = _try(errors, "voice_queue", lambda: _voice_queue(as_of, timeline))
    snap["mode"] = _try(errors, "mode", lambda: _mode(replay, as_of.isoformat()))
    snap["rules"] = _try(errors, "rules", _rules, [])
    snap["stats"] = _try(errors, "stats", lambda: _stats(timeline))
    snap["errors"] = errors
    return snap


def build(day: date = DAY) -> dict:
    errors: list[dict] = []
    try:
        tmp = _isolate()
    except Exception as exc:
        return {"generated_at": None, "timeline": [], "errors": [{"section": "isolation", "error": _err(exc)}]}
    from app import clock

    try:
        with contextlib.redirect_stdout(sys.stderr):  # stray prints must not corrupt the JSON
            snap = _build(day, errors)
        return _redact(snap)
    finally:
        clock.set_override(None)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Replay the demo day for the dashboard, offline and side-effect free.")
    parser.add_argument("--out", type=Path, help="write the JSON here instead of stdout")
    parser.add_argument("--day", type=date.fromisoformat, default=DAY, help="YYYY-MM-DD, default 2026-10-11")
    args = parser.parse_args(argv)
    out = args.out.resolve() if args.out else None
    os.chdir(ROOT)  # settings read .env relative to the working directory
    text = json.dumps(build(args.day), indent=2, ensure_ascii=False)
    if out:
        out.write_text(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
