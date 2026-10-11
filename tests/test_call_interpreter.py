import asyncio
import hashlib
import hmac
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from app.agent import calling
from app.agent.engine import Engine
from app.brain import briefing, memory, runner, state
from app.config import settings
from app.context import router
from app.models import TriggerEvent
from app.phone import dial, test_user

LA = ZoneInfo('America/Los_Angeles')
NOW = datetime(2026, 10, 11, 8, 0, tzinfo=LA)
PHONE = '+15555550100'   # the test user's Dial number
TIMO = '+15550009999'    # Timo's Dial number
PRE_COMMUTE = {'depart_at': (NOW + timedelta(minutes=25)).isoformat(), 'destination': 'the Berkeley campus',
               'done': ['Drafted your reply to Sarah']}


def trigger(kind, payload):
    return TriggerEvent(user_id=PHONE, type=kind, payload=payload, ts=NOW)


def signed(payload):
    raw = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(b'whsec_unit', stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    return raw, {'X-Dial-Event-ID': payload['id'], 'X-Dial-Signature': f't={stamp},v1={digest}'}


class CallInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.multiple(settings, USE_MOCKS=True, LIVE_CALLS_ENABLED=False, CALL_OPT_IN=False, NOW_OVERRIDE='',
            DEMO_PHONE=PHONE, DIAL_NUMBER=TIMO, DIAL_USER_NUMBER_ID='user-number', SMS_ENABLED=False,
            MCP_TOKEN='mcp-unit', CALL_TRIGGER_TOKEN='operator', DIAL_API_KEY='unit-only', DIAL_WEBHOOK_SECRET='whsec_unit',
            ANTHROPIC_API_KEY='', CALL_STATE_PATH=str(self.root / 'calls.db'), CALL_ENGINE_STATE_PATH=str(self.root / 'engine.json'),
            CALL_COOLDOWN_SECONDS=0, CALL_SESSION_LIMIT=5, POLLER_ENABLED=False, RECEIPT_MODE='dashboard')
        self.patch.start()
        self.clock_patch = patch('app.clock.now', return_value=NOW); self.clock_patch.start()
        self.state_patch = patch.object(state, '_DB', self.root / 'memory.db'); self.state_patch.start()
        self.old_service = calling._service; calling._service = None
        dial.outbox.clear(); router.reset()
        test_user.seed()
        self.offer = next(a for a in state.get_pending(PHONE) if a.kind == 'send_email')

    def tearDown(self):
        calling._service = self.old_service
        self.state_patch.stop(); self.clock_patch.stop(); self.patch.stop(); self.tmp.cleanup()

    def test_user_agent_answers_timo_from_the_persona(self):
        self.assertEqual(test_user.answer('Is that you? Reply Y to add it to your profile, N if not.'), 'Y')
        self.assertEqual(test_user.answer("What's your name?"), 'Sam')
        self.assertEqual(test_user.answer('How do you get to work?'), 'BART')
        self.assertEqual(test_user.answer('What time do you usually leave?'), '8:15am')
        self.assertEqual(test_user.answer('Where do you live?'), 'the Mission')
        self.assertEqual(test_user.answer('Where do you work or study?'), 'the Berkeley campus')
        self.assertIsNone(test_user.answer('Reply: go / next / done / scooter / bus / laptop'))
        self.assertIsNone(test_user.answer("Saved to your profile. I'll use it to plan around your day."))
        self.assertIn('Never use tools', test_user.instruction())

    def test_briefing_leads_with_the_plan_and_offers_by_id(self):
        script = briefing.script(PHONE, trigger('pre_commute', PRE_COMMUTE))
        self.assertIn('Plan: Leave at 8:25am for the Berkeley campus.', script)
        self.assertIn('Already done for them: Drafted your reply to Sarah', script)
        self.assertIn(f'Offer [{self.offer.id}]: Send your drafted reply to Sarah', script)
        self.assertIn(f'Always pass user_id {PHONE}', script)
        self.assertEqual(briefing.text(PHONE, trigger('pre_commute', PRE_COMMUTE)),
                         "Leave at 8:25am for the Berkeley campus. Done: Drafted your reply to Sarah. "
                         "Send your drafted reply to Sarah saying you'll be 10 minutes late? Reply Y to go ahead.")

    def test_pre_commute_calls_with_the_briefing(self):
        outcome = runner.run(trigger('pre_commute', PRE_COMMUTE))
        self.assertEqual(outcome, 'briefed by call')
        session = calling.service().sessions()[0]
        self.assertEqual((session['mode'], session['status']), ('dry_run', 'calls_disabled'))
        self.assertIn('approve_action', session['script'])
        self.assertTrue(briefing.last(PHONE).startswith('Leave at 8:25am'))
        self.assertEqual(dial.outbox, [])  # called, not texted

    def test_a_texted_y_runs_the_offer_once(self):
        test_user.text('Y')
        self.assertEqual(dial.outbox, [f'SMS {TIMO}: Y', f'SMS {PHONE}: Done, I sent it.'])
        self.assertEqual(state.get_pending(PHONE), [])
        self.assertIsNone(runner.run(trigger('user_message', {'body': 'Y'})))  # nothing left to answer

    def test_dial_text_webhooks_reach_timo_and_the_test_user(self):
        from app import main
        with TestClient(main.app) as client:
            to_user = {'id': 'evt-to-user', 'type': 'message.received',
                       'data': {'from': TIMO, 'to': PHONE, 'body': 'Send the reply to Sarah? Reply Y to go ahead.'}}
            raw, headers = signed(to_user)
            self.assertEqual(client.post('/webhooks/dial', content=raw, headers=headers).status_code, 200)
            self.assertEqual(dial.outbox, [f'SMS {TIMO}: Y'])  # the user agent answered Timo
            to_timo = {'id': 'evt-to-timo', 'type': 'message.received', 'data': {'from': PHONE, 'to': TIMO, 'body': 'yes'}}
            raw, headers = signed(to_timo)
            client.post('/webhooks/dial', content=raw, headers=headers)
            client.post('/webhooks/dial', content=raw, headers=headers)  # Dial retries: handled once
            self.assertEqual(dial.outbox[1:], [f'SMS {PHONE}: Done, I sent it.'])
            self.assertEqual(state.get_pending(PHONE), [])
            raw, headers = signed({**to_timo, 'id': 'evt-forged'})
            headers['X-Dial-Signature'] = headers['X-Dial-Signature'][:-4] + '0000'
            self.assertEqual(client.post('/webhooks/dial', content=raw, headers=headers).status_code, 401)

    def test_call_tools_act_mid_call_only_for_timo(self):
        from app import main
        memory.log_interaction(PHONE, 'out', 'call', 'Leave at 8:25am for the Berkeley campus.', proactive=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        with patch.object(settings, 'USE_MOCKS', False):  # real mode: /mcp needs the token
            server = uvicorn.Server(uvicorn.Config(main.app, host='127.0.0.1', port=port, log_level='warning'))
            thread = threading.Thread(target=server.run, daemon=True); thread.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    time.sleep(.05)
                url = f'http://127.0.0.1:{port}/mcp/'
                self.assertEqual(httpx.post(url, json={}).status_code, 401)

                async def call(tool, args, agent=TIMO):
                    headers = {'Authorization': 'Bearer mcp-unit', 'X-Dial-Direction': 'outbound',
                               'X-Dial-User-Number': PHONE, 'X-Dial-Agent-Number': agent}
                    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
                        return (await client.call_tool(tool, args)).structured_content

                approve = {'user_id': PHONE, 'action_id': self.offer.id}
                # The test user's own agent sees the account-wide tools too; it must not act.
                self.assertEqual(asyncio.run(call('approve_action', approve, agent=PHONE))['ok'], False)
                self.assertEqual(len(state.get_pending(PHONE)), 1)
                self.assertEqual(asyncio.run(call('approve_action', approve)), {'ok': True, 'say': 'Done, I sent it.'})
                self.assertEqual(asyncio.run(call('approve_action', approve))['say'], "That one's already taken care of.")
                self.assertEqual(asyncio.run(call('text_details', {'user_id': PHONE}))['ok'], True)
                self.assertEqual(dial.outbox, [f'SMS {PHONE}: Leave at 8:25am for the Berkeley campus.'])
                note = {'user_id': PHONE, 'key': 'commute_mode', 'value': 'driving today'}
                self.assertEqual(asyncio.run(call('save_note', note))['ok'], True)
                facts = {fact.key: fact for fact in state.get_facts(PHONE)}
                self.assertEqual((facts['commute_mode'].value, facts['commute_mode'].source), ('driving today', 'user_said'))
                spoofed = {**approve, 'user_id': '+15551112222'}
                self.assertEqual(asyncio.run(call('decline_action', spoofed))['ok'], False)
            finally:
                server.should_exit = True; thread.join(5)

    def test_briefing_call_is_placed_live_and_its_transcript_is_kept(self):
        async def exercise():
            with patch.object(settings, 'LIVE_CALLS_ENABLED', True), patch.object(settings, 'CALL_OPT_IN', True):
                requests = []
                def provider(request):
                    requests.append(request)
                    if request.method == 'POST':
                        return httpx.Response(202, json={'call': {'id': 'brief-call'}})
                    return httpx.Response(200, json={'call': {'id': 'brief-call', 'transcriptTurns': [
                        {'speaker': 'agent', 'text': 'Leave at 8:25. Want me to send your reply to Sarah?'},
                        {'speaker': 'user', 'text': 'Yes, go ahead. Also text me the details.'}]}})
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                    service = calling.service()
                    engine = Engine({'DIAL_API_KEY': 'unit', 'DIAL_FROM_NUMBER_ID': settings.DIAL_FROM_NUMBER_ID,
                                     'USER_PHONE': PHONE, 'DIAL_WEBHOOK_SECRET': 'whsec_unit', 'LIVE_CALLS_ENABLED': 'true'},
                                    self.root / 'engine.json', client)
                    service.bind(engine, asyncio.get_running_loop())
                    service.callbacks_ready = True
                    outcome = await asyncio.to_thread(runner.run, trigger('pre_commute', PRE_COMMUTE))
                    self.assertEqual(outcome, 'briefed by call')
                    body = json.loads(requests[0].content)
                    self.assertEqual(body['to'], PHONE)
                    self.assertIn(f'Offer [{self.offer.id}]', body['outboundInstruction'])
                    await engine.event({'id': 'evt-brief', 'type': 'call.transcribed', 'data': {'callId': 'brief-call'}})
                    session = service.sessions()[0]
                    self.assertEqual((session['status'], session['decision']), ('resolved', 'briefed'))
                    heard = [i.text for i in memory.load(PHONE).recent if i.direction == 'in' and i.channel == 'call']
                    self.assertEqual(heard, ['Yes, go ahead. Also text me the details.'])
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
