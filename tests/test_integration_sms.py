"""SMS endpoints, transport and memory integration; all providers are mocked."""

import asyncio
import hashlib
import hmac
import itertools
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import calling
from app.agent.routes import router as agent_router
from app.brain import memory, runner, state
from app.config import settings
from app.context import router
from app.models import TriggerEvent
from app.phone import dial, notify, sms_handler
from app.phone.routes import router as phone_router

PHONE = '+15555550100'
ALIAS = '+15555550101'
TIMO = '+15555550199'
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=ZoneInfo('America/Los_Angeles'))


def signed(event):
    raw = json.dumps(event).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(b'whsec_unit', stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    return raw, {'X-Dial-Event-ID': event['id'], 'X-Dial-Event-Type': event['type'],
                 'X-Dial-Signature': f't={stamp},v1={digest}'}


class SmsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.multiple(settings, USE_MOCKS=True, MOCK_DIAL=True, SMS_ENABLED=False,
                           DEMO_PHONE=PHONE, USER_NUMBER=ALIAS, USER_NUMBER_ID='user-alias',
                           DIAL_USER_NUMBER_ID='', DIAL_NUMBER=TIMO, TIMO_NUMBER=TIMO,
                           DIAL_FROM_NUMBER_ID='timo-id', DIAL_SMS_WEBHOOK_SECRET='',
                           DIAL_WEBHOOK_SECRET='whsec_unit', CALL_TRIGGER_TOKEN='operator',
                           ANTHROPIC_API_KEY=''),
            patch.object(state, '_DB', Path(self.temp.name) / 'memory.db'),
            patch('app.clock.now', return_value=NOW),
            patch('app.phone.sms_handler.now', return_value=NOW),
        ]
        for item in self.patches:
            item.start()
        self.call_service = Mock()
        self.call_service.config = SimpleNamespace(DIAL_WEBHOOK_SECRET='whsec_unit')
        self.call_service.text_reply.return_value = None
        self.call_service.has_uncertain_call.return_value = False
        item = patch('app.agent.calling.service', return_value=self.call_service)
        item.start(); self.patches.append(item)
        router.reset(); sms_handler.reset_memory(); dial.clear_messages()
        app = FastAPI()
        app.include_router(phone_router); app.include_router(agent_router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        dial.clear_messages(); self.temp.cleanup()

    def test_both_signed_endpoints_and_restart_claim_one_event(self):
        event = {'id': 'sms-shared', 'type': 'message.received',
                 'data': {'from': ALIAS, 'to': 'timo-id', 'body': 'next'}}
        raw, headers = signed(event)
        with patch('app.brain.runner.run', return_value='one reply') as run:
            self.assertEqual(self.client.post('/sms/inbound', content=raw, headers=headers).status_code, 200)
            self.assertEqual(self.client.post('/webhooks/dial', content=raw, headers=headers).status_code, 200)
            sms_handler.reset_memory()  # A process-local reset cannot erase durable reservations.
            self.client.post('/sms/inbound', content=raw, headers=headers)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0].user_id, PHONE)
        self.assertEqual([entry['direction'] for entry in dial.thread], ['user'])

    def test_bad_signature_or_header_mismatch_does_not_consume_id(self):
        event = {'id': 'sms-valid-later', 'type': 'message.received',
                 'data': {'from': PHONE, 'to': TIMO, 'body': 'next'}}
        raw, headers = signed(event)
        with patch('app.brain.runner.run', return_value=None) as run:
            self.assertEqual(self.client.post('/sms/inbound', content=raw,
                             headers={**headers, 'X-Dial-Signature': 't=0,v1=wrong'}).status_code, 401)
            self.assertEqual(self.client.post('/sms/inbound', content=raw,
                             headers={**headers, 'X-Dial-Event-ID': 'wrong-id'}).status_code, 400)
            self.assertEqual(self.client.post('/sms/inbound', content=raw, headers=headers).status_code, 200)
            self.assertEqual(run.call_count, 1)

    def test_local_inbound_returns_actual_shared_task_reply_and_memory(self):
        self.call_service.text_reply.return_value = 'Sarah is already taken care of; Pri is next.'
        response = self.client.post('/sms/inbound', json={'from': ALIAS, 'body': 'what is next?'})
        self.assertEqual(response.json(), {'reply': 'Sarah is already taken care of; Pri is next.'})
        self.call_service.text_reply.assert_called_once_with(PHONE, 'what is next?')
        self.assertEqual(dial.outbox, [f'SMS {PHONE}: Sarah is already taken care of; Pri is next.'])
        recent = memory.load(PHONE).recent
        self.assertEqual([(item.direction, item.channel) for item in recent], [('in', 'sms'), ('out', 'sms')])
        self.assertEqual(memory.load(ALIAS).recent, [])

    def test_pending_confirmation_also_returns_reply_and_only_resolves_once(self):
        action = memory.add_pending(PHONE, 'send_email', 'Send prepared reply', {'demo': True})
        for _ in range(2):
            result = self.client.post('/sms/inbound', json={'from': PHONE, 'body': 'Y'}).json()
            if _ == 0:
                self.assertEqual(result['reply'], 'Simulation finished. No email was sent.')
        self.assertNotIn(action.id, [item.id for item in state.get_pending(PHONE)])
        self.assertEqual(sum('Simulation finished. No email was sent.' in line for line in dial.outbox), 1)

    def test_real_mode_local_sms_requires_operator_and_valid_signature(self):
        with patch.object(settings, 'USE_MOCKS', False), patch.object(settings, 'MOCK_DIAL', False):
            self.assertEqual(self.client.post('/sms/inbound', json={'from': PHONE, 'body': 'next'}).status_code, 401)
            with patch('app.brain.runner.run', return_value='reply'):
                result = self.client.post('/sms/inbound', json={'from': PHONE, 'body': 'next'},
                                          headers={'Authorization': 'Bearer operator'})
                self.assertEqual(result.json(), {'reply': 'reply'})
            with patch.object(settings, 'DIAL_WEBHOOK_SECRET', ''):
                raw, headers = signed({'id': 'no-secret', 'type': 'message.received', 'data': {}})
                self.assertEqual(self.client.post('/sms/inbound', content=raw, headers=headers).status_code, 401)

    def test_second_number_persona_requires_explicit_number_id(self):
        event = {'id': 'to-user-1', 'type': 'message.received',
                 'data': {'from': TIMO, 'to': PHONE, 'body': 'Reply Y to send'}}
        with patch('app.phone.test_user.on_text') as answer:
            sms_handler.handle_dial_message(event)
            answer.assert_not_called()
            with patch.object(settings, 'DIAL_USER_NUMBER_ID', 'second-number'):
                sms_handler.handle_dial_message({**event, 'id': 'to-user-2'})
                answer.assert_called_once_with(TIMO, 'Reply Y to send')

    def test_all_sms_paths_obey_same_readiness_gates_and_explicit_recipient(self):
        client = SimpleNamespace(post=AsyncMock(return_value=Mock(raise_for_status=Mock())))
        with patch('app.phone.dial.get_async_client', new=AsyncMock(return_value=client)):
            for use_mocks, mock_dial, enabled in itertools.product([False, True], repeat=3):
                with self.subTest(use_mocks=use_mocks, mock_dial=mock_dial, enabled=enabled):
                    dial.clear_messages(); client.post.reset_mock()
                    with patch.multiple(settings, USE_MOCKS=use_mocks, MOCK_DIAL=mock_dial, SMS_ENABLED=enabled):
                        self.assertTrue(dial.send_sms_now('+15555550888', 'targeted text'))
                        expected = int(not use_mocks and not mock_dial and enabled)
                        self.assertEqual(client.post.await_count, expected)
                        if expected:
                            self.assertEqual(client.post.call_args.kwargs['json']['to'], '+15555550888')
                        self.assertEqual(len(dial.thread), 1)
                        self.assertEqual(dial.thread[0]['to'], '+15555550888')
                        self.assertEqual(dial.outbox, ['SMS +15555550888: targeted text'])
                        dial.mark_timo_delivered('targeted text')
                        self.assertEqual(len(dial.thread), 1)

    def test_call_notification_does_not_send_duplicate_or_fallback_on_unknown(self):
        recommendation = SimpleNamespace(pick=SimpleNamespace(id='shared-task'))
        with patch('app.agent.call.place_call', return_value=True), patch('app.phone.notify.timo_says', new=AsyncMock()) as sms:
            self.assertTrue(asyncio.run(notify._notify(PHONE, recommendation, 'call')))
            sms.assert_not_called()
        self.call_service.has_uncertain_call.return_value = True
        with patch('app.agent.call.place_call', return_value=False), patch('app.phone.notify.timo_says', new=AsyncMock()) as sms:
            self.assertFalse(asyncio.run(notify._notify(PHONE, recommendation, 'call')))
            sms.assert_not_called()

    def test_stable_calendar_opportunities_reach_durable_service_every_tick(self):
        event = TriggerEvent(user_id=PHONE, type='free_slot', payload={'calendar_gap': {'opportunity_id': 'stable'}}, ts=NOW)
        self.call_service.handle_gap.return_value = {'disposition': 'duplicate'}
        self.assertEqual(router.dispatch(event), {'disposition': 'duplicate'})
        self.assertEqual(router.dispatch(event), {'disposition': 'duplicate'})
        self.assertEqual(self.call_service.handle_gap.call_count, 2)


if __name__ == '__main__':
    unittest.main()
