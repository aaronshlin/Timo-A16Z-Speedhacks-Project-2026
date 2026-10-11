"""Current-main calendar/SMS/call proof with isolated state and fake providers."""
import copy
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from app.agent import calling, voice_mcp
from app.agent.calling import fixture_recommendation
from app.agent.conversation import speech_item
from app.agent.engine import atomic_json
from app.brain import memory, state
from app.config import settings
from app.context import calendar, router
from app.models import UserProfile
from app.phone import dial, sms_handler

PHONE = '+15555550987'
TIMO = '+15555550123'
NOW = datetime(2026, 10, 11, 13, 0, tzinfo=ZoneInfo('America/Los_Angeles'))
SECRET = 'whsec_flow_unit'


def signed(event):
    raw = json.dumps(event).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(SECRET.encode(), stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    return raw, {'X-Dial-Event-ID': event['id'], 'X-Dial-Event-Type': event['type'],
                 'X-Dial-Signature': f't={stamp},v1={digest}'}


class IntegratedFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.requests, self.transcript, self.connections = [], [], {}
        self.original_client = httpx.AsyncClient
        self.patches = [
            patch.multiple(settings, USE_MOCKS=False, MOCK_DIAL=True, SMS_ENABLED=False,
                LIVE_CALLS_ENABLED=False, CALL_OPT_IN=False, NOW_OVERRIDE='', POLLER_ENABLED=False,
                DEMO_PHONE=PHONE, USER_NUMBER=PHONE, USER_NUMBER_ID='', DIAL_USER_NUMBER_ID='',
                DIAL_NUMBER=TIMO, TIMO_NUMBER=TIMO, DIAL_FROM_NUMBER_ID='unit-outgoing',
                CALL_TRIGGER_TOKEN='operator', MCP_TOKEN='mcp-unit', CALENDAR_TRIGGER_TOKEN='calendar-unit',
                DIAL_API_KEY='unit-only', DIAL_WEBHOOK_SECRET=SECRET, DIAL_SMS_WEBHOOK_SECRET='',
                CALL_STATE_PATH=str(self.root / 'calls.db'), CALL_ENGINE_STATE_PATH=str(self.root / 'engine.json'),
                CALL_SESSION_LIMIT=5, CALL_COOLDOWN_SECONDS=0, RECEIPT_MODE='dashboard',
                GOOGLE_CALENDAR_IDS=['primary'], CALENDAR_ACTIVE_START_HOUR=9, CALENDAR_ACTIVE_END_HOUR=18,
                CALENDAR_BUFFER_MINUTES=3, CALL_DEMO_WINDOW_START='', CALL_DEMO_WINDOW_END='',
                ALLOW_DEMO_CALENDAR_CALLS=False, MOCK_CALENDAR_PATH='', ANTHROPIC_API_KEY=''),
            patch.object(state, '_DB', self.root / 'memory.db'),
            patch('app.clock.now', return_value=NOW),
            patch('app.brain.memory.now', return_value=NOW),
            patch('app.brain.runner.now', return_value=NOW),
            patch('app.brain.state.now', return_value=NOW),
            patch('app.phone.sms_handler.now', return_value=NOW),
            patch.object(calendar, 'get_access_token', return_value='unit-google-token'),
            patch.object(calendar, '_request', side_effect=self.google),
        ]
        for item in self.patches:
            item.start()
        from app import main
        self.app = main.app
        network = patch('app.main.httpx.AsyncClient', side_effect=lambda **kw:
                        self.original_client(transport=httpx.MockTransport(self.provider), **kw))
        network.start(); self.patches.append(network)
        self.old_service = calling._service
        calling._service = None
        dial.clear_messages(); sms_handler.reset_memory(); router.reset()
        self.headers = {'Authorization': 'Bearer operator'}

    def tearDown(self):
        calling._service = self.old_service
        for item in reversed(self.patches):
            item.stop()
        dial.clear_messages(); sms_handler.reset_memory(); router.reset()
        self.temp.cleanup()

    def google(self, method, url, **kw):
        self.assertEqual(method, 'GET')
        return httpx.Response(200, request=httpx.Request(method, url), json={
            'timeZone': 'America/Los_Angeles', 'items': [
                {'id': 'before', 'summary': 'Morning meeting',
                 'start': {'dateTime': NOW.replace(hour=9).isoformat()}, 'end': {'dateTime': NOW.isoformat()}},
                {'id': 'after', 'summary': 'Next meeting',
                 'start': {'dateTime': (NOW + timedelta(minutes=30)).isoformat()},
                 'end': {'dateTime': NOW.replace(hour=18).isoformat()}}]})

    def provider(self, request):
        self.requests.append((request.method, request.url.path, bytes(request.content)))
        if request.method == 'POST' and request.url.path.endswith('/calls'):
            return httpx.Response(202, json={'call': {'id': 'flow-call'}})
        if request.method == 'GET' and request.url.path.endswith('/calls/flow-call'):
            return httpx.Response(200, json={'call': {'id': 'flow-call', 'transcriptTurns': self.transcript}})
        if request.method == 'GET' and '/context-mcps/' in request.url.path:
            return httpx.Response(200, json={'contextMcp': self.connections[request.url.path.rsplit('/', 1)[-1]]})
        raise AssertionError('Unexpected provider operation: ' + request.method + ' ' + request.url.path)

    def sms(self, client, event_id, body, endpoint='/sms/inbound'):
        raw, headers = signed({'id': event_id, 'type': 'message.received',
                              'data': {'from': PHONE, 'to': TIMO, 'body': body}})
        response = client.post(endpoint, content=raw, headers=headers)
        self.assertEqual(response.status_code, 200)
        return response

    def select_sarah(self):
        fixture = calling.service()._engine_operation(lambda engine: copy.deepcopy(next(
            item for item in engine.state['items'] if item['id'] == 'demo-sarah-reply')))
        state.set_last_pick(PHONE, fixture_recommendation(fixture))
        return fixture

    def tick(self, client):
        response = client.post('/demo/tick', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def wait_for_finished(self, client):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            sessions = client.get('/voice/sessions', headers=self.headers).json()
            if sessions and sessions[0].get('approvedCount') == 1 and sessions[0].get('actionResults'):
                return sessions[0]
            if sessions and sessions[0].get('status') in ('needs_review', 'failed'):
                self.fail('Verified transcript produced ' + sessions[0]['status'] + ' without its expected approval')
            time.sleep(.05)
        self.fail('The signed transcript did not finish its one local simulated action')

    def test_text_profile_calendar_approval_restart_and_status_share_one_result(self):
        pending = memory.stage_facts(PHONE, {'name': 'Johnson'}, 'user_said')
        with TestClient(self.app) as client:
            self.sms(client, 'profile-confirmed', 'Y')
            self.assertEqual(memory.load(PHONE).profile.name, 'Johnson')
            self.assertNotIn(pending.id, [action.id for action in memory.load(PHONE).pending])
            fixture = self.select_sarah()
            first = self.tick(client)
            self.assertEqual(first['pick']['id'], fixture['id'])
            offer = client.get('/outbox', headers=self.headers).json()['outbox'][-1]
            for value in (fixture['action']['args']['recipient'], fixture['action']['args']['body'], fixture['confirmation']):
                self.assertIn(value, offer)
            self.assertIn('Reply Y to confirm', offer)
            self.tick(client)
            self.assertEqual(sum(fixture['confirmation'] in text for text in dial.outbox), 1)
            self.sms(client, 'task-approved', 'Y', '/webhooks/dial')
            self.sms(client, 'task-approved', 'Y', '/sms/inbound')
            session = client.get('/voice/sessions', headers=self.headers).json()[0]
            self.assertEqual(session['approvedCount'], 1)
            self.assertEqual(len(session['actionResults']), 1)
            self.assertIs(session['actionResults'][0]['external_effect'], False)
        calling._service = None  # Reopen the actual main lifespan with persisted temporary files.
        with TestClient(self.app) as client:
            self.sms(client, 'task-approved', 'Y')
            self.sms(client, 'new-message-same-task', 'Y')
            self.sms(client, 'status-after-restart', 'status')
            session = client.get('/voice/sessions', headers=self.headers).json()[0]
            self.assertEqual(session['approvedCount'], 1)
            self.assertEqual(len(session['actionResults']), 1)
            receipt = session['actionResults'][0]['receipt']
            self.assertIn('Simulation only:', receipt)
            self.assertEqual(client.get('/inbox/thread').json()['entries'][-1]['body'], receipt)
            self.assertTrue(any(item.text == receipt for item in memory.load(PHONE).recent))
            self.assertEqual(memory.load(PHONE).profile.name, 'Johnson')
        persisted = json.loads((self.root / 'engine.json').read_text())
        self.assertEqual(len(persisted['mock_results']), 1)
        self.assertEqual(self.requests, [])  # Reads were fake Google; every text stayed local.

    def test_saved_call_preference_calendar_call_transcript_and_text_status_share_result(self):
        settings.LIVE_CALLS_ENABLED = settings.CALL_OPT_IN = True
        state.save_profile(UserProfile(phone=PHONE, name='Johnson', channel='call'))
        with TestClient(self.app) as client:
            fixture = self.select_sarah()
            engine = calling.service().engine
            voice = voice_mcp._owned(engine)
            voice.update(id='voice-context', status='connected', url='https://unit.invalid/voice-mcp/')
            main = {'name': 'Timo main tools unit', 'id': 'main-context', 'status': 'connected', 'url': 'https://unit.invalid/mcp/'}
            atomic_json(voice_mcp._owned_path(engine), voice)
            atomic_json(engine.path.with_name('.timo-main-context-mcp.json'), main)
            self.connections = {'voice-context': {**voice, 'authMode': 'static', 'toolCount': 5},
                                'main-context': {**main, 'authMode': 'static', 'toolCount': 8}}
            raw, headers = signed({'id': 'callback-ready', 'type': 'webhook.ping', 'data': {}, 'relatedObject': None})
            self.assertEqual(client.post('/webhooks/dial', content=raw, headers=headers).status_code, 200)
            ready = client.post('/voice/runtime/ready', json={'poll_calendar': False}, headers=self.headers)
            self.assertEqual(ready.status_code, 200)
            first = self.tick(client)
            self.assertEqual(first['pick']['id'], fixture['id'])
            self.tick(client)
            posts = [record for record in self.requests if record[:2] == ('POST', '/api/v1/calls')]
            self.assertEqual(len(posts), 1)
            outbound = json.loads(posts[0][2])
            self.assertEqual(outbound['to'], PHONE)
            data = speech_item(fixture)
            self.assertIn(data['say'], outbound['outboundInstruction'])
            catalog = json.loads(outbound['outboundInstruction'].split('TASK DATA: ', 1)[1])
            self.assertEqual(catalog[0]['title'], fixture['title'])
            self.assertEqual(dial.outbox, [])
            data = speech_item(fixture)
            self.transcript = [
                {'speaker': 'agent', 'text': 'Hey Johnson, is now a good time?'},
                {'speaker': 'user', 'text': 'Yes.'},
                {'speaker': 'agent', 'text': data['say']},
                {'speaker': 'agent', 'text': 'To: ' + data['recipient'] + '\n' + data['draft']},
                {'speaker': 'agent', 'text': data['confirmation']},
                {'speaker': 'user', 'text': 'Yes, send exactly that.'}]
            raw, headers = signed({'id': 'transcript-complete', 'type': 'call.transcribed', 'data': {'callId': 'flow-call'}})
            client.post('/webhooks/dial', content=raw, headers=headers)
            client.post('/voice/webhooks/dial', content=raw, headers=headers)
            session = self.wait_for_finished(client)
            self.assertEqual(len(session['actionResults']), 1)
            receipt = session['actionResults'][0]['receipt']
            self.assertEqual(dial.outbox, [])  # Completed calls do not send an unsolicited follow-up.
            self.assertTrue(any(item.channel == 'call' and item.text == receipt for item in memory.load(PHONE).recent))
            self.sms(client, 'status-after-call', 'status')
            self.assertEqual(dial.outbox, [f'SMS {PHONE}: {receipt}'])
            self.assertEqual(client.get('/inbox/thread').json()['entries'][-1]['body'], receipt)
            self.assertEqual(len([r for r in self.requests if r[0] == 'POST' and r[1].endswith('/calls')]), 1)


if __name__ == '__main__':
    unittest.main()
