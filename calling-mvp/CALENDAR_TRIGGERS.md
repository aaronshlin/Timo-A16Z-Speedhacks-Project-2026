# Calendar calls and manual calls

The only app is `app.main:app`. Its single poller runs every 60 seconds, reads Eric's `calendar.get_events`, computes the current eligible gap, and calls the same typed `trigger_calendar_gap(CalendarGap)` function used by the HTTP receiver. There is one Dial HTTP client and one verified `Engine.trigger` implementation. The manual command uses that Engine too. Calendar and manual calls share the eligible task catalog and the same conversational approval flow.

## Local setup

From the repository root, run `uv sync`. Put the team's Google desktop OAuth client JSON in ignored `credentials.json`, then run:

```bash
uv run python scripts/google_auth_setup.py
```

This saves the personal Google grant to ignored `token.json`. An existing grant can be selected through `GOOGLE_TOKEN_PATH`. Both OAuth files stay local. Choose calendars using `GOOGLE_CALENDAR_IDS='["primary"]'`; all selected calendars must read successfully.

Dial login and tooling remain under `calling-mvp/.local` and `.tools`. Set the enrolled recipient in private `calling-mvp/.env` as `USER_PHONE`, or in the main `.env` as `DEMO_PHONE`. `DIAL_NUMBER` is an E.164 telephone number; `DIAL_FROM_NUMBER_ID` is the separate Dial resource ID. The launcher explicitly uses `cmv44iugl00mt0ftbpptzmudz` as the outgoing ID and never purchases a number.

## Dry-run first

From the repository root:

```bash
uv run python -m app.agent.dry_run --state-path data/calendar-validation.db --ticks 2
uv run python -m app.agent.dry_run --state-path data/calendar-validation.db --ticks 1
```

These commands force real calendar reads, actual time, and disabled calls. They never start a Dial client. When a current eligible gap exists, the first command reports an accepted reservation, then a duplicate. The second command starts a fresh interpreter and must report the same session ID as a duplicate. An occupied calendar, missing grant, failed read, or incomplete pagination suppresses the trigger instead of inventing free time. If there is no eligible real gap, live reservation/restart evidence remains unverified; use the isolated automated tests for those cases.

Validation uses a separate SQLite file so it does not consume the live demo's opportunities. Never delete the live dedupe database. **Do not run `scripts/smoke.sh` in real-read or live mode: it deletes the brain database and uses fake time.**

## Run polling or a manual call

In `calling-mvp`, using the locally installed Python (`../.venv/bin/python` also works after `uv sync`):

```bash
# Real calendar reads, call reservations, calls disabled:
.venv/bin/python serve_calls.py

# Explicitly enable the persistent call budget and keep the server open:
.venv/bin/python serve_calls.py --live

# Existing command: one manual call, even without a connected calendar:
.venv/bin/python first_call.py --call
```

The manual command reuses a running live service when available; otherwise it starts the same main app with its poller disabled. It respects active-call, unknown-response, cooldown and session-budget protections. `CALL_SESSION_LIMIT=1` is a persistent budget, including uncertain attempts; restarting never resets it. Increase it explicitly in private config only when authorizing more calls.

The launcher verifies the signed callback and dedicated voice Context MCP connection before sending a manual trigger or allowing a live calendar producer to call. Live mode has no `NOW_OVERRIDE` or fake-time ticks. The agent waits for permission to talk, offers eligible tasks naturally, and can continue after a skip or task deferral. Each action needs its own supplied confirmation; at most three tasks may be approved per call. Skips do not consume that allowance. Read [DEMO_SESSIONS.md](DEMO_SESSIONS.md) for conversation, simulation, and recovery details. Receipts stay in `/voice/sessions`; all fixture actions are local simulations, and calendar writes and SMS remain disabled.

## A 15-minute demo calling window

From `calling-mvp`, choose a future window today in Los Angeles time:

```bash
.venv/bin/python serve_calls.py --live --demo-window 14:40-14:55
```

This limits eligible calling time to that interval while still reading the real Google calendar. It does not create calendar events or fake the clock. Real busy events still suppress calls. A 15-minute interval leaves 12 usable minutes after the 3-minute buffer; the poller must detect it while at least 10 usable minutes remain. An 8-minute interval does not qualify. Start the launcher ahead of the window so the public tunnel and signed callback are verified in time.

The usual persistent opportunity reservation and persistent call budget still apply. Repeated ticks or a restart return the same session for the same interval. Keep the dedupe database; do not reset it to repeat a demo. The task catalog comes from the twelve supplied calling fixtures; student math and podcast picks are not used for the voice conversation. After the window ends the backend stays running and suppresses calendar calls outside the window.

## Optional external calendar producer

If Eric's producer runs separately, POST to `/voice/calendar-gap` with `Authorization: Bearer <CALENDAR_TRIGGER_TOKEN>` and the payload below. Configure `CALENDAR_TRIGGER_TOKEN` independently from the operator `CALL_TRIGGER_TOKEN`. No Google OAuth token, Dial key or phone number belongs in this payload. The in-process poller calls the typed function directly and does not need HTTP credentials.

```json
{
  "event_id": "unique-calendar-observation-id",
  "source": "google_calendar",
  "observed_at": "2026-10-11T13:16:00-07:00",
  "start": "2026-10-11T13:15:00-07:00",
  "end": "2026-10-11T13:45:00-07:00",
  "calendars_checked": ["primary"],
  "snapshot_complete": true,
  "next_event_title": "Team meeting"
}
```

Timestamps must be current and timezone-aware. `observed_at` is the beginning of the successful calendar read and must be within 90 seconds of actual server time. Preserve the original free block's bounds on every tick; never move `start` to each new observation. Gaps must contain now and leave at least 10 usable minutes after the default 3-minute buffer. Overlapping events and busy all-day events block time. Empty days and trailing gaps are bounded by configured calling hours (default 09:00–18:00, Los Angeles timezone).

SQLite reserves the opportunity transactionally **before** dialing, keyed on enrolled user, selected calendar scope and original UTC gap bounds. Calls disabled still produces a persisted dry-run reservation. Repeat events, repeat ticks and restarts return that reservation. A network-uncertain call becomes `call_unknown`; it is never redialed automatically. Crash recovery preserves that uncertainty.

`202` means a reservation was accepted (`queued` for live, `calls_disabled` for dry-run). `200` reports a duplicate or suppression. `401` rejects the producer token; `422` rejects invalid input. `/voice/readiness` reports read/call/write/SMS flags separately. `/health` is public:

```bash
curl --fail http://127.0.0.1:3000/health
```

Substitute the launcher's printed port. Live callbacks require the local terminal and temporary Cloudflare tunnel to stay open. No permanent deployment is configured.

## Validation

For verification without placing a call, preview the next eligible task and run the isolated suite from the repository root:

```bash
uv run python calling-mvp/preview_sessions.py
uv run python -m unittest discover -s tests -v
```

The preview acquires the Engine state lock and leaves call placement disabled. Stop a running backend first; previewing newly ingested fixtures starts their stored TTLs without renewing existing ones. Google and Dial are mocked in tests; temporary databases isolate conversation confirmation/cap, multi-task recovery, repeat/restart, all-day, overlap, pagination, error, signature and manual-command checks from real user state. No live calls, calendar writes or SMS are issued by tests.
