# Voice task conversations

`demo_sessions.json` contains the twelve supplied fictional task fixtures. `mock_queue.py` exposes the same data for older queue readers. Calendar and manual triggers select the lowest-priority-number eligible pending task as the first suggestion, then make the eligible task catalog available to the same conversation. The calendar determines when to call; the queue determines what can be discussed. Student math/podcast recommendations are not used for these voice sessions.

| Priority | Task ID | Proposal | TTL in minutes |
| --- | --- | --- | --- |
| 1 | `demo-ride-home` | $18 UberX back to the office | 20 |
| 2 | `demo-sarah-reply` | Reply to Sarah with Thursday at 2 PM | 60 |
| 3 | `demo-zacua-follow-up` | Send the fictional investor follow-up | 240 |
| 4 | `demo-trial-renewal` | Stop the $15 trial renewal | 120 |
| 5 | `demo-austin-flight` | Save a flight option without buying | 15 |
| 6 | `demo-pri-rsvp` | Accept Pri's dinner invitation for one | 180 |
| 7 | `demo-meeting-prep` | Prepare a five-bullet meeting brief | 20 |
| 8 | `demo-review-handoff` | Send Pri the checklist with open items marked | 90 |
| 9 | `demo-invoice-chase` | Ask the organizer for the invoice | 240 |
| 10 | `demo-calendar-buffer` | Add a private travel buffer without invitations | 30 |
| 11 | `demo-return-start` | Start a free mock return without issuing a refund | 120 |
| 12 | `demo-session-research` | Continue the comparison without signup or purchase | 240 |

## Conversation and confirmation

The agent starts with a brief greeting and asks whether now is a good time, then waits for the person's answer. It speaks each proposal naturally without reading menus, internal decision labels, tool names, or the word “demo.” Fixture IDs and historical receipts retain their original names for migration compatibility.

The voice model interprets ordinary speech in context. “No, I'm good; what else can you help me with?” declines the current suggestion and requests another. “Hold this one for later” defers that task and allows the conversation to continue. “I have to go” ends the call. Asking what is coming up requests an overview, and asking for an explanation does not approve an action. Skips and deferrals do not consume the action allowance.

Each call can approve at most **three tasks**. The agent can continue answering questions or give a recap after that limit, but cannot queue a fourth action. Hearing a proposal, granting permission to talk, or agreeing to hear a draft is not execution approval. “Do it” or “send that” prompts the supplied task-specific confirmation; execution requires the person's clear affirmative response to that confirmation. For the ride, the confirmation is “Request the $18 UberX from this venue to the office?”

Email proposals require the full exact recipient and draft before confirmation. Questions, uncertainty, negations, and changes to recipient, price, destination, or content require clarification. The fixtures contain fixed simulated terms; a correction cannot silently authorize a different action. Offers and explanations use only the supplied task context and read-only calendar context available to the call, without inventing inbox access or calendar appointments.

## Background work and truthful status

The dedicated authenticated voice Context MCP exposes the local conversation operations to Dial's managed agent. It uses the existing Engine and the same call catalog, confirmation state, and action limit. Approved work is reserved durably before a local simulation runs; the conversation can move to another task while that work completes. The returned status distinguishes queued, running, finished, and failed work. The agent must report the returned state and must never claim an actual ride was booked, email was sent, or calendar was changed.

Every fixture action has `execution_mode: simulate`. All dispatcher labels invoke local handlers. No ride, email, subscription change, booking, calendar write, return, purchase, SMS, or external background job is submitted. Simulated receipts remain dashboard-only. “Finished” refers to the local simulated job; the agent should describe it as such when discussing its result.

Signed webhook processing retrieves the verified call transcript and reconciles task decisions. This recovers attributable confirmed approvals when a during-call callback was unavailable. It never converts a request for more options or a generic affirmative into action approval. During-call MCP availability and post-call transcript recovery are separate paths; an unavailable callback is not proof that work ran during the call.

## State, expiry, and context storage

Ingestion persists `created_at` and `expires_at`. `expires_minutes` starts at ingestion, including the first preview that loads a new queue. Restarting or previewing again never renews an existing TTL. Expired tasks are excluded from selection. A future live-action implementation must revalidate source data, recipient, content, availability, and price; fixtures never authorize a live action.

Migration updates pending fixture IDs in place, appends missing fixtures once, and marks old pending fixture IDs superseded. It preserves active calls, resolved/skipped/deferred history, webhook records, mock results, and existing timestamps. The SQLite opportunity/dedupe store remains intact. Never delete either store to replay a task or bypass a call budget.

New simulated job/result IDs include both call identity and task identity, so several approvals within one call remain distinct. Existing historical result IDs are preserved. Duplicate callbacks and a process restart cannot create a second result for the same approved task. A placement with an uncertain network result remains unknown and is not redialed automatically.

Default context storage is **local_only**. An explicitly injected adapter implementing `record_result(result)` may mirror a result; it is optional storage and cannot authorize or execute an action. The default launcher connects no such adapter and uploads no private history to Alike. An unavailable adapter leaves the result local and reports that outcome.

## Preview and verify without calling

Stop a running backend before previewing its Engine state so the preview can acquire the same state lock. From the repository root:

```bash
uv run python calling-mvp/preview_sessions.py
```

An isolated state path can be used without changing the normal queue:

```bash
uv run python calling-mvp/preview_sessions.py --state-path /tmp/timo-preview-state.json
```

The CLI uses the configured `CALL_ENGINE_STATE_PATH`, or `calling-mvp/.timo-state.json` by default. It loads a disabled Engine with `client=None`, safely migrates pending fixtures, and prints the first candidate, spoken proposal, task confirmation, expiry, and storage mode. It creates no HTTP client, calendar reservation, call, action, context upload, or history deletion. Previewing a newly ingested queue starts its persisted TTLs.

Run the isolated automated suite from the repository root:

```bash
uv run python -m unittest discover -s tests -v
```

Tests use mocked integrations and temporary state. They verify conversation consent, task-specific confirmation, compound speech and negation, overview/question/stop handling, multiple tasks, the three-approval cap, duplicate/restart recovery, safe migration, expiry, and the absence of vendor writes. Calendar tests cover containing-now gaps, buffers, pagination, failure suppression, transactional dedupe, restart, and the explicit mock-calendar gate.

Never run `scripts/smoke.sh` against real-read/live state: it deletes a database and uses fake time. The preview and test commands above do not place a call.

## Calendar and manual entrypoints

Both entrypoints use the same Engine and conversational task queue. See [CALENDAR_TRIGGERS.md](CALENDAR_TRIGGERS.md) for read-only validation, launcher behavior, and future call setup. A dated complete mock snapshot supplies busy intervals around a free slot; the actual 60-second poller computes the gap from those intervals rather than invoking a manual-call endpoint.

Ordinary mock mode cannot dial. A live mock-calendar run requires an explicit snapshot, actual time, signed callback readiness, opt-in, the enrolled recipient, persistent call budget, cooldown, and dedupe. Wrong-day or incomplete snapshots fail closed. Fifteen minutes leaves twelve usable minutes at the start after the three-minute buffer; at least ten usable minutes must remain. All task actions stay simulated even when a phone call itself is real.
