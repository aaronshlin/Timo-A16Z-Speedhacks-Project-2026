# Timo

Timo is an always-on agent that lives in a phone number. It reads the calendar, plans the day around the commute, and calls or texts first with one exact next step: when to leave, what fits the gap, and what is ready to approve.

There is no app to install and no list to work through. A person texts one number. Timo takes it from there.

## What it does

- **Watches the day.** Events and free time drive the plan. Leave-by is the event start, minus travel and a short buffer. Timo reaches out about twenty minutes before departure.
- **Picks one thing.** A free gap becomes a single recommendation. One-word replies steer it: `go`, `next`, `done`, `bus`, `scooter`.
- **Acts, then asks.** Reading, drafting, and blocking time happen on their own. Anything that affects another person waits for a yes: `reply Y`.
- **Chooses the channel.** A call when someone is about to leave, when something is urgent, or when there are several steps. A text otherwise. Quiet hours and rate limits live in code, including when the model is unavailable.

> Leave at 8:10. The 101 is slow, take 280. On the train: reply to Sarah (drafted, reply Y to send) and skim the Q3 deck before your 10am.

## How a run works

A calendar change, an inbound text, and the scheduler all become one trigger. One loop loads memory, reads context, decides, acts, tells the person, and writes back what it learned. If the model errors, times out, or hits its step cap, the same loop sends a message built in code, such as the leave-by time, or it does nothing.

| Path | Role |
| --- | --- |
| `app/main.py` | The server: SMS, voice, and tools |
| `app/phone/` | Dial SMS, the inbox, and replies |
| `app/context/` | Google Calendar, live and mock |
| `app/brain/` | Memory, rules, the planner, and the agent loop |
| `app/agent/` | Voice, calls, and in-process tools |
| `dashboard/` | A read-only replay of a day, and a live mirror of calls |
| `data/` | Mock calendar and tasks for offline runs |
| `calling-mvp/` | Live-call launcher and rehearsal notes |

**Stack.** Python 3.12, FastAPI, Claude, Google Calendar, Dial for SMS and voice, SQLite.

## Product rules

These are enforced in code.

- The phone number is the account. No password and no signup.
- Timo ignores calendar changes it created, so it does not trigger itself.
- At most one proactive text an hour, and at most two calls a day, unless the item is urgent. Replies are never rate limited.
- Messages stay short. An SMS leads with the action and the time.
- Every integration has a mock. `USE_MOCKS=1` runs the loop with nothing leaving the machine.
- Reading a real calendar does not send a text or place a call. Those are separate switches.

## Run it locally

Nothing in this path leaves the machine. From the repo root:

```sh
uv sync
```

Start the app:

```sh
USE_MOCKS=1 MOCK_DIAL=1 SMS_ENABLED=0 LIVE_CALLS_ENABLED=false POLLER_ENABLED=false \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 3000 --workers 1
```

Open [http://127.0.0.1:3000/inbox](http://127.0.0.1:3000/inbox). In a second terminal:

```sh
curl -s http://127.0.0.1:3000/sms/inbound \
  -H 'Content-Type: application/json' \
  -d '{"from_number":"+15555550100","body":"30 minutes on the bus"}'
```

If `DEMO_PHONE` in `.env` is not `+15555550100`, use that number. The inbox shows one pick and the reply words. `Demo: onboard` at the top plays a scripted first conversation and does not send anything through Dial.

These pages do not place a call:

- [http://127.0.0.1:3000/voice/preview](http://127.0.0.1:3000/voice/preview)
- [http://127.0.0.1:3000/voice/readiness](http://127.0.0.1:3000/voice/readiness)
- [http://127.0.0.1:3000/outbox](http://127.0.0.1:3000/outbox)

The read-only day view:

```sh
uv run uvicorn dashboard.server:app --port 8100
```

Then open [http://127.0.0.1:8100](http://127.0.0.1:8100). The product page is [http://127.0.0.1:8100/showcase](http://127.0.0.1:8100/showcase).

To exercise the calendar poller offline, stop the server above and run:

```sh
uv run python calling-mvp/serve_calls.py --mock-calendar
```

The launcher prints the port it bound. Real calls stay off.

### A local walkthrough

1. Open the inbox and use **Demo: onboard**.
2. Send `30 minutes on the bus` and show the single pick.
3. Reply `bus`, then `next`, then `done`.
4. Open the voice preview. A call uses the same decisions and does not dial unless live mode is on.

Voice suggestions are the local fixtures in [calling-mvp/DEMO_SESSIONS.md](calling-mvp/DEMO_SESSIONS.md). Approving one in a fixture session records the action locally. It does not book a ride, send email, or change a real calendar.

## Connect a real calendar

Mocks are the default. A real Google read needs `USE_MOCKS=0`. That setting does not enable texts or calls.

1. Create a Google Cloud project, enable the Calendar API, add each account under Test users, and create a Desktop OAuth client.
2. Save that client JSON as `credentials.json` in the repo root. It only identifies the app. It is gitignored.
3. Each person runs `uv run python scripts/google_auth_setup.py` once and approves their own account. That writes `token.json`, also gitignored.
4. Check the read:

```sh
USE_MOCKS=0 uv run python -c "from datetime import date; from app.context.calendar import get_events; print(get_events(date.today()))"
```

Cancelled, free, and declined events are not busy. Busy all-day events block the day. Overlapping busy events are merged. Holds Timo created are tagged and ignored on read, so they do not invent new gaps. `uv run python scripts/clear_timo_blocks.py --delete` removes those holds. While the Google app is in testing, refresh tokens expire after 7 days; rerun the setup script. A failed calendar read suppresses the trigger. It is never treated as free time.

## Connect real texts and calls

Stop the local server first. Dial credentials and the enrolled phone belong in `.env`, copied from [`.env.example`](.env.example). Do not paste keys into a terminal you record.

Real outbound texts require all three: `USE_MOCKS=0`, `MOCK_DIAL=0`, and `SMS_ENABLED=1`. Otherwise messages stay in `/outbox` and `/inbox/thread`.

Real calls use a separate launcher:

```sh
uv run python calling-mvp/serve_calls.py --live
```

Startup checks the outgoing Dial number, opens the tunnel, and connects the tools. If that check fails, calls stay off. A manual call through the same engine is `calling-mvp/.venv/bin/python first_call.py --call` from `calling-mvp`, and only for a planned rehearsal.

`DEMO_PHONE` and `USER_NUMBER` are the person's number, in E.164. `DIAL_NUMBER` and `TIMO_NUMBER` are Timo's number. `DIAL_FROM_NUMBER_ID` is that Dial number's id.

During a live rehearsal, do not run `scripts/smoke.sh`, reset the inbox, delete state files, or clear the dedupe database. Details are in [calling-mvp/INTEGRATED_DEMO.md](calling-mvp/INTEGRATED_DEMO.md) and [calling-mvp/CALENDAR_TRIGGERS.md](calling-mvp/CALENDAR_TRIGGERS.md).

## Status

The offline loop, the inbox, calendar reads, SMS, and live Dial calls run today. Voice fixtures confirm actions locally and record them on the dashboard. They do not book, purchase, or send on a person's behalf.

Built for a16z Speedrun, October 2026.
