# Timo

Timo turns a free block on a student's calendar into one specific thing to do. It picks one task, does the reversible part itself, and texts or calls with that pick. The person replies with one word (`go`, `next`, `done`, `bus`, `scooter`) instead of choosing from a list.

Calendar events, texts, and calls go through one FastAPI app and one planner. Texts and calls are separate switches. Reading a real calendar does not send a text or place a call.

## Run it offline

This is the path for a rehearsal or a demo video. Nothing leaves the laptop.

From the repo root:

```sh
uv sync
```

`uv sync` installs the Python libraries from `pyproject.toml` into `.venv`. Run it once, and again only if dependencies change.

Start the app:

```sh
USE_MOCKS=1 MOCK_DIAL=1 SMS_ENABLED=0 LIVE_CALLS_ENABLED=false POLLER_ENABLED=false \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 3000 --workers 1
```

Leave that terminal open. In a browser, open [http://127.0.0.1:3000/inbox](http://127.0.0.1:3000/inbox).

Then, in a second terminal:

```sh
curl -s http://127.0.0.1:3000/sms/inbound \
  -H 'Content-Type: application/json' \
  -d '{"from_number":"+15555550100","body":"30 minutes on the bus"}'
```

If `DEMO_PHONE` in `.env` is not `+15555550100`, use that number instead. The inbox shows one pick, a link, and the reply words. Tap `bus`, `next`, or `done` to steer it. `Demo: onboard` at the top of the inbox plays a scripted first conversation and does not send anything through Dial.

These pages do not place a call:

- [http://127.0.0.1:3000/voice/preview](http://127.0.0.1:3000/voice/preview)
- [http://127.0.0.1:3000/voice/readiness](http://127.0.0.1:3000/voice/readiness)
- [http://127.0.0.1:3000/outbox](http://127.0.0.1:3000/outbox)

To exercise the calendar poller offline, stop the server above and run:

```sh
uv run python calling-mvp/serve_calls.py --mock-calendar
```

The launcher prints the port it actually bound. Real calls stay off.

## What a demo can show

1. Open the inbox and tap **Demo: onboard**.
2. Send `30 minutes on the bus` and show the single pick.
3. Tap `bus`, then `next`, then `done`.
4. Open the voice preview and say that a call uses the same brain and does not dial unless live mode is on.

Voice suggestions are the twelve local fixtures in [calling-mvp/DEMO_SESSIONS.md](calling-mvp/DEMO_SESSIONS.md) (a ride, a reply, a follow-up, and so on). Approving one in a fixture session simulates the action. It does not book a ride, send email, or change a real calendar.

## Real calendar

Mocks are the default. A real Google read needs `USE_MOCKS=0` and does not, by itself, enable texts or calls.

1. One person creates the Google Cloud project, enables the Calendar API, adds every teammate's Gmail under Test users, and creates a Desktop OAuth client.
2. Save that client JSON as `credentials.json` in the repo root. It only identifies the app.
3. Each person runs `uv run python scripts/google_auth_setup.py` once and approves their own account. That writes `token.json`. Both files are gitignored.
4. Check the read:

```sh
USE_MOCKS=0 uv run python -c "from datetime import date; from app.context.calendar import get_events; print(get_events(date.today()))"
```

Cancelled, free, and declined events are not busy. Busy all-day events block the day. Overlapping busy events are merged. Holds Timo created are tagged and ignored when reading, so they do not invent new gaps. After demo runs, `uv run python scripts/clear_timo_blocks.py --delete` removes those holds. While the Google app is in Testing, refresh tokens expire after 7 days; rerun the setup script. A failed calendar read suppresses the trigger. It is never treated as free time.

## Real texts and calls

Stop the offline server first. Dial login, Google consent, and the enrolled phone belong in the local `.env`. Do not paste keys into the terminal history you record.

Real outbound texts require all three: `USE_MOCKS=0`, `MOCK_DIAL=0`, and `SMS_ENABLED=1`. Otherwise messages stay in `/outbox` and `/inbox/thread`.

Real calls use a separate launcher:

```sh
uv run python calling-mvp/serve_calls.py --live
```

Startup checks the outgoing Dial number, opens the tunnel, and connects the tools. If that check fails, calls stay off. A manual call through the same engine, which does dial, is `calling-mvp/.venv/bin/python first_call.py --call` from `calling-mvp`. Run it only for a planned rehearsal.

`DEMO_PHONE` and `USER_NUMBER` are the same person's E.164 number. `DIAL_NUMBER` and `TIMO_NUMBER` are Timo's E.164 number. `DIAL_FROM_NUMBER_ID` is that Dial number's id.

During a live rehearsal, do not run `scripts/smoke.sh`, reset the inbox, delete state files, or clear the dedupe database. Details are in [calling-mvp/INTEGRATED_DEMO.md](calling-mvp/INTEGRATED_DEMO.md) and [calling-mvp/CALENDAR_TRIGGERS.md](calling-mvp/CALENDAR_TRIGGERS.md).

## Layout

| Path | What it is |
| --- | --- |
| `app/main.py` | The one server |
| `app/phone/` | Inbox, SMS replies, Dial send |
| `app/context/` | Calendar and tasks |
| `app/brain/` | Ranking, profile, the planner |
| `app/agent/` | Poller, calls, voice tools |
| `calling-mvp/` | Live launcher, fixtures, and the longer runbooks |
| `data/` | Mock calendar, tasks, and course index |
| `.env` | Local secrets. Gitignored. Copy from `.env.example` |
