"""Single-process FastAPI calling loop. No real context ingestion."""
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from app.agent.demo_sessions import (eligible, executable, migrate_queue, next_session, simulate,
                                     instruction as session_instruction, extract_decision as session_decision,
                                     normalize, intent, task_confirmation)

ROOT = Path(__file__).resolve().parent
log = logging.getLogger('timo')


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def verify_signature(secret, header, raw, now=None):
    try:
        parts = dict(p.strip().split('=', 1) for p in header.split(','))
        stamp, digest = parts['t'], parts['v1']
        if not re.fullmatch(r'\d+', stamp) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
            return False
        if abs((time.time() if now is None else now) - int(stamp)) >= 300:
            return False
        expected = hmac.new(secret.encode(), stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, digest.lower())
    except (AttributeError, KeyError, ValueError, TypeError):
        return False


def _legacy_extract_decision(call, item):
    turns = call.get('transcriptTurns')
    if not isinstance(turns, list) or not turns:
        turns = []
        for line in str(call.get('transcript') or '').splitlines():
            match = re.match(r'\s*(user|human|agent|assistant)\s*:\s*(.+)', line, re.I)
            if match:
                turns.append({'speaker': 'user' if match[1].lower() in ('user', 'human') else 'agent', 'text': match[2]})
    candidate = confirmed = None
    awaiting = False
    for turn in turns:
        text = re.sub(r'[.,!?]', '', str(turn.get('text', '')).lower()).strip()
        if turn.get('speaker') == 'agent':
            awaiting = bool(candidate and re.search(r'\b(confirm|correct|right)\b', text) and candidate in text)
        elif turn.get('speaker') == 'user':
            choice = next((o for o in item['options'] if re.fullmatch(
                r"(?:(?:i choose|choose|let's|lets|please|go ahead and) )?" + re.escape(o) + r'(?: it| please)?', text)), None)
            if choice:
                candidate, confirmed, awaiting = choice, None, False
            elif awaiting and text in ('yes', 'yeah', 'yep', 'correct', 'confirmed', 'that is correct', 'thats right', "that's right", 'yes correct'):
                confirmed, awaiting = candidate, False
            else:
                candidate = confirmed = None
                awaiting = False
    return confirmed


def extract_decision(call, item):
    return session_decision(call, item) if executable(item) else _legacy_extract_decision(call, item)


def instruction(item):
    return session_instruction(item)


class DialHTTPError(Exception):
    pass


class Engine:
    def __init__(self, env, state_path, client, result_handler=None, alike_adapter=None):
        for name in ('DIAL_API_KEY', 'DIAL_FROM_NUMBER_ID', 'USER_PHONE', 'DIAL_WEBHOOK_SECRET'):
            if env.get('LIVE_CALLS_ENABLED') == 'true' and not env.get(name):
                raise ValueError('Missing ' + name)
        if env.get('LIVE_CALLS_ENABLED') == 'true' and not re.fullmatch(r'\+[1-9]\d{7,14}', env['USER_PHONE']):
            raise ValueError('USER_PHONE must be E.164')
        self.env, self.path, self.client = env, Path(state_path), client
        self.result_handler = result_handler
        self.alike_adapter = alike_adapter
        # Historical calls keep the main branch's unanswered-call hook.
        self.miss_handler = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.jobs = asyncio.Queue()
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'items': [], 'events': {}}
        migrate_queue(self.state)
        self.state.setdefault('conversations', {})
        self.state.setdefault('action_jobs', {})
        for conversation in self.state['conversations'].values():
            if conversation['status'] == 'placing':
                conversation['status'] = 'call_unknown'
        for item in self.state['items']:
            if item['status'] == 'placing':
                item['status'] = 'call_unknown'
            if item.get('receiptStatus') == 'sending':
                item['receiptStatus'] = 'delivery_unknown'
        for event_id, entry in self.state['events'].items():
            if entry['status'] != 'processed':
                entry['status'], entry['attempts'] = 'pending', 0
                self.jobs.put_nowait(event_id)
        for result_id, job in self.state['action_jobs'].items():
            if job['status'] in ('queued', 'running'):
                job['status'] = 'queued'
                event_id = 'action:' + result_id
                if event_id not in self.state['events'] or self.state['events'][event_id]['status'] == 'processed':
                    self.state['events'][event_id] = {'status': 'pending', 'attempts': 0,
                        'event': {'type': 'local.action', 'data': {'resultId': result_id}}}
                    self.jobs.put_nowait(event_id)
        self.save()

    def save(self):
        atomic_json(self.path, self.state)

    async def api(self, method, endpoint, body=None, headers=None):
        opts = {'headers': {'Authorization': 'Bearer ' + self.env['DIAL_API_KEY'],
                           'Content-Type': 'application/json', **(headers or {})}, 'timeout': 7.0}
        if body is not None:
            opts['json'] = body
        response = await self.client.request(method, 'https://api.getdial.ai/api/v1' + endpoint, **opts)
        if not response.is_success:
            raise DialHTTPError('Dial HTTP ' + str(response.status_code))
        return {} if response.status_code == 204 else response.json()

    def next_pending(self, user='johnson'):
        return next_session(self.state, user)

    async def trigger(self, user, item_id=None):
        if user != 'johnson':
            return 400, {'error': 'Only mock user johnson is supported'}
        if self.env.get('LIVE_CALLS_ENABLED') != 'true':
            return 403, {'error': 'Live calls disabled. Approve credit spend before enabling LIVE_CALLS_ENABLED=true.'}
        if self.env.get('VOICE_TOOLS_REQUIRED') == 'true' and self.env.get('VOICE_TOOLS_ENABLED') != 'true':
            return 403, {'error': 'Voice conversation tools are not ready'}
        active_conversation = next((c for c in self.state['conversations'].values()
            if c['status'] in ('placing', 'queued', 'awaiting_transcript', 'ending', 'call_unknown')), None)
        if active_conversation:
            return 409, {'error': 'Conversation active or needs reconciliation',
                         'conversationId': active_conversation['id'], 'status': active_conversation['status']}
        active = next((i for i in self.state['items'] if i['status'] in ('placing', 'queued', 'awaiting_transcript', 'call_unknown')), None)
        if active:
            return 409, {'error': 'Session active or needs reconciliation', 'itemId': active['id'], 'status': active['status']}
        pending = sorted((i for i in self.state['items'] if i['user'] == user and eligible(i) and (item_id is None or i['id'] == item_id)), key=lambda i: i['priority'])
        if not pending:
            return 200, {'message': 'No eligible pending sessions', 'status': 'no_eligible_session'}
        item = pending[0]
        item['status'] = 'placing'
        item.setdefault('idempotencyKey', str(uuid.uuid4()))
        conversation_id = item.get('opportunitySessionId') or item['idempotencyKey']
        catalog = sorted((copy.deepcopy(i) for i in self.state['items']
            if i['user'] == user and (i['id'] == item['id'] or eligible(i))),
            key=lambda i: (i['priority'], i['id']))
        conversation = {'id': conversation_id, 'primaryItemId': item['id'], 'user': user,
            'status': 'placing', 'callId': None, 'sessions': catalog, 'dispositions': {},
            'approvals': {}, 'actionIds': [], 'maxActions': 3, 'createdAt': self._timestamp()}
        self.state['conversations'][conversation_id] = conversation
        self.save()  # Immutable catalog and call reservation precede placement.
        from app.agent.conversation import build_instruction
        try:
            response = await self.api('POST', '/calls', {
                'to': self.env['USER_PHONE'], 'fromNumberId': self.env['DIAL_FROM_NUMBER_ID'],
                'outboundInstruction': build_instruction(item, catalog, conversation_id,
                    self.env.get('VOICE_TOOLS_ENABLED') == 'true') if executable(item) else item.get('instruction') or instruction(item), 'language': 'en-US', 'maxCallDurationSeconds': 180
            }, {'Idempotency-Key': item['idempotencyKey']})
            item['callId'] = response['call']['id']
            if not isinstance(item['callId'], str) or not item['callId']:
                raise ValueError('Missing call.id')
            item['status'] = 'queued'
            conversation.update(status='queued', callId=item['callId'])
            self.save()
            return 202, {'itemId': item['id'], 'conversationId': conversation_id,
                         'callId': item['callId'], 'status': 'queued'}
        except Exception as error:
            item['status'] = 'failed' if isinstance(error, DialHTTPError) else 'call_unknown'
            conversation['status'] = item['status']
            item['error'] = str(error) if isinstance(error, DialHTTPError) else 'Call response uncertain; reconcile in Dial before retrying'
            self.save()
            return 502, {'itemId': item['id'], 'status': item['status'], 'error': item['error']}

    async def event(self, event):
        if event['type'] == 'local.action':
            await self._run_action(event['data']['resultId'])
            return
        if event['type'] == 'webhook.ping':
            return
        call_id = event.get('data', {}).get('callId') or event.get('relatedObject', {}).get('id')
        conversation = next((c for c in self.state['conversations'].values()
                             if call_id and c.get('callId') == call_id), None)
        if conversation:
            await self._conversation_event(event, conversation)
            return
        item = next((i for i in self.state['items'] if i.get('callId') == call_id and call_id), None)
        if not item or item['status'] in ('resolved', 'skipped', 'deferred', 'needs_review', 'failed'):
            return
        if event['type'] == 'call.ended':
            data = event.get('data', {})
            ended = data.get('status')
            if ended in ('busy', 'no-answer', 'failed', 'canceled', 'cancelled') or data.get('canceled'):
                item['status'], item['error'] = 'failed', 'Call ended: ' + str(ended or 'canceled')
                self.save()
                # Only a call that reached the handset earns a follow-up text. A
                # 'failed' or 'canceled' call may never have rung, and telling the
                # user they missed it would be untrue. The guard above means a
                # repeated webhook for this call returns before reaching here.
                if ended in ('busy', 'no-answer') and self.miss_handler:
                    await self.miss_handler(item, ended)
                return
            item['status'] = 'awaiting_transcript'
            self.save()
            return
        if event['type'] != 'call.transcribed':
            return
        call = (await self.api('GET', '/calls/' + quote(call_id, safe='')))['call']
        if call.get('id') != call_id or not (call.get('transcript') or call.get('transcriptTurns')):
            raise ValueError('Transcript not yet available or unexpected call record')
        decision = extract_decision(call, item)
        item['transcribedAt'] = time.time()
        if not decision:
            item['status'], item['error'] = 'needs_review', 'No explicit confirmed decision in attributed user turns'
            self.save()
            return
        if executable(item):
            item.update(decision=decision, status='resolving', receiptStatus='dashboard_only')
            if decision == 'go':
                result_id = 'mock:' + (item.get('callId') or item['idempotencyKey'])
                result = self.state['mock_results'].get(result_id)
                if result is None:
                    result = simulate(item, result_id)
                    self.state['mock_results'][result_id] = result
                    item['resultId'] = result_id
                    self.save()  # Durable, idempotent local result before any optional mirror.
                    if self.alike_adapter is not None:
                        result['mirror_status'] = 'attempted'
                        self.save()
                        try:
                            self.alike_adapter.record_result(copy.deepcopy(result))
                            result.update(context_storage='local_and_configured_adapter', mirror_status='mirrored')
                        except Exception:
                            result.update(context_storage='local_only', mirror_status='unavailable')
                        self.save()
                item.update(resultId=result_id, receiptBody=result['receipt'],
                            actionStatus='simulated', contextStorage=result['context_storage'])
            else:
                verb = 'Skipped' if decision == 'next' else 'Deferred'
                item.update(receiptBody='Demo only: ' + verb + ' ' + item['title'] + '; no mock action performed.',
                            actionStatus='not_executed', contextStorage='local_only')
            self.save()
            if self.result_handler:
                await self.result_handler(item)
            item['status'] = {'go': 'resolved', 'next': 'skipped', 'hold': 'deferred'}[decision]
        else:
            # Historical in-flight calls retain their original literal-choice contract.
            item.update(decision=decision, receiptBody=item['receiptMessage'].replace('{decision}', decision),
                        receiptStatus='dashboard_only')
            if self.result_handler:
                await self.result_handler(item)
            item['status'] = 'resolved'
        self.save()

    @staticmethod
    def _timestamp():
        from app import clock
        return clock.now().isoformat()

    def voice_context_valid(self, conversation_id):
        conversation = self.state['conversations'].get(conversation_id)
        return bool(self.env.get('VOICE_TOOLS_ENABLED') == 'true' and conversation
                    and conversation.get('callId') and conversation['status'] in
                    ('queued',))

    def _conversation(self, conversation_id):
        if not self.voice_context_valid(conversation_id):
            raise ValueError('No active authorized voice conversation')
        return self.state['conversations'][conversation_id]

    def _task(self, conversation, item_id, require_available=False):
        from app.agent.conversation import public_task_id
        item = next((i for i in conversation['sessions']
                     if i['id'] == item_id or public_task_id(i) == item_id), None)
        if item is None or not executable(item):
            raise ValueError('Task does not belong to this conversation')
        disposition = conversation['dispositions'].get(item['id'])
        if require_available and disposition:
            raise ValueError('Task already handled in this conversation')
        if require_available:
            candidate = {**item, 'status': 'pending', 'decision': 'pending'}
            if not eligible(candidate):
                raise ValueError('Task opportunity has expired')
        return item

    def _public_tasks(self, conversation):
        from app.agent.conversation import speech_item
        result = []
        for item in conversation['sessions']:
            candidate = {**item, 'status': 'pending', 'decision': 'pending'}
            if item['id'] not in conversation['dispositions'] and eligible(candidate):
                result.append(speech_item(item))
        return result

    async def voice_tasks(self, conversation_id):
        calendar_events, calendar_error = [], None
        try:
            from app import clock
            from app.config import settings
            from app.context.calendar import get_events
            events = await asyncio.to_thread(get_events, clock.now().date())
            from app.agent.conversation import speech_text
            calendar_events = [{'title': speech_text(e.title), 'start': e.start.isoformat(), 'end': e.end.isoformat()} for e in events]
            calendar_source = 'mock_calendar' if settings.USE_MOCKS else 'google_calendar'
        except Exception:
            calendar_source, calendar_error = 'unavailable', 'Calendar read unavailable; do not infer an empty calendar.'
        async with self.lock:
            c = self._conversation(conversation_id)
            return {'conversation_id': conversation_id, 'tasks': self._public_tasks(c),
                    'approved_count': len(c['actionIds']), 'max_actions': 3,
                    'execution_mode': 'simulate', 'calendar_source': calendar_source,
                    'calendar_events': calendar_events, 'calendar_snapshot_complete': calendar_error is None,
                    'calendar_note': calendar_error or 'These events came from the current calendar read.'}

    async def voice_prepare(self, conversation_id, item_id):
        from app.agent.conversation import speech_item
        async with self.lock:
            c = self._conversation(conversation_id)
            item = self._task(c, item_id, require_available=True)
            if len(c['actionIds']) >= 3:
                return {'status': 'limit_reached', 'max_actions': 3, 'can_continue_conversation': True}
            token = str(uuid.uuid4())
            c['approvals'].clear()  # A changed proposal invalidates the previous confirmation.
            c['approvals'][token] = {'itemId': item['id'], 'createdAt': self._timestamp()}
            self.save()
            spoken = speech_item(item)
            return {'status': 'needs_confirmation', 'item_id': spoken['task_id'],
                    'confirmation_token': token, 'confirmation': spoken['confirmation'],
                    'draft': spoken.get('draft'), 'recipient': spoken.get('recipient'),
                    'execution_mode': 'simulate', 'requires_full_draft': bool(spoken.get('draft'))}

    async def voice_confirm(self, conversation_id, item_id, confirmation_token,
                            user_text, spoken_confirmation, draft_read=''):
        from app.agent.conversation import speech_item
        async with self.lock:
            c = self._conversation(conversation_id)
            item = self._task(c, item_id)
            previous = c['dispositions'].get(item['id'])
            if previous and previous['decision'] == 'go':
                return {'status': previous['status'], 'result_id': previous['resultId'], 'duplicate': True}
            if previous:
                return {'status': 'not_approved', 'reason': 'task_already_handled'}
            self._task(c, item_id, require_available=True)
            approval = c['approvals'].get(confirmation_token)
            spoken = speech_item(item)
            projected = copy.deepcopy(item)
            projected['confirmation'] = spoken['confirmation']
            body = spoken.get('draft')
            if (not approval or approval['itemId'] != item['id']
                    or not task_confirmation(spoken_confirmation, projected)
                    or (body and normalize(body) not in normalize(draft_read))
                    or intent(user_text, projected) != 'go'):
                c['approvals'].clear()
                self.save()
                return {'status': 'not_approved', 'reason': 'clarify_and_confirm_current_task'}
            if len(c['actionIds']) >= 3:
                return {'status': 'limit_reached', 'max_actions': 3, 'can_continue_conversation': True}
            c['approvals'].clear()
            proof = {'source': 'authenticated_voice_tool', 'userText': user_text,
                     'spokenConfirmation': spoken_confirmation, 'draftRead': draft_read,
                     'at': self._timestamp()}
            result_id = self._queue_action(c, item, proof)
            return {'status': c['dispositions'][item['id']]['status'], 'result_id': result_id,
                    'execution_mode': 'simulate', 'external_effect': False,
                    'approved_count': len(c['actionIds']), 'max_actions': 3,
                    'next_tasks': self._public_tasks(c)}

    async def voice_status(self, conversation_id):
        from app.agent.conversation import public_task_id
        async with self.lock:
            c = self._conversation(conversation_id)
            actions = []
            for item in c['sessions']:
                record = c['dispositions'].get(item['id'])
                if record:
                    actions.append({'item_id': public_task_id(item), 'title': item['title'],
                                    **copy.deepcopy(record), 'execution_mode': 'simulate',
                                    'external_effect': False})
            return {'actions': actions, 'approved_count': len(c['actionIds']), 'max_actions': 3}

    async def voice_disposition(self, conversation_id, item_id, disposition):
        async with self.lock:
            c = self._conversation(conversation_id)
            if disposition == 'end':
                c.update(status='ending', endedByUser=True)
                c['approvals'].clear()
                self.save()
                return {'status': 'ending', 'message': 'End the call politely.'}
            if disposition not in ('next', 'hold'):
                raise ValueError('Unsupported task disposition')
            item = self._task(c, item_id)
            previous = c['dispositions'].get(item['id'])
            if previous:
                return {'status': previous['status'], 'duplicate': True}
            self._set_disposition(c, item, disposition)
            c['approvals'].clear()
            self.save()
            return {'status': c['dispositions'][item['id']]['status'],
                    'next_tasks': self._public_tasks(c), 'continue_conversation': True}

    def _set_disposition(self, conversation, snapshot, decision):
        record = {'decision': decision, 'status': {'next': 'skipped', 'hold': 'deferred'}[decision],
                  'at': self._timestamp(), 'actionStatus': 'not_executed'}
        conversation['dispositions'][snapshot['id']] = record
        item = next((i for i in self.state['items'] if i['id'] == snapshot['id']), None)
        if item:
            item.update(decision=decision, status=record['status'], receiptStatus='dashboard_only',
                        receiptBody='Simulation only: ' + record['status'].capitalize() + ' ' + item['title'] + '.',
                        actionStatus='not_executed', contextStorage='local_only', conversationId=conversation['id'])

    def _queue_action(self, conversation, snapshot, proof):
        result_id = 'mock:' + conversation['id'] + ':' + snapshot['id']
        if result_id in self.state['action_jobs'] or result_id in self.state['mock_results']:
            return result_id
        if len(conversation['actionIds']) >= 3:
            raise ValueError('Three-action conversation limit reached')
        from app.agent.conversation import speech_item
        action_item = copy.deepcopy(snapshot)
        spoken = speech_item(snapshot)
        if spoken.get('draft'):
            action_item['action']['args']['body'] = spoken['draft']
        action_item['receipt'] = re.sub(r'^Demo only:', 'Simulation only:', action_item['receipt'])
        job = {'resultId': result_id, 'conversationId': conversation['id'], 'itemId': snapshot['id'],
               'status': 'queued', 'snapshot': action_item, 'approval': copy.deepcopy(proof)}
        self.state['action_jobs'][result_id] = job
        conversation['actionIds'].append(result_id)
        conversation['dispositions'][snapshot['id']] = {
            'decision': 'go', 'status': 'queued', 'resultId': result_id, 'at': self._timestamp()}
        item = next(i for i in self.state['items'] if i['id'] == snapshot['id'])
        item.update(decision='go', status='resolving', actionStatus='queued', resultId=result_id,
                    conversationId=conversation['id'], receiptStatus='dashboard_only')
        event_id = 'action:' + result_id
        self.state['events'][event_id] = {'event': {'type': 'local.action', 'data': {'resultId': result_id}},
                                        'status': 'pending', 'attempts': 0}
        self.save()  # Approval + job are durable before the worker sees the job.
        self.jobs.put_nowait(event_id)
        return result_id

    async def _run_action(self, result_id):
        job = self.state['action_jobs'][result_id]
        if job['status'] in ('finished', 'failed'):
            return
        conversation = self.state['conversations'][job['conversationId']]
        item = next(i for i in self.state['items'] if i['id'] == job['itemId'])
        job['status'] = item['actionStatus'] = 'running'
        conversation['dispositions'][item['id']]['status'] = 'running'
        self.save()
        try:
            result = self.state['mock_results'].get(result_id)
            if result is None:
                result = simulate(job['snapshot'], result_id)
                self.state['mock_results'][result_id] = result
                self.save()
                if self.alike_adapter is not None:
                    result['mirror_status'] = 'attempted'
                    self.save()
                    try:
                        self.alike_adapter.record_result(copy.deepcopy(result))
                        result.update(context_storage='local_and_configured_adapter', mirror_status='mirrored')
                    except Exception:
                        result.update(context_storage='local_only', mirror_status='unavailable')
                    self.save()
            job['status'] = 'finished'
            conversation['dispositions'][item['id']]['status'] = 'finished'
            item.update(status='resolved', actionStatus='finished', receiptBody=result['receipt'],
                        contextStorage=result['context_storage'], resultId=result_id)
        except Exception:
            job['status'] = 'failed'
            conversation['dispositions'][item['id']]['status'] = 'failed'
            item.update(status='needs_review', actionStatus='failed',
                        error='Local simulated action failed; no external operation performed')
        self.save()
        if self.result_handler:
            await self.result_handler(item)

    async def _conversation_event(self, event, conversation):
        if conversation['status'] in ('finished', 'failed', 'needs_review'):
            return
        primary = next(i for i in self.state['items'] if i['id'] == conversation['primaryItemId'])
        if event['type'] == 'call.ended':
            data = event.get('data', {})
            if data.get('status') in ('busy', 'no-answer', 'failed', 'canceled', 'cancelled') or data.get('canceled'):
                conversation['status'] = 'failed'
                if not conversation['dispositions']:
                    primary['status'] = 'failed'
            else:
                conversation['status'] = 'awaiting_transcript'
                if primary['id'] not in conversation['dispositions']:
                    primary['status'] = 'awaiting_transcript'
            self.save()
            return
        if event['type'] != 'call.transcribed':
            return
        call_id = conversation['callId']
        call = (await self.api('GET', '/calls/' + quote(call_id, safe='')))['call']
        if call.get('id') != call_id or not (call.get('transcript') or call.get('transcriptTurns')):
            raise ValueError('Transcript not yet available or unexpected call record')
        from app.agent.conversation import extract_conversation
        decisions = extract_conversation(call, conversation['sessions'])
        verified = {d['itemId']: d['decision'] for d in decisions}
        for decision in decisions:
            snapshot = next((i for i in conversation['sessions'] if i['id'] == decision['itemId']), None)
            if not snapshot or snapshot['id'] in conversation['dispositions']:
                continue
            if decision['decision'] == 'go':
                candidate = {**snapshot, 'status': 'pending', 'decision': 'pending'}
                if len(conversation['actionIds']) < 3 and eligible(candidate):
                    result_id = self._queue_action(conversation, snapshot,
                        {'source': 'verified_transcript', 'callId': call_id, 'at': self._timestamp()})
                    await self._run_action(result_id)
            else:
                self._set_disposition(conversation, snapshot, decision['decision'])
        uncertain = False
        for task_id, record in conversation['dispositions'].items():
            if record['decision'] == 'go':
                record['transcriptVerification'] = 'verified' if verified.get(task_id) == 'go' else 'needs_review'
                uncertain |= record['transcriptVerification'] != 'verified'
        conversation['status'] = 'needs_review' if uncertain or not conversation['dispositions'] else 'finished'
        conversation['transcribedAt'] = self._timestamp()
        if primary['id'] not in conversation['dispositions']:
            primary.update(status='needs_review', error='No attributed decision for the initial proposal')
        self.save()
        if self.result_handler:
            for snapshot in conversation['sessions']:
                item = next(i for i in self.state['items'] if i['id'] == snapshot['id'])
                if snapshot['id'] in conversation['dispositions']:
                    await self.result_handler(item)

    async def worker(self):
        while True:
            event_id = await self.jobs.get()
            try:
                async with self.lock:
                    entry = self.state['events'][event_id]
                    if entry['status'] == 'processed':
                        continue
                    entry['status'] = 'processing'
                    self.save()
                    try:
                        await self.event(entry['event'])
                        entry['status'] = 'processed'
                        entry.pop('error', None)
                    except Exception:
                        entry['status'], entry['error'] = 'retryable', 'Processing failed; check call record and server configuration'
                        log.warning('Event %s processing failed', event_id)
                    self.save()
                if entry['status'] == 'retryable' and entry.get('attempts', 0) < 5:
                    entry['attempts'] = entry.get('attempts', 0) + 1
                    self.save()
                    await asyncio.sleep(5 * entry['attempts'])
                    self.jobs.put_nowait(event_id)
            finally:
                self.jobs.task_done()
