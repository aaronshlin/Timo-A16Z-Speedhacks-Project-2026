<!-- new file, no collision with in-flight app/* work -->
# Timo packaging: verify after Codex

Prepared against main 63e180c56954ccf5e5f5b3dbe0bc075fece07862. These are ALL-NEW files, not a new app implementation. No remote writes, Docker build or live call were performed during preparation. Do not run them until Codex checks the assumptions below against the completed local rewrite.

## Put the files here

- Dockerfile.demo, Dockerfile.demo.dockerignore, compose.demo.yaml, Makefile at repository root.
- scripts/demo_ctl.py and scripts/demo_qa.py under scripts/.
- This checklist at repository root.
- If any destination already exists locally, STOP and compare it. Do not overwrite Codex's files. Rename Makefile to Makefile.demo and run make -f Makefile.demo if needed.

## First-time local setup

Docker Desktop running; Python 3.10+ for the host helper; Docker Compose 2.23.1+ for inline configs. Docker image dependencies use the repository's frozen uv.lock. Image tags and uv 0.9.5 must be resolved and built on the laptop; package availability/build has not been verified here.

Create a PRIVATE .demo.env at root, chmod 600. Alternatively set DEMO_ENV_FILE to a private file outside the repository. Do not git add that file. The generated .timo-demo-package directory has its own ignore file, but your INPUT .demo.env still needs to stay out of Git. It can contain:

```dotenv
DEMO_PHONE=+17372978567
DIAL_FROM_NUMBER_ID=cmv44iugl00mt0ftbpptzmudz
DIAL_API_KEY=YOUR_PRIVATE_KEY_FROM_LOCAL_STORAGE
GOOGLE_TOKEN_PATH=/absolute/laptop/path/token.json
GOOGLE_CALENDAR_IDS=["primary"]
CALL_TRIGGER_TOKEN=YOUR_PRIVATE_RANDOM_OPERATOR_TOKEN
PORT=3000
```

Do not paste actual keys/tokens in chat. DIAL_FROM_NUMBER_ID above is the previously verified older outgoing line, not a hidden new selection. Controller re-checks it in live mode. The human destination must be reviewed before CALL. If another number is selected, change the ID explicitly. Google OAuth consent happens separately on the laptop before packaging; the mounted token is writable because refresh updates it.

Existing state: if data/calls.db or calling-mvp/.timo-state.json exists, controller refuses a fresh package automatically. Stop the old laptop server, then run `python3 scripts/demo_ctl.py import-state`. It asks for STOPPED, requires both state files, acquires the engine lock, rejects active/unknown item calls, uses a SQLite backup (including WAL), copies engine JSON and voice ownership metadata, and preserves original files. Keep the old app stopped. Codex must check this migration if the rewrite changes state paths/schema. Do not erase budgets or receipts.

Commands after verification:

- make demo: build, local startup with real calendar config and calls/texts OFF, read-only QA, one stable dry reservation (no call). Google token presence is NOT proof that its current contents work.
- make demo-live: cold restart, private ingress/tunnel, owned callback replacement, private /voice-mcp discovery, signed ping, ready with polling OFF. NO automatic call.
- make qa: read-only HTTP checks. No resets, ticks or vendor calls.
- make call: asks for CALL on the terminal, records intent BEFORE placement, places at most one real call and waits for session status. Existing credit is used; maxCallDurationSeconds is app-controlled and must remain 180. Previously approved total spend cap still applies.
- make status / make logs / make stop: stop preserves state, never removes volumes.

## Exact app assumptions to re-check

1. Entry point app.main:app, uvicorn, Python >=3.12; dependency lock includes all imports. Single worker and file lock supported in Linux.
2. Engine loads /app/calling-mvp/demo_sessions.json. Twelve fixture tasks use execution_mode=simulate and explicit Demo only receipts. No automatic external email/calendar/ride execution from any path we register.
3. App config names: DEMO_PHONE, CALL_TRIGGER_TOKEN, DIAL_API_KEY, DIAL_FROM_NUMBER_ID, DIAL_NUMBER, TIMO_NUMBER, DIAL_WEBHOOK_SECRET, GOOGLE_TOKEN_PATH, GOOGLE_CALENDAR_IDS, LIVE_CALLS_ENABLED, CALL_OPT_IN, POLLER_ENABLED, RECEIPT_MODE, MOCK_DIAL, SMS_ENABLED, CALL_STATE_PATH, CALL_ENGINE_STATE_PATH. USER_PHONE/TRIGGER_TOKEN aliases are normalized only by this helper.
4. /health GET returns {ok:true}. /voice/readiness GET lists receipt_mode, calendar_writes_enabled, sms_enabled, real_calendar_reads, google_token_present, signed_callback_ready, voice_tools_ready, live_calls_enabled, call_opt_in.
5. Authenticated /voice/preview GET returns {next_session:{id:...},places_call:false}. Authenticated /voice/sessions GET returns an ARRAY of sessions, with sessionId, mode, status, receiptBody as applicable.
6. POST /voice/trigger accepts event_id + available_minutes; dry result mode=dry_run/status=calls_disabled; live result status=queued + sessionId. Stable event ID dedupe survives restart. Dry reservations must not consume a live opportunity or suppress subsequent live manual test.
7. POST /voice/runtime/context accepts {public_origin}; registers ONLY installation-owned /voice-mcp connection. DELETE same path disables private tools and poller, safely cleans only owned connection. Both require CALL_TRIGGER_TOKEN.
8. POST /voice/runtime/ready accepts {poll_calendar:false}; response ready=true/poller_enabled=false. It requires verified signed callbacks and private tool discovery; no timer starts before this gate. Static LIVE_CALLS_ENABLED is not sufficient alone.
9. /voice-mcp/ is Streamable HTTP; correct bearer plus X-Dial-* identity/current conversation checks. Exactly five safe fixture tools. DO NOT register legacy /mcp; its prior approval/false-success issues were not fixed by these packaging files.
10. Unified public /webhooks/dial accepts signed webhook.ping (relatedObject may be null), call.ended/call.transcribed/message.received and records callbacks_ready. Never accept unsigned user spoofing. The public proxy blocks /inbox*, /sms/inbound, /mcp and operator /voice/runtime routes.
11. Persisted call DB /state/calls.db and engine JSON /state/engine.json; Context MCP ownership metadata stored beside engine JSON. Brain DB remains /app/data/timo.db and local data is bind-mounted there. OAuth writes /secrets/google-token.json. Existing call state migration above must preserve budgets.
12. Both SMS flags are OFF. In-flight Codex wiring must not route a failed/uncertain voice placement into live SMS or legacy briefing side effects. App's /inbox is local only, not an SMS-delivery proof.

## Webhook rotation

No public webhook PATCH contract was found in inspected Dial docs. This helper therefore uses documented GET/POST/DELETE: persist intended target before creation, create replacement, obtain full secret privately, verify ping, then delete only the prior owned subscription. Do not fabricate PATCH behavior. Lost create response leaves a reconciliation blocker. A callback matching the public URL but without ownership evidence is refused. This deliberately fails closed rather than grabbing an unrelated hook.

No legacy /mcp gets registered. Nginx exposes health + signed callbacks + private voice tools only. No Alike connection or transcript upload is made by this package.

Quick tunnel URL rotates when its container restarts. Run make demo-live to rebuild readiness for the new URL. Do not restart just tunnel while a call is active. Before every explicit make call, QA checks current public health and excluded routes. For uninterrupted automatic polling a later supervisor/alert is still needed; this package intentionally never starts auto polling.

## QA before 5pm

- Build image and run compose config WITHOUT printing expanded env values. Syntax-only checks here are not Docker execution proof.
- Make demo, inspect dry reservation and next fixture. In a separate controlled app check with calls OFF, invoke run_tick_detailed and verify a SUCCESSFUL fresh real-calendar read. Missing token, revoked OAuth, all-day busy, incomplete pages, no eligible gap => no call. demo_qa only checks token presence; it cannot infer completeness.
- Recheck all merged app tests with mocks. Repeat stable dry event and restart: no duplicate placement. Never run scripts/smoke.sh on live state (it deletes brain DB and resets inbox).
- Live bootstrap ping and private tool discovery verified; poller OFF. Test invalid signature/headers/token in mocks, not by spamming provider.
- Explicit one phone call: greeting yes != task approval; negation/uncertainty hold; exact confirmation; natural speech; simulated results; max three actions; signed end/transcript arrives; receipt exists and transcriptVerification correct. Controller prints only status, not private receipt content.
- Real outbound SMS is blocked while the selected line is not_registered; inbound may work. Verify current vendor status before making delivery claims. No paid registration or provisioning in startup.
- Inspect existing session cap and expired fixtures. TTL is not renewed on restart. Do not wipe state to manufacture a passing test or replenish budget.
- Tunnel loss/missing tools/callback failure => no new call. Unknown placement => provider reconciliation, never blind retry. Stop stack after proof.

## Known packaging limits

No Docker daemon/build/tunnel/provider mutation was used to test this artifact. Host script syntax and mock HTTP behavior were checked only. The app rewrite must land before integration tests.

This package deliberately excludes automatic calendar dialing and legacy briefings. It runs the same app and lets calendar reads/dry diagnostics and local mocked texts coexist; it does not assert that every path is already unified. Codex owns that wiring.

No wholesale repo mount enters the app, but existing data/ is persisted by bind mount for the hardcoded brain DB. Secrets only mount at the token path or enter the runtime env file, never image layers. Python env parser is intentionally limited to plain KEY=VALUE or single/double quoted single-line values; apostrophes/multiline values in generated config are rejected.
