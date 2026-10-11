# Timo - agent rules

Read this before every task. These rules apply to every AI agent working in this repo (Cursor, Claude Code, anything else).

## Context
- Hackathon build (a16z Speedrun, Oct 11 2026). Hard feature freeze at 4:00 into the build. Shipping a working demo beats clean architecture.
- Timo turns a student's free calendar blocks into one specific, executable action. It runs mainly in push mode: it detects a gap of 10 minutes or more, picks one task or piece of content, does the reversible actions itself, and texts the user. The user can reply with one word to steer it.
- Pull mode (the user calls or texts Timo through Dial) uses the same core.

## Stack
Python 3.12, uv, FastAPI, FastMCP (streamable HTTP, mounted at /mcp), Pydantic v2, pydantic-settings, httpx, APScheduler or an asyncio loop, Anthropic SDK (model from settings, default `claude-haiku-5-5`), google-api-python-client, SQLite (stdlib sqlite3).
Do not add dependencies without asking.

## Repo layout
```
app/
  main.py            FastAPI app; mounts FastMCP at /mcp; lifespan starts poller
  config.py          Settings (env): USE_MOCKS, NOW_OVERRIDE, keys, USER_PHONE
  clock.py           now() - the ONLY source of current time
  models.py          Pydantic contracts (see below)
  state.py           SQLite: fired gaps, last pick per phone
  mcp_server.py      FastMCP tools: recommend, get_tasks, get_free_window, act
  core/
    ranking.py       deterministic filters + sort (no LLM)
    llm.py           one structured LLM call: choose among candidates, write text
    recommend.py     recommend(window, situation, exclude_ids) -> Recommendation
    actions.py       auto actions: block calendar, update Todoist
  integrations/      one module per service; each has a real and a mock impl
    calendar.py  todoist.py  dial.py  darwin.py  content_index.py  mocks.py
  triggers/
    poller.py        every 60s: find gap -> recommend -> actions -> SMS
    sms_handler.py   parse replies: go | next | done | scooter | walk | bus | laptop
data/
  course_index.json  mock_tasks.json  mock_calendar.json
scripts/smoke.sh     curl-based smoke tests
```

## Contracts (app/models.py)
Treat these as frozen. Do not rename or reshape them unless asked.
- `Situation`: `audio` | `phone` | `laptop`
- `Task {id, title, due: datetime|None, est_minutes: int, context: "noscreen"|"phone"|"laptop", course: str|None, url: str|None}`
- `FreeWindow {start, end, minutes, next_event_title: str|None}`
- `Candidate {kind: "task"|"content", id, title, url, minutes, source, due: datetime|None, course: str|None}`
- `Recommendation {pick: Candidate, reason, speech_text, sms_text, auto_actions: list[str]}`

## Architecture rules
1. `recommend()` is one plain function with three callers: the poller, the SMS handler, and the MCP tool. Never duplicate its logic.
2. Hard rules live in code (ranking.py), not in the LLM:
   - audio allows only noscreen; phone allows noscreen and phone; laptop allows everything.
   - The item must fit the window: minutes <= window minus a 3-minute buffer.
   - Sort by nearest due date, then by best fit to the window.
   - Pass at most 5 candidates to the LLM.
3. The LLM only picks one of the given candidates and writes the speech and SMS text. If it fails, times out (3s), or returns an id that isn't in the list, fall back to the top-ranked candidate with templated text. The demo must never depend on the LLM succeeding.
4. Every integration has a mock. `USE_MOCKS=1` must run the whole loop offline.
5. Never call `datetime.now()` directly. Use `clock.now()`, which honors `NOW_OVERRIDE`.
6. Reversible actions (calendar block, Todoist status) run automatically. External or money actions (Darwin orders, messages to other people) only run after the user replies "go". Darwin orders stop at the review step and never complete payment.
7. External calls: httpx timeout of 5s, wrapped so a failure logs and degrades gracefully instead of crashing the loop.
8. Log one line per decision: window, situation, candidates considered, pick, actions taken.
9. Spoken text: 1-2 sentences. SMS: 1 line of why, then the link, then the reply options. Always name the exact item. Never answer with a category.

## Out of scope (do not build)
Wake word, native app, screen watching, ambient audio, continuous location, inferring commutes from event locations, memory beyond last-pick state, any web frontend, auth or multi-user. If a task seems to need one of these, stop and ask.

## Secrets
Keep all keys in `.env` (gitignored). Maintain `.env.example` with placeholder values. Never print keys in logs.

## Git and attribution (strict)
- Never add an AI tool, assistant, or agent as an author, co-author, or contributor.
- No `Co-authored-by:` trailers for Claude, Cursor, Anthropic, Copilot, or any AI.
- No "Generated with ...", "Made with ...", or similar lines in commits, PR titles or descriptions, code comments, the README, or docs.
- Commit messages: short, imperative, conventional prefix (`feat:`, `fix:`, `chore:`). Commit only when asked.
- A commit-msg hook in `.githooks/` strips AI trailers. Run `git config core.hooksPath .githooks` once per clone.

## Working style
- Make small diffs. Don't refactor files outside the task.
- After a change, run `scripts/smoke.sh` or give the exact curl command that verifies it.
- If something conflicts with these rules or the time budget, say so before writing code.
