# Timo - agent rules

Read this before every task. These rules apply to every AI agent working in this repo (Cursor, Claude Code, anything else). The full product spec is the "Timo build context" doc (ask Adith for the link). This file is the working contract for the code.

## Context
- Hackathon build (a16z Speedrun, Oct 11 2026). Hard feature freeze at 4:00 into the build. One complete demo loop beats five half-built features.
- Timo is an always-on agent that plans a user's day around their commute and reaches out first, by text or call, with exactly what to do and when. Users never open an app: they text one Dial number, set up over SMS, and Timo takes it from there.
- Agentic, not reactive: Timo wakes on triggers, decides what matters, takes safe actions itself, and asks before anything that affects other people. Doing nothing is a valid outcome.
- One integration: Google only (Gmail + Calendar through one OAuth consent). Dial handles SMS and calls.
- Demo story:
  1. Text the number and onboard live.
  2. Timo replies with the profile it built.
  3. Add a calendar event on stage.
  4. Timo calls or texts with a leave-by time and a prep summary, and offers a drafted email.
  5. Reply "Y" and show the email in Drafts or Sent.

## Stack
Python 3.12, uv, FastAPI, FastMCP (streamable HTTP, mounted at /mcp), Pydantic v2, pydantic-settings, httpx, asyncio background tasks, Anthropic SDK (Claude API with tool use; model from settings `LLM_MODEL`), google-api-python-client + google-auth-oauthlib, SQLite (stdlib sqlite3; Postgres is an open decision).
Do not add dependencies without asking. A travel-time API will need one: ask first.

## Dev tooling: Alike
Alike is build tooling, not part of Timo. Timo's agent is our own code (`brain/runner.py`), with Claude called through the Anthropic SDK; nothing at runtime depends on Alike.
- Alike runs inside our coding agents (Claude Code, Codex) over MCP and hooks. It records what each session decided and why, and tells the next session before it edits a file that decision touched.
- This file stays the source of truth for contracts, ownership and rules. Approve a standing rule Alike extracts only if it matches this file.
- Alike sends whole session transcripts (prompts, responses, tool calls and results) to the team's store. Never print `.env`, paste keys, or echo secrets in an agent session.

## Repo layout and ownership
Only edit files in your own folder. To change anything else, ask its owner.
`(new)` = to build. `(legacy)` = from the student free-block skeleton: keep it running until the new path works, don't extend it.
```
app/
  main.py            shared shell: routers, MCP mount, lifespan. Changes go through Johnson.
  config.py          Settings (env). Every new key also goes in .env.example.
  clock.py           now() - the ONLY source of current time
  models.py          Pydantic contracts - owner Adith
  phone/             owner Aaron
    dial.py          send_sms (real + mock); owns the shared Dial client and credentials
    notify.py        delivery helpers behind the send_message and call_user tools
    sms_handler.py   handle_reply(from_number, body): onboarding if new, else a user_message trigger
    onboarding.py    (new) first-text onboarding over SMS
    test_user.py     (new) plays the user from the second Dial number for scripted runs
    routes.py        POST /sms/inbound (Dial SMS webhook)
  context/           owner Eric
    oauth.py         (new) consent link with signed state, callback, token storage, get_google_token
    calendar.py      events, free slots, create_event tagged source: timo, watch + renewal (real + mock)
    gmail.py         (new) search_emails, read_thread, create_draft, send_email (real + mock)
    router.py        (new) event router: every source -> TriggerEvent -> brain.runner.run
    routes.py        (new) GET /google/callback, POST /calendar/webhook
    todoist.py       (legacy)
  brain/             owner Adith
    runner.py        run(event): the one agent entry point (tool-use loop + rule-based fallback)
    llm.py           Claude calls: the tool-use loop and fact extraction
    memory.py        facts (source + confidence), interaction log, pending actions
    rules.py         text vs call, quiet hours, rate limits, ignore source: timo (no LLM)
    profile.py       get_profile, upsert_profile, record_feedback
    scrub.py         (new) profile scrub: Gmail first, then web search; staged until the user says Y
    state.py         SQLite storage for brain/
    ranking.py       (legacy) student pick filters
    recommend.py     (legacy) student pick
  agent/             owner Johnson
    mcp_server.py    FastMCP tools (table below); the runner calls the same functions in-process
    scheduler.py     (new) morning plan, ~20 min before predicted departure, free slots, fallback poll
    call.py          call_user via Dial voice (real + mock)
    travel.py        (new, owner TBD) get_travel_time
    routes.py        POST /demo/tick, GET /outbox, POST /voice/* (Dial voice webhooks)
    poller.py        (legacy) replaced by scheduler.py
    actions.py       (legacy)
    darwin.py        (legacy)
data/                owner Eric: mocks (calendar, emails) and the demo seed account
scripts/smoke.sh     shared
```
The landing page showing the phone number is the only web page besides Google's consent screen. Owner TBD.

## Contracts (app/models.py, owner Adith)
Do not rename or reshape without asking Adith.
- `TriggerEvent {user_id, type: "new_event"|"free_slot"|"pre_commute"|"morning_plan"|"user_message", payload: dict, ts}`. `user_id` is the user's phone number. Every trigger source sends this to `router.dispatch`.
  - Proposed payload keys: `new_event` {event_id, source}, `free_slot` {window}, `pre_commute` {depart_at, destination}, `user_message` {body}, `morning_plan` {}. Any type may add `urgent: bool`.
- `Event {id, title, start, end}`. Add `location` and `source` when the real calendar lands.
- `FreeWindow {start, end, minutes, next_event_title: str|None}`
- `Fact {key, value, source: "user_said"|"gmail"|"scrub"|"inferred", confidence, updated_at}`. Profile facts use keys `name`, `home`, `weekday_destination`, `commute_mode`, `leave_time`, plus role and interests.
- `Interaction {ts, direction: "out"|"in", channel, text, proactive, pick_id}`
- `PendingAction {id, kind, summary, payload, created_at}`
- `Memory {profile, facts, recent: list[Interaction], pending: list[PendingAction]}`
- `UserProfile {phone, name: str|None, channel: "sms"|"call", quiet_hours: tuple[int,int]|None, affinity: dict[str, float]}` (`courses` is legacy)
- Legacy: `Situation`, `Task`, `Candidate`, `Recommendation`.

## MCP tools (agent/mcp_server.py, owner Johnson)
Proposed signatures; Johnson confirms. Every tool takes `user_id` first and looks up that user's Google token itself. Each tool is defined once: exposed at /mcp and called in-process by the runner.

| Tool | Implemented in | What it does | Needs confirmation |
| --- | --- | --- | --- |
| `get_profile(user_id) -> Memory` | brain/memory | Profile, facts, recent interactions, pending actions | No |
| `update_profile(user_id, key, value, source, confidence)` | brain/memory | Save a fact with source and confidence | No |
| `log_interaction(user_id, direction, channel, text)` | brain/memory | Record what Timo sent and how the user replied | No |
| `list_events(user_id, start, end) -> list[Event]` | context/calendar | Read events in a time range | No |
| `get_free_slots(user_id, day) -> list[FreeWindow]` | context/calendar | Gaps of 30+ min in waking hours | No |
| `create_event(user_id, start, end, title) -> str` | context/calendar | Add a block tagged `source: timo` | No |
| `search_emails(user_id, query)` | context/gmail | Find threads by query | No |
| `read_thread(user_id, thread_id)` | context/gmail | Read one thread | No |
| `create_draft(user_id, thread_id, body) -> str` | context/gmail | Draft a reply | No |
| `send_email(user_id, draft_id)` | context/gmail | Send a draft | Yes |
| `get_travel_time(user_id, origin, destination, depart_at) -> int` | agent/travel | Minutes for a departure time | No |
| `send_message(user_id, text, urgent=False)` | phone/dial | Text the user; rate limited by brain/rules | No |
| `call_user(user_id, script, urgent=False)` | agent/call | Call the user with a script; limited by brain/rules | No |
| `finish_run(user_id, summary, learned, memories_used)` | brain/runner | End the loop and write memory | No |

## Cross-track handoffs
- Aaron: `dial.send_sms(to, body) -> bool`; `dial.get_client()`; `onboarding.handle(phone, body) -> str`; after onboarding, inbound texts become `TriggerEvent(type="user_message", payload={"body": ...})` via `router.dispatch`.
- Eric: `oauth.consent_link(phone) -> str`; `oauth.get_google_token(user_id)` (None until consent); `router.dispatch(event: TriggerEvent) -> None`.
- Johnson: the tool list above; `call.place_call(phone, script) -> bool`; the scheduler sends triggers through `router.dispatch`.
- Adith: `runner.run(event: TriggerEvent) -> str | None` (the finish_run summary; None = did nothing); `memory.*`; `rules.*`; demo test scenarios.

## Architecture rules
1. One agent entry point: `brain/runner.run(event)`. The router is its only caller; inbound SMS, the calendar webhook and the scheduler all go through the router. Never start a second agent loop.
2. A run: load memory, read context, decide, act, tell the user, save what was learned. It is a Claude API tool-use loop: send the context, the model calls tools, repeat until `finish_run`. Cap the steps (default 10 tool calls) and time, and log every tool call.
3. Hard rules live in code, not the prompt. brain/rules.py decides text vs call, quiet hours and rate limits; `send_message` and `call_user` enforce them and return a refusal the model can read. A tool that needs confirmation refuses unless the user replied Y to the matching pending action.
4. The demo must survive the LLM being down. On an LLM error, timeout or step cap, fall back to a templated message built by code (e.g. the leave-by time for `pre_commute` and `new_event`), or do nothing.
5. Memory (brain/memory.py) is read at the start of every run and written at the end.
   - Facts store a source and confidence. What the user said outranks what Timo found or inferred.
   - Scrubbed facts are staged and saved only after the user replies Y.
   - After each interaction, extract new or contradicting facts the user stated. No LLM-inferred personality.
   - Pending actions live in memory so a "Y" arriving as a fresh run can complete them.
   - Borrowed from Alike: serve a memory when it applies; keep what was ruled out and why; log which memories each run used and whether the suggestion landed; confirm a standing rule ("never call before 9am") with the user by text before adopting it.
6. Autonomy. Do freely and report as done: read, research, draft, plan, block travel time, pull notes. Ask first ("reply Y to send"): send emails, accept invites, book anything.
7. Text vs call. Call when the user is likely driving or about to leave, when something is urgent, or when there are several steps; at most 2 calls a day unless urgent; stop calling a user who keeps missing calls. Otherwise text, at most one proactive text per hour unless urgent. Respect quiet hours. Replies are never rate limited.
8. Calendar triggers: ignore changes tagged `source: timo` so Timo never triggers itself. Debounce about 30s per event and fetch changes with a sync token. Renew watch channels before they expire (about a week). Keep a fallback poll every 10-15 min.
9. Identity: the phone number is the user. A first text from an unknown number starts onboarding; no signup, no password. The OAuth `state` is a signed token that maps to the phone number.
10. Every integration has a mock. `USE_MOCKS=1` must run the whole loop offline.
11. Never call `datetime.now()` directly. Use `clock.now()`.
12. External calls: httpx timeout of 5s; failures log and degrade gracefully, never crash a run or the server.
13. Log one line per run: user, trigger, tools called, actions, channel, outcome, memories used. Log every outbound message and pending action.
14. Messages: spoken text is 1-2 sentences. An SMS leads with the action and the time, names exact items, and phrases asks as "reply Y to send". Example: "Leave at 8:10. The 101 is slow, take 280. On the train: reply to Sarah (drafted, reply Y to send) and skim the Q3 deck before your 10am."

## Demo setup
- Two Dial numbers. `DIAL_NUMBER` is Timo. `DEMO_PHONE` plays the user, driven by phone/test_user.py, so the loop runs without anyone's phone and doubles as the demo backup. Test calls on a real phone.
- The Google app stays in testing mode. Add teammates' and judges' accounts as test users; they click through the unverified-app warning.
- Webhooks need public HTTPS during development: ngrok or Cloudflare Tunnel.
- Pre-seed one account with a week of commute history, and keep a recorded backup video.

## Current state
- The legacy student-pick loop still runs offline: `POST /demo/tick` -> poller -> recommend -> actions -> notify, and `/sms/inbound` handles go / next / done / bus / scooter / laptop. Keep `scripts/smoke.sh` passing until the new loop replaces it.
- Built in brain/ but not wired to a router yet: memory, rules, the TriggerEvent and memory models, and `runner.run`, which still wraps the legacy `recommend()` until the tool-use loop lands.
- Not started: OAuth, Gmail, real Calendar, the event router, the scheduler, onboarding, real Dial, the new MCP tools, travel time, the landing page.

## Open decisions
- SQLite or Postgres.
- Remove Todoist, Darwin and the student pick once the new loop works?
- Owners for `get_travel_time` and the landing page.
- Does Dial support outbound calls where the user can talk back? Inbound MMS for resumes?
- Where do we host the backend and MCP server?

## Out of scope (do not build)
Wake word, native app, screen watching, ambient audio, continuous location tracking, any web page beyond the landing page and Google's consent screen, passwords or signup flows, integrations beyond Google and Dial, LLM-inferred personality, Alike at runtime. If a task seems to need one of these, stop and ask.

## Secrets
Keep all keys in `.env` (gitignored). Maintain `.env.example` with placeholder values. Never print keys in logs or in agent sessions (Alike records full transcripts).

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
