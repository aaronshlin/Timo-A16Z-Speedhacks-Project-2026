# Run the current-main integration

Calendar events, incoming texts, and demo triggers feed `context.router.dispatch` and the same `brain.runner.run`. The planner reads one phone identity and shared memory, then chooses a text or the existing call service. There is one FastAPI app, one calendar poller, and one verified call Engine.

## Identities and delivery gates

- `DEMO_PHONE` and `USER_NUMBER` identify the same enrolled person by E.164.
- `DIAL_FROM_NUMBER_ID` is the Dial number ID; `DIAL_NUMBER` and `TIMO_NUMBER` are its E.164 phone number. The launcher verifies the existing outgoing number and aligns these aliases without changing the private main `.env`.
- A human phone works without `DIAL_USER_NUMBER_ID`. The scripted second-number persona is enabled only when that configured number actually matches the selected target.
- `USE_MOCKS=1` keeps calendar reads local. Real calendar reads require `USE_MOCKS=0`; read access does not itself enable calls or texts.
- Real outbound texts require all three: `USE_MOCKS=0`, `MOCK_DIAL=0`, and `SMS_ENABLED=1`. Otherwise messages appear in `/outbox` and `/inbox/thread`.
- Real calls separately require explicit `--live`, call opt-in, enrolled target, available persistent budget, signed callbacks, and both verified tool connections. SMS remains controlled by its own flags.

## Rehearse offline first

From the repository root, with dependencies installed by `uv sync`, start the one app:

```sh
USE_MOCKS=1 MOCK_DIAL=1 SMS_ENABLED=0 LIVE_CALLS_ENABLED=false POLLER_ENABLED=false \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 3000 --workers 1
```

In another terminal, send an incoming text for the enrolled E.164 phone. Replace the example target with the same `DEMO_PHONE` used by the app:

```sh
curl -s http://127.0.0.1:3000/sms/inbound \
  -H 'Content-Type: application/json' \
  -d '{"from_number":"+15555550100","body":"30 minutes on the bus"}'
curl -s http://127.0.0.1:3000/outbox
curl -s http://127.0.0.1:3000/voice/readiness
curl -s http://127.0.0.1:3000/voice/preview
```

This uses existing state and the current text commands. The example requests a recommendation for a thirty-minute bus window; it does not change the saved call/text preference. The voice preview never places a call. To exercise the calendar poller offline, stop that app and run:

```sh
uv run python calling-mvp/serve_calls.py --mock-calendar
```

The launcher prints its actual local port, starts the same app, and leaves real calls disabled. Use an explicit dated `--demo-calendar PATH` snapshot when the standard calendar fixtures have no current eligible gap. Calendar gaps must contain actual now, leave at least 10 usable minutes after the next-event buffer, and come from a complete successful read.

## Connect the live phone path

Stop the offline app first. Private Google consent, Dial login, and the enrolled phone must already be configured locally. Do not copy credentials into commands, transcripts, or Git.

```sh
uv run python calling-mvp/serve_calls.py --live
```

Startup verifies the outgoing number, starts the existing HTTPS tunnel, configures signed callbacks, connects the eight main tools at `/mcp/` and the five task tools at `/voice-mcp/`, verifies discovery, and only then enables the single calendar poller. Failed discovery or callback verification blocks calls. Shutdown removes only connections owned by this installation; unrelated Context MCPs are preserved.

For a manual call through that same Engine, without calendar polling:

```sh
cd calling-mvp
.venv/bin/python first_call.py --call
```

That command places a real call. Run it only for an intended rehearsal. If a live service is already running, the command reuses it instead of creating another poller. A completed or uncertain call still consumes its stored reservation; restarting never resets the budget or redials that opportunity.

## Shared context and results

Planner briefing calls carry the planner's actual script and pending-action IDs. Their main MCP tools approve or decline the matching shared pending action, save user notes, or send details through the same SMS gate. A texted `Y` uses the same pending-action handler as a voice approval.

Task conversations additionally offer eligible fixture tasks, up to three task-specific approvals per call. Fixture actions remain local simulations with receipts and durable history. The planner uses shared local profile and pending-action memory. Full private facts stay local; calls receive the task context already used by the existing flows; asking for other tasks never authorizes execution. Skips and deferrals continue the conversation, while an explicit request to stop ends it.

Inspect `/voice/sessions`, `/voice/readiness`, `/outbox`, and `/inbox/thread` during rehearsal. Live/private routes require the configured operator bearer token; the launcher keeps it out of logs. Google writes and non-fixture actions depend on the handlers and granted scopes already present in main; missing integration must report failure rather than claim completion.

Do not run `scripts/smoke.sh`, reset the inbox, remove state files, renew expired fixtures, or delete the dedupe database against the live rehearsal. The automated integration tests use temporary databases, fake provider transports, and isolated clocks.
