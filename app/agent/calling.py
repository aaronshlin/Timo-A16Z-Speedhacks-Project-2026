"""Persistent opportunity reservations around the verified asynchronous call Engine."""
import asyncio
import fcntl
import copy
import json
import inspect
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from app import clock
from app.config import Settings, settings
from app.models import Candidate, Recommendation
from app.agent.demo_sessions import instruction as session_instruction

CallSettings = Settings
_ROOT = Path(__file__).resolve().parents[2]


def verify_signature(secret, header, raw):
    from app.agent.engine import verify_signature as verify
    return bool(secret) and verify(secret, header, raw)


def instruction(item):
    return session_instruction(item)


def fixture_recommendation(item):
    return Recommendation(pick=Candidate(kind='task', id=item['id'], title=item['title'],
        minutes=1, source='demo_queue'), reason='Fictional session selected by queue priority.',
        speech_text=item['say'], sms_text='Demo only: ' + item['title'], auto_actions=[])


def real_clock_required(config):
    return config.LIVE_CALLS_ENABLED or not config.USE_MOCKS


def validate_clock(config):
    if real_clock_required(config) and (config.NOW_OVERRIDE or getattr(clock, '_override', None)):
        raise ValueError('Real calendar reads and live calls require the actual clock; clear NOW_OVERRIDE.')


class CallService:
    def __init__(self, config=None):
        self.config = config or settings
        self.lock = threading.RLock()
        self.path = Path(self.config.CALL_STATE_PATH or _ROOT / 'data/calls.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = self.loop = None
        self.callbacks_ready = False
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS opportunities (opportunity_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
                    mode TEXT NOT NULL, status TEXT NOT NULL, created_epoch REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trigger_events (event_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL);
            ''')
            # Preserve reservations from the previous calling adapter when present.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='calling_state'").fetchone():
                row = db.execute('SELECT payload FROM calling_state WHERE id=1').fetchone()
                for s in json.loads(row[0]).get('sessions', []) if row else []:
                    key = s.get('calendarTrigger', {}).get('windowId') or 'legacy:' + s['sessionId']
                    mode = 'live' if s['mode'] == 'live' else 'dry_run'
                    db.execute('INSERT OR IGNORE INTO opportunities VALUES (?,?,?,?,?,?)',
                               (key, s['sessionId'], mode, s['status'], s['createdEpoch'], json.dumps(s)))
            # A crash after reservation may already have initiated a call. Never resubmit it.
            for key, payload in db.execute("SELECT opportunity_id,payload FROM opportunities WHERE mode='live' AND status IN ('reserved','placing')"):
                s = json.loads(payload)
                s.update(status='call_unknown', error='Reserved call interrupted; reconcile before retrying')
                db.execute('UPDATE opportunities SET status=?,payload=? WHERE opportunity_id=?', ('call_unknown', json.dumps(s), key))
            for key, payload in db.execute("SELECT opportunity_id,payload FROM opportunities WHERE mode='sms' AND status='reserved'"):
                item = json.loads(payload)
                item.update(status='sms_unknown', error='Reserved text interrupted; delivery is uncertain')
                db.execute('UPDATE opportunities SET status=?,payload=? WHERE opportunity_id=?',
                           ('sms_unknown', json.dumps(item), key))
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def bind(self, engine, loop):
        self.engine, self.loop = engine, loop
        engine.result_handler = self.on_result
        engine.miss_handler = self.on_missed
        engine.context_handler = self.context_for

    def preview_next(self):
        if self.engine:
            return self.engine.next_pending()
        from app.agent.engine import Engine
        path = Path(self.config.CALL_ENGINE_STATE_PATH or _ROOT / 'calling-mvp/.timo-state.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Disabled Engine loads/migrates the local queue without a Dial client.
            engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, path, None)
            return engine.next_pending()

    def _engine_operation(self, callback):
        """Use the one live Engine, or the same locked file with all calling disabled."""
        async def apply(engine):
            result = callback(engine)
            return await result if inspect.isawaitable(result) else result
        if self.engine and self.loop:
            return asyncio.run_coroutine_threadsafe(apply(self.engine), self.loop).result(timeout=15)
        from app.agent.engine import Engine
        path = Path(self.config.CALL_ENGINE_STATE_PATH or _ROOT / 'calling-mvp/.timo-state.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, path, None)
            engine.result_handler = lambda item: self.on_result(item, engine)
            engine.context_handler = self.context_for
            return asyncio.run(apply(engine))

    def context_for(self, phone=None):
        # Private facts and pending action details remain in the local memory store.
        from app.brain.profile import get_profile
        return {'channel': get_profile(phone or self.config.DEMO_PHONE).channel, 'facts': [], 'pending': []}

    def _fixture(self, item_id=None):
        from app.agent.demo_sessions import eligible
        if item_id is None:
            return self.preview_next()
        return self._engine_operation(lambda engine: next((copy.deepcopy(item) for item in engine.state['items']
            if item['id'] == item_id and eligible(item)), None))

    def _duplicate(self, opportunity_id, event_id):
        with self.connect() as db:
            alias = db.execute('SELECT opportunity_id FROM trigger_events WHERE event_id=?', (event_id,)).fetchone()
            row = db.execute('SELECT payload FROM opportunities WHERE opportunity_id=?',
                             (alias[0] if alias else opportunity_id,)).fetchone()
        if row:
            session = json.loads(row[0])
            return {'disposition': 'duplicate', 'sessionId': session['sessionId'],
                    'status': session['status'], 'mode': session['mode']}
        return None

    def has_uncertain_call(self, phone):
        self.sessions()
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM opportunities WHERE mode='live' AND status IN "
                "('reserved','placing','queued','awaiting_transcript','call_unknown')").fetchall()
        return any(json.loads(row[0]).get('phone') == phone for row in rows)

    def handle_gap(self, event, mem):
        """Calendar, text and voice use one planner decision and one durable opportunity."""
        from app.brain import briefing, memory, rules, state
        from app.models import FreeWindow
        gap = event.payload['calendar_gap']
        duplicate = self._duplicate(gap['opportunity_id'], gap['event_id'])
        if duplicate:
            return duplicate
        if event.user_id != self.config.DEMO_PHONE:
            return {'disposition': 'suppressed', 'reason': 'wrong_user'}
        # Existing operators explicitly opted into calls before profiles existed.
        if state.load_profile(event.user_id) is None and self.config.CALL_OPT_IN:
            mem = mem.model_copy(update={'profile': mem.profile.model_copy(update={'channel': 'call'})})
        channel = rules.choose_channel(mem, event.type, clock.now(), bool(event.payload.get('urgent')))
        if channel is None:
            return {'disposition': 'suppressed', 'reason': 'quiet_hours_or_rate_limit'}
        rec = state.get_last_pick(event.user_id)
        if rec and rec.pick.source == 'demo_queue' and not self._fixture(rec.pick.id):
            rec = None
        if rec is None and not briefing.offers(mem):
            fixture = self._fixture()
            if fixture:
                rec = fixture_recommendation(fixture)
        window = FreeWindow.model_validate(event.payload['window'])
        if rec is None:
            plan = f'You have {window.minutes} usable minutes before {window.next_event_title or "your next commitment"}.'
            planned = event.model_copy(update={'payload': {**event.payload, 'plan': plan}})
            script = briefing.script(event.user_id, planned) if channel == 'call' else None
            text = briefing.text(event.user_id, planned)
        else:
            planned, script, text = event, None, rec.sms_text
        if channel == 'call':
            result = self.submit(event.user_id, rec, gap['opportunity_id'], gap['event_id'], gap['metadata'], script=script)
            if result['disposition'] == 'accepted':
                memory.log_interaction(event.user_id, 'out', 'call', rec.speech_text if rec else briefing.text(event.user_id, planned, ask=False),
                    proactive=True, pick_id=rec.pick.id if rec else None, event_id='gap:' + result['sessionId'])
                if rec:
                    state.set_last_pick(event.user_id, rec)
            return result
        return self._submit_text(event.user_id, rec, text, gap)

    def _submit_text(self, phone, rec, text, gap):
        from app.brain import memory, state
        from app.phone.dial import send_sms_now
        current_epoch = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            alias = db.execute('SELECT opportunity_id FROM trigger_events WHERE event_id=?', (gap['event_id'],)).fetchone()
            row = db.execute('SELECT payload FROM opportunities WHERE opportunity_id=?',
                (alias[0] if alias else gap['opportunity_id'],)).fetchone()
            if row:
                session = json.loads(row[0])
                return {'disposition': 'duplicate', 'sessionId': session['sessionId'], 'status': session['status'], 'mode': session['mode']}
            session = {'sessionId': str(uuid.uuid4()), 'opportunityId': gap['opportunity_id'], 'mode': 'sms',
                'status': 'reserved', 'phone': phone, 'createdAt': clock.now().isoformat(),
                'createdEpoch': current_epoch, 'recommendation': rec.model_dump(mode='json') if rec else None,
                'calendarTrigger': gap['metadata'], 'callId': None, 'externalEffect': False}
            db.execute('INSERT INTO opportunities VALUES (?,?,?,?,?,?)',
                (gap['opportunity_id'], session['sessionId'], 'sms', 'reserved', current_epoch, json.dumps(session)))
            db.execute('INSERT INTO trigger_events VALUES (?,?)', (gap['event_id'], gap['opportunity_id']))
        try:
            if rec and rec.pick.source == 'demo_queue':
                text = self._engine_operation(lambda engine: engine.text_offer(phone, rec.pick.id, session['sessionId']))
                session['fixtureId'] = rec.pick.id
            session['text'] = text
            sent = send_sms_now(phone, text)
            if rec and rec.pick.source == 'demo_queue':
                self._engine_operation(lambda engine: engine.mark_text_delivery(phone, session['sessionId'], sent))
            else:
                self._engine_operation(lambda engine: engine.clear_text_proposal(phone))
            real = self.config.SMS_ENABLED and not self.config.USE_MOCKS and not self.config.MOCK_DIAL
            session['status'] = ('text_sent' if real else 'text_simulated') if sent else 'sms_unknown'
            session['externalEffect'] = bool(real and sent)
            if not sent:
                session['error'] = 'Text delivery uncertain; do not resend this opportunity'
            if sent:
                memory.log_interaction(phone, 'out', 'sms', text, proactive=True,
                    pick_id=rec.pick.id if rec else None, event_id='gap:' + session['sessionId'])
                if rec:
                    state.set_last_pick(phone, rec)
        except Exception:
            session.update(status='sms_unknown', error='Text delivery uncertain; do not resend this opportunity')
        self._update(session)
        return {'disposition': 'accepted' if session['status'] in ('text_sent','text_simulated') else 'suppressed',
                'sessionId': session['sessionId'], 'status': session['status'], 'mode': 'sms'}

    def text_reply(self, phone, body):
        if phone != self.config.DEMO_PHONE:
            return None
        normalized = body.strip().lower().rstrip('?.!')
        if normalized in ('status', 'what was done', 'what did you do', 'what have you done'):
            if phone != self.config.DEMO_PHONE:
                return None
            def status(engine):
                receipts = [result['receipt'] for result in engine.state.get('mock_results', {}).values()
                            if result.get('receipt')]
                return '\n'.join(dict.fromkeys(receipts[-3:])) if receipts else 'No completed actions are recorded yet.'
            return self._engine_operation(status)
        from app.brain import memory, state
        def reply(engine):
            reference = engine.state.get('text_proposals', {}).get(phone)
            if reference:
                proposal = next((i for i in engine.state['items'] if i['id'] == reference['itemId']), {})
                recent = memory.load(phone).recent
                last_ask = next((i.text for i in reversed(recent) if i.direction == 'out' and i.channel == 'sms' and 'reply y' in i.text.lower()), '')
                from app.agent.demo_sessions import normalize
                if last_ask and normalize(proposal.get('confirmation', '')) not in normalize(last_ask):
                    async def answer_latest():
                        await engine.clear_text_proposal(phone)
                        return None
                    return answer_latest()
            last = state.get_last_pick(phone)
            if not reference and normalized in ('next', 'what else', 'another task') and last and last.pick.source == 'demo_queue':
                item = engine.next_pending()
                return engine.text_offer(phone, item['id'], 'sms:' + str(uuid.uuid4())) if item else 'No unexpired pending tasks remain.'
            return engine.text_reply(phone, body)
        return self._engine_operation(reply)

    def mark_text_reply_delivery(self, phone, message, delivered):
        if phone == self.config.DEMO_PHONE:
            self._engine_operation(lambda engine: engine.mark_text_reply_delivery(phone, message, delivered))

    def _update(self, session):
        with self.connect() as db:
            db.execute('UPDATE opportunities SET status=?,payload=? WHERE session_id=?',
                       (session['status'], json.dumps(session), session['sessionId']))

    def sessions(self):
        with self.connect() as db:
            rows = db.execute('SELECT payload FROM opportunities ORDER BY created_epoch').fetchall()
        result = [json.loads(row[0]) for row in rows]
        if self.engine:
            items = {i.get('opportunitySessionId', i['id']): copy.deepcopy(i) for i in self.engine.state['items']}
            for session in result:
                conversation = self.engine.state.get('conversations', {}).get(session['sessionId'])
                if conversation:
                    primary = next((i for i in self.engine.state['items'] if i['id'] == conversation['primaryItemId']), {})
                    for key in ('decision', 'receiptBody', 'resultId', 'actionStatus', 'contextStorage'):
                        if key in primary:
                            session[key] = primary[key]
                    session.update(conversationStatus=conversation['status'], callId=conversation.get('callId'),
                        conversationId=conversation['id'], maxActions=3,
                        approvedCount=len(conversation['actionIds']),
                        taskDecisions=copy.deepcopy(conversation['dispositions']),
                        actionResults=[copy.deepcopy(self.engine.state['mock_results'][rid])
                            for rid in conversation['actionIds'] if rid in self.engine.state['mock_results']],
                        externalEffect=False, receiptStatus='dashboard_only')
                    if conversation.get('channel') != 'sms':
                        session['status'] = conversation['status']
                    self._update(session)
                    continue
                item = items.get(session['sessionId'])
                if item:
                    for key in ('status','callId','decision','receiptBody','receiptStatus','error','resultId','actionStatus','contextStorage'):
                        if key in item:
                            session[key] = item[key]
                    self._update(session)
        return result

    def get_session(self, session_id):
        return next((s for s in self.sessions() if s['sessionId'] == session_id), None)

    def submit(self, phone, rec, opportunity_id, event_id, metadata=None, script=None):
        validate_clock(self.config)
        if phone != self.config.DEMO_PHONE:
            return {'disposition': 'suppressed', 'reason': 'wrong_user'}
        self.sessions()
        live = self.config.LIVE_CALLS_ENABLED
        current_epoch = time.time()
        with self.connect() as db:
            # SQLite's write transaction and primary key enforce dedupe across processes.
            db.execute('BEGIN IMMEDIATE')
            alias = db.execute('SELECT opportunity_id FROM trigger_events WHERE event_id=?', (event_id,)).fetchone()
            row = db.execute('SELECT payload FROM opportunities WHERE opportunity_id=?', (alias[0] if alias else opportunity_id,)).fetchone()
            if row:
                s = json.loads(row[0])
                return {'disposition': 'duplicate', 'sessionId': s['sessionId'], 'status': s['status'], 'mode': s['mode']}
            if live:
                if not self.callbacks_ready:
                    return {'disposition': 'suppressed', 'reason': 'waiting_for_signed_callback'}
                if (not self.config.CALL_OPT_IN or not re.fullmatch(r'\+[1-9]\d{7,14}', phone)
                    or not self.config.DIAL_API_KEY or not self.config.DIAL_FROM_NUMBER_ID
                    or not self.config.DIAL_WEBHOOK_SECRET or not self.config.CALL_TRIGGER_TOKEN
                    or not self.engine or not self.loop):
                    return {'disposition': 'suppressed', 'reason': 'live_call_not_ready'}
                if (self.engine.env.get('VOICE_TOOLS_REQUIRED') == 'true'
                        and (self.engine.env.get('VOICE_TOOLS_ENABLED') != 'true'
                             or self.engine.env.get('MAIN_TOOLS_ENABLED') != 'true')):
                    return {'disposition': 'suppressed', 'reason': 'voice_tools_not_ready'}
                from app.brain.profile import get_profile
                hours = get_profile(phone).quiet_hours
                hour = clock.now().hour
                if hours and ((hours[0] < hours[1] and hours[0] <= hour < hours[1]) or
                              (hours[0] >= hours[1] and (hour >= hours[0] or hour < hours[1]))):
                    return {'disposition': 'suppressed', 'reason': 'quiet_hours'}
                prior = db.execute("SELECT status,created_epoch FROM opportunities WHERE mode='live' ORDER BY created_epoch").fetchall()
                if any(status in ('reserved','placing','queued','awaiting_transcript','call_unknown') for status, _ in prior):
                    return {'disposition': 'suppressed', 'reason': 'active_or_unknown_call'}
                if len(prior) >= self.config.CALL_SESSION_LIMIT:
                    return {'disposition': 'suppressed', 'reason': 'session_budget'}
                if prior and current_epoch - prior[-1][1] < self.config.CALL_COOLDOWN_SECONDS:
                    return {'disposition': 'suppressed', 'reason': 'cooldown'}
            # Preserve the planner's recommendation; fixtures are selected only for their own source.
            fixture = None
            if script is None and rec is not None and rec.pick.source != 'demo_queue':
                from app.brain import briefing
                from app.models import TriggerEvent
                script = briefing.script(phone, TriggerEvent(user_id=phone, type='free_slot', ts=clock.now(),
                    payload={'plan': rec.speech_text}))
            elif script is None:
                fixture = self._fixture(rec.pick.id if rec else None)
                if not fixture:
                    return {'disposition': 'suppressed', 'reason': 'no_eligible_demo_session'}
                rec = fixture_recommendation(fixture)
            s = {'sessionId': str(uuid.uuid4()), 'idempotencyKey': str(uuid.uuid4()), 'opportunityId': opportunity_id,
                 'mode': 'live' if live else 'dry_run', 'status': 'reserved' if live else 'calls_disabled',
                 'phone': phone, 'recommendation': rec.model_dump(mode='json') if rec else None, 'script': script,
                 'createdAt': clock.now().isoformat(),
                 'createdEpoch': current_epoch, 'callId': None, 'decision': None, 'receiptStatus': None,
                 'externalEffect': False, 'calendarTrigger': metadata,
                 'fixtureId': fixture['id'] if fixture else None, 'fixture': fixture}
            db.execute('INSERT INTO opportunities VALUES (?,?,?,?,?,?)',
                       (opportunity_id, s['sessionId'], s['mode'], s['status'], current_epoch, json.dumps(s)))
            db.execute('INSERT INTO trigger_events VALUES (?,?)', (event_id, opportunity_id))
        # Reservation is committed before any Dial request, even if we crash here.
        if live:
            try:
                future = asyncio.run_coroutine_threadsafe(self._dial(s), self.loop)
                result = future.result(timeout=15)
                s.update(status=result.get('status', 'call_unknown'), callId=result.get('callId'), error=result.get('error'))
            except Exception:
                s.update(status='call_unknown', error='Placement uncertain; reconcile in Dial before retrying')
            self._update(s)
        return {'disposition': 'accepted' if s['status'] in ('queued','calls_disabled') else 'suppressed',
                'sessionId': s['sessionId'], 'status': s['status'], 'mode': s['mode']}

    async def _dial(self, session):
        if session.get('script'):
            return await self._dial_briefing(session)
        async with self.engine.lock:
            item = next((i for i in self.engine.state['items'] if i['id'] == session['fixtureId']), None)
            if not item or any(item.get(key) != session['fixture'].get(key)
                               for key in ('action', 'say', 'confirmation', 'receipt')):
                return {'status': 'needs_review', 'error': 'Fixture proposal changed after reservation'}
            item.update(opportunitySessionId=session['sessionId'], idempotencyKey=session['idempotencyKey'])
            self.engine.save()
            _, result = await self.engine.trigger('johnson', item['id'])
            return result

    async def _dial_briefing(self, session):
        """Place a briefing call outside the demo-task queue.

        The user acts mid-call through Timo's /mcp tools, so there is no task catalog or
        conversation. The item has no options, so its webhooks take the Engine's per-item path:
        a missed call reaches miss() and the transcript reaches on_result() as 'briefed'.
        """
        from app.agent.engine import DialHTTPError
        engine = self.engine
        if engine.env.get('LIVE_CALLS_ENABLED') != 'true':
            return {'status': 'failed', 'error': 'Live calls disabled'}
        item = {'id': session['sessionId'], 'user': 'johnson', 'priority': 0, 'status': 'placing',
                'idempotencyKey': session['idempotencyKey'], 'title': 'Timo briefing',
                'instruction': session['script'],
                'receiptMessage': 'Timo briefing call finished; any actions ran during the call.'}
        async with engine.lock:
            engine.state['items'].append(item)
            engine.save()  # Reserved before placement, like a fixture call.
            try:
                response = await engine.api('POST', '/calls', {
                    'to': engine.env['USER_PHONE'], 'fromNumberId': engine.env['DIAL_FROM_NUMBER_ID'],
                    'outboundInstruction': session['script'], 'language': 'en-US', 'maxCallDurationSeconds': 180,
                }, {'Idempotency-Key': item['idempotencyKey']})
                item['callId'] = response['call']['id']
                if not isinstance(item['callId'], str) or not item['callId']:
                    raise ValueError('Missing call.id')
                item['status'] = 'queued'
            except Exception as error:
                item['status'] = 'failed' if isinstance(error, DialHTTPError) else 'call_unknown'
                item['error'] = str(error) if isinstance(error, DialHTTPError) else 'Call response uncertain; reconcile in Dial before retrying'
            engine.save()
        return {'status': item['status'], 'callId': item.get('callId'), 'error': item.get('error')}

    def place(self, phone, rec):
        key = 'manual:' + str(uuid.uuid4())
        return self.submit(phone, rec, key, key)['disposition'] == 'accepted'

    def place_script(self, phone, script):
        key = 'briefing:' + str(uuid.uuid4())
        return self.submit(phone, None, key, key, script=script)['disposition'] == 'accepted'

    def accept(self, event):
        if not self.engine:
            raise RuntimeError('Call Engine is not running')
        if event['type'] == 'webhook.ping':
            self.callbacks_ready = True
        event_id = event['id']
        entry = self.engine.state['events'].get(event_id)
        if not entry:
            self.engine.state['events'][event_id] = {'event': event, 'status': 'pending', 'attempts': 0}
            self.engine.save()
            self.engine.jobs.put_nowait(event_id)
        return bool(entry)

    def record(self, session, choice):
        from app.agent.actions import prepare
        from app.brain.state import set_last_pick
        rec = Recommendation.model_validate(session['recommendation'])
        set_last_pick(session['phone'], rec)
        session.update(status='resolved', decision=choice, resultId='result:' + session['sessionId'],
                       memoryStatus='recorded', receiptStatus='dashboard_only',
                       actionStatus='simulated' if choice == 'go' else 'recorded',
                       action=prepare(rec) if choice == 'go' else None,
                       receiptBody=f'Timo recorded {choice}: {rec.pick.title}. Demo only; no external work executed.')
        self._update(session)

    def record_briefing(self, session, user_turns):
        from app.brain import memory
        said = ' '.join(turn for turn in user_turns if turn).strip()
        if said:
            # What the user said on the call, kept like a text: logged, and stated facts learned.
            memory.log_interaction(session['phone'], 'in', 'call', said, event_id='call-user:' + session['sessionId'])
            memory.learn(session['phone'], said, memory.PROFILE_KEYS)
        session.update(status='resolved', decision='briefed', receiptStatus='dashboard_only',
                       receiptBody='Timo briefing call finished; any actions ran during the call.')
        self._update(session)

    def missed_text(self, rec, session):
        minutes = (session.get('calendarTrigger') or {}).get('usableMinutes')
        gap = f'{minutes}-min gap' if minutes else 'free block'
        return (f'Missed you - called about your {gap}. {rec.pick.title}, '
                f'about {rec.pick.minutes} min. Reply go and I will set it up.')

    async def miss(self, item, ended):
        """One text after an unanswered call, then nothing. Never a second call.

        AGENTS.md rule 7 caps calls at 2 a day and says to stop calling a user who
        keeps missing them, so the follow-up is a text the user can answer whenever.
        """
        from app.agent.demo_sessions import executable
        if executable(item):
            return  # Fixture calls never dispatch SMS or bypass task confirmation.
        session = self.get_session(item.get('opportunitySessionId', item['id']))
        if not session or session.get('fixtureId') or session.get('missTextSent'):
            return
        if session.get('script') and not session.get('recommendation'):
            # A planner briefing has no pick: text the plan it would have given.
            sent = await asyncio.to_thread(self._text_briefing, session['phone'])
            if sent is None:
                return
        else:
            from app.brain.state import set_last_pick
            from app.phone.dial import send_sms
            rec = Recommendation.model_validate(session['recommendation'])
            # "go" is answered by sms_handler, which reads the last pick.
            set_last_pick(session['phone'], rec)
            sent = bool(await send_sms(self.config.DIAL_FROM_NUMBER_ID, session['phone'], self.missed_text(rec, session)))
        session.update(missedReason=ended, missTextSent=sent)
        self._update(session)

    def _text_briefing(self, phone):
        """None when there is nothing to send."""
        from app.brain import briefing, memory
        from app.phone.dial import send_sms_now
        text = briefing.details(phone)
        if text is None:
            return None
        sent = bool(send_sms_now(phone, text))
        if sent:
            memory.log_interaction(phone, 'out', 'sms', text)
        return sent

    async def on_missed(self, item, ended):
        # A failed text must not mark the webhook retryable; the call is still over.
        try:
            await self.miss(item, ended)
        except Exception as error:
            print(f'missed-call text failed for {item.get("id")}: {error}')

    async def on_result(self, item, engine=None):
        engine = engine or self.engine
        from app.brain import memory, state
        from app.agent.demo_sessions import executable
        if executable(item) and item.get('decision') in ('go', 'next', 'hold'):
            conversation = engine.state.get('conversations', {}).get(item.get('conversationId'), {}) if engine else {}
            phone = conversation.get('phone') or self.config.DEMO_PHONE
            reference = item.get('resultId') or ':'.join(['task', item.get('conversationId', ''), item['id'], item['decision']])
            memory.log_interaction(phone, 'out', 'sms' if conversation.get('channel') == 'sms' else 'call',
                item.get('receiptBody') or f'Recorded {item["decision"]}: {item["title"]}.',
                pick_id=item['id'], event_id=reference)
            state.set_last_pick(phone, fixture_recommendation(item))
        if engine and item.get('conversationId') in engine.state.get('conversations', {}):
            conversation = engine.state['conversations'][item['conversationId']]
            said = ' '.join(str(turn) for turn in conversation.get('userTurns', []) if turn).strip()
            if said:
                memory.log_interaction(conversation.get('phone') or self.config.DEMO_PHONE, 'in', 'call', said,
                    event_id='transcript:' + conversation['id'])
            session = self.get_session(conversation['id'])
            if session:
                session.update(conversationId=conversation['id'], approvedCount=len(conversation['actionIds']),
                    taskDecisions=copy.deepcopy(conversation['dispositions']),
                    actionResults=[copy.deepcopy(engine.state['mock_results'][rid])
                        for rid in conversation['actionIds'] if rid in engine.state['mock_results']],
                    receiptStatus='dashboard_only', conversationStatus=conversation['status'])
                if conversation.get('channel') != 'sms':
                    session['status'] = conversation['status']
                self._update(session)
            return
        session = self.get_session(item.get('opportunitySessionId', item['id']))
        if not session:
            return
        if session.get('script'):
            await asyncio.to_thread(self.record_briefing, session, item.get('userTurns', []))
        elif session.get('fixtureId'):
            decision = item['decision']
            session.update(status={'go': 'resolved', 'next': 'skipped', 'hold': 'deferred'}[decision],
                decision=decision, receiptStatus='dashboard_only', receiptBody=item['receiptBody'],
                actionStatus=item['actionStatus'], externalEffect=False,
                contextStorage=item.get('contextStorage', 'local_only'))
            if item.get('resultId'):
                session.update(resultId=item['resultId'], mockResult=copy.deepcopy(engine.state['mock_results'][item['resultId']]))
            self._update(session)
        else:
            await asyncio.to_thread(self.record, session, item['decision'])

    async def worker(self):
        await self.engine.worker()


_service = None


def service():
    global _service
    if _service is None:
        _service = CallService()
    return _service
