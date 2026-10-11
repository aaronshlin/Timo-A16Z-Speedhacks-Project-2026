# Timo-A16Z-Speedhacks-Project-2026

## Google Calendar

Mocks by default. To read and write a real calendar (`USE_MOCKS=0`):

1. One person creates the Google Cloud project, enables the Calendar API, adds every teammate's Gmail under Test users, and creates a Desktop app OAuth client.
2. Save the client JSON as `credentials.json` in the repo root. Share it with the team, it only identifies the app.
3. Each person runs `python scripts/google_auth_setup.py` once and approves their own account. That writes `token.json`, which is personal. Both files are gitignored.
4. `USE_MOCKS=0 python -c "from datetime import date; from app.context.calendar import get_events; print(get_events(date.today()))"` to check it reads your day.

Notes:
- Cancelled, marked-Free, declined, and all-day events do not count as busy. Overlapping events are merged into one block.
- Timo's calendar holds are tagged and ignored when reading, so they never create gaps. `python scripts/clear_timo_blocks.py --delete` removes them after demo runs.
- Google expires refresh tokens after 7 days while the app is in Testing. Rerun the setup script.
- A failed Google read is logged and treated as an empty day, so the poller stays quiet instead of crashing.

## Calendar-triggered calls

See [calendar call setup](calling-mvp/CALENDAR_TRIGGERS.md) for real-read dry-runs, persistent deduplication, and the preserved manual call command. Real calendar reads do not enable calls, calendar writes or SMS. Use the isolated tests for this integration; never run scripts/smoke.sh in real-read or live mode.
