# Timo - agent rules

Read this before every task. These rules apply to every AI agent working in this repo (Cursor, Claude Code, anything else).

## Context
- Hackathon build (a16z Speedrun, Oct 11 2026). Hard feature freeze at 4:00 into the build. Shipping a working demo beats clean architecture.
- Timo turns a student's free calendar blocks into one specific, executable action:
  1. It detects a free calendar block of 10 minutes or more.
  2. It picks ONE exact task or piece of content.
  3. It does the reversible actions itself.
  4. It texts (or calls) the user with the pick.
  The user can reply with one word to steer it. Timo never asks the user to decide what to do; it proposes one pick.
- Pull mode (the user calls or texts Timo through Dial) uses the same brain.

## Stack
Python 3.12, uv, FastAPI, FastMCP (streamable HTTP, mounted at /mcp), Pydantic v2, pydantic-settings, httpx, asyncio background loop, Anthropic SDK (model from settings, default `claude-haiku-5-5`), google-api-python-client, SQLite (stdlib sqlite3).
Do not add dependencies without asking.

## Repo layout and ownership
Only edit files in your own folder. To change anything else, ask its owner.
```
app/
  main.py            shared shell: includes routers, mounts MCP, lifespan. Changes go through Johnson.
  config.py          Settings (env): USE_MOCKS, NOW_OVERRIDE, POLLER_ENABLED, keys, DEMO_PHONE
  clock.py           now() - the ONLY source of current time
  models.py          Pydantic contracts - owner Adith
  phone/             owner Aaron
    dial.py          send_sms (real + mock); owns the shared Dial client and credentials
    notify.py        notify_user(phone, rec, channel) - sms here, "call" delegates to agent/call.py
    sms_handler.py   handle_reply(from_number, body)
    onboarding.py    first-text onboarding flow
    routes.py        POST /sms/inbound (Dial SMS webhook)
  context/           owner Eric
    calendar.py      get_events, create_block (real + mock)
    todoist.py       get_tasks, update_status (real + mock)
    oauth.py         Google token setup for the demo account
  brain/             owner Adith
    ranking.py       deterministic filters + sort (no LLM)
    llm.py           one structured LLM call: choose among candidates, write text
    recommend.py     recommend(...) -> Recommendation
    profile.py       get_profile, upsert_profile, record_feedback
    state.py         SQLite: fired gaps, last pick per phone, profiles
  agent/             owner Johnson
    poller.py        every 60s: find gap -> recommend -> actions -> notify
    actions.py       auto actions: block calendar, update Todoist; Darwin on "go"
    darwin.py        Darwin MCP client (real + mock)
    call.py          place_call(phone, rec) via Dial voice; speaks the pick, handles go/next by voice
    mcp_server.py    FastMCP tools: recommend, get_tasks, get_free_window, act (Dial Context MCP for calls)
    routes.py        POST /demo/tick, GET /outbox, POST /voice/* (Dial voice webhooks)
data/                owner Eric (course_index.json, mock_tasks.json, mock_calendar.json)
scripts/smoke.sh     shared
```

## Contracts (app/models.py, owner Adith)
Frozen. Do not rename or reshape without asking Adith.
- `Situation`: `audio` | `phone` | `laptop`
- `Event {id, title, start, end}`
- `Task {id, title, due: datetime|None, est_minutes: int, context: "noscreen"|"phone"|"laptop", course: str|None, url: str|None}`
- `FreeWindow {start, end, minutes, next_event_title: str|None}`
- `Candidate {kind: "task"|"content", id, title, url, minutes, source, due: datetime|None, course: str|None}`
- `Recommendation {pick: Candidate, reason, speech_text, sms_text, auto_actions: list[str]}`
- `UserProfile {phone, name: str|None, courses: list[str], channel: "sms"|"call", quiet_hours: tuple[int,int]|None, affinity: dict[str, float]}`

## Cross-track interfaces (frozen signatures)
- Aaron: `notify.notify_user(phone: str, rec: Recommendation, channel: str = "sms") -> bool`; `dial.send_sms(to, body) -> bool`; `dial.get_client()` (shared Dial auth); `sms_handler.handle_reply(from_number, body) -> str`
- Eric: `calendar.get_events(day: date) -> list[Event]`; `calendar.create_block(start, end, title) -> str`; `todoist.get_tasks() -> list[Task]`; `todoist.update_status(task_id, status: "in_progress"|"done"|"rescheduled") -> bool`
- Adith: `recommend.recommend(window: FreeWindow, situation: Situation, profile: UserProfile, exclude_ids: list[str] = []) -> Recommendation`; `profile.get_profile(phone) -> UserProfile`; `profile.upsert_profile(phone, **fields) -> UserProfile`; `profile.record_feedback(phone, rec, signal: str) -> None`; `state.get_last_pick(phone)`; `state.set_last_pick(phone, rec)`
- Johnson: `call.place_call(phone: str, rec: Recommendation) -> bool`; `poller.find_gap(now) -> FreeWindow | None`; `poller.run_tick(now=None) -> Recommendation | None`; `actions.run_auto_actions(rec) -> list[str]`; `actions.execute(rec) -> str` (on "go")

## Architecture rules
1. `recommend()` is one function with three callers: the poller, the reply handler, and the MCP tool. Never duplicate its logic.
2. Hard rules live in brain/ranking.py, not in the LLM:
   - audio allows only noscreen; phone allows noscreen and phone; laptop allows everything.
   - The item must fit the window: minutes <= window minus 3.
   - Sort by nearest due date, then by fit, then by profile affinity.
   - Pass at most 5 candidates to the LLM.
3. The LLM only picks one of the given candidates and writes the speech and SMS text. On failure, a 3s timeout, or an invalid id, fall back to the top-ranked candidate with templated text. The demo must never depend on the LLM.
4. Memory is small and explicit: onboarding answers plus counts of reply signals. No LLM-inferred personality, no conversation logs.
5. Every integration has a mock. `USE_MOCKS=1` must run the whole loop offline.
6. Never call `datetime.now()` directly. Use `clock.now()`.
7. Reversible actions (calendar block, Todoist status) run automatically. External or money actions (Darwin orders, messages to other people) only run after the user replies "go". Darwin orders stop at the review step and never complete payment.
8. External calls: httpx timeout of 5s; failures log and degrade gracefully, never crash the loop.
9. Log one line per decision: window, situation, candidates, pick, actions, channel.
10. Spoken text: 1-2 sentences. SMS: why in one line, then the link, then the reply options (go / next / done / scooter / bus / laptop). Always name the exact item, never a category.

## Out of scope (do not build)
Wake word, native app, screen watching, ambient audio, continuous location, commute inference from event locations, LLM-built user models, any web frontend, auth or multi-user. If a task seems to need one of these, stop and ask.

## Secrets
Keep all keys in `.env` (gitignored). Maintain `.env.example` with placeholder values. Never print keys in logs.

## Git and attribution (strict)
- Never add an AI tool, assistant, or agent as an author, co-author, or contributor.
- No `Co-authored-by:` trailers for Claude, Cursor, Anthropic, Copilot, or any AI.
- No "Generated with ...", "Made with ...", or similar lines in commits, PR titles or descriptions, code comments, the README, or docs.
- Commit messages: short, imperative, conventional prefix (`feat:`, `fix:`, `chore:`). Commit only when asked.
- Branch per person (`aaron/phone`, `eric/context`, `adith/brain`, `johnson/agent`). Merge to main small and often.
- A commit-msg hook in `.githooks/` strips AI trailers. Run `git config core.hooksPath .githooks` once per clone.

## Working style
- Make small diffs inside your folder. Don't refactor other folders.
- After a change, run `scripts/smoke.sh` or give the exact curl command that verifies it.
- If something conflicts with these rules or the time budget, say so before writing code.
