import asyncio
import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.agent import calling
from app.agent.engine import Engine
from app.brain import memory, pending, runner, state
from app.config import settings
from app.models import TriggerEvent

NOW = datetime(2026, 10, 11, 13, tzinfo=ZoneInfo('America/Los_Angeles'))
PHONE = '+15555550100'


class IntegratedGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'engine.json'
        self.patches = [patch('app.clock.now', return_value=NOW), patch.object(state, '_DB', Path(self.tmp.name) / 'memory.db'),
            patch.multiple(settings, USE_MOCKS=True, MOCK_DIAL=True, SMS_ENABLED=False, LIVE_CALLS_ENABLED=False,
                DEMO_PHONE=PHONE, ANTHROPIC_API_KEY='', CALL_ENGINE_STATE_PATH=str(self.path),
                CALL_STATE_PATH=str(Path(self.tmp.name) / 'calls.db'))]
        for p in self.patches:
            p.start()
        self.old_service, calling._service = calling._service, None

    def tearDown(self):
        calling._service = self.old_service
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_corrective_sms_does_not_approve_unrelated_pending_action(self):
        async def offer():
            engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, self.path, None)
            task = engine.state['items'][1]
            await engine.text_offer(PHONE, task['id'], 'sms-proposal')
            await engine.mark_text_delivery(PHONE, 'sms-proposal', True)
        asyncio.run(offer())
        action = memory.add_pending(PHONE, 'send_email', 'Send a different reply', {'demo': True})
        with patch.dict(pending._RUNNERS, {'send_email': lambda *_: self.fail('unrelated action executed')}):
            reply = runner.run(TriggerEvent(user_id=PHONE, type='user_message', payload={'body': 'Yes, change recipient to Bob'}, ts=NOW))
        self.assertIn('No action', reply)
        self.assertEqual([a.id for a in state.get_pending(PHONE)], [action.id])

    def test_undelivered_confirmation_cannot_authorize_action(self):
        async def check():
            engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, self.path, None)
            await engine.text_offer(PHONE, engine.state['items'][0]['id'], 'sms-undelivered')
            reply = await engine.text_reply(PHONE, 'Y')
            self.assertIn('not delivered', reply)
            self.assertEqual(engine.state['mock_results'], {})
        asyncio.run(check())

    def test_stale_call_skip_cannot_overwrite_sms_completed_task(self):
        async def check():
            engine = Engine({'LIVE_CALLS_ENABLED': 'false', 'VOICE_TOOLS_ENABLED': 'true'}, self.path, None)
            task = engine.state['items'][0]
            engine.state['conversations']['call-stale'] = {'id': 'call-stale', 'primaryItemId': task['id'],
                'status': 'queued', 'callId': 'unit-call', 'sessions': [copy.deepcopy(task)], 'channel': 'call',
                'dispositions': {}, 'approvals': {}, 'actionIds': [], 'maxActions': 3}
            await engine.text_offer(PHONE, task['id'], 'sms-approve')
            await engine.mark_text_delivery(PHONE, 'sms-approve', True)
            await engine.text_reply(PHONE, 'Y')
            result = await engine.voice_disposition('call-stale', 'task-1', 'next')
            self.assertTrue(result['duplicate'])
            self.assertEqual((task['decision'], task['status']), ('go', 'resolved'))
            self.assertEqual(len(engine.state['mock_results']), 1)
            self.assertEqual(engine._public_tasks(engine.state['conversations']['call-stale']), [])
        asyncio.run(check())

    def test_legacy_pending_negation_requires_clarification(self):
        action = memory.add_pending(PHONE, 'send_email', 'Send that reply', {'demo': True})
        with patch.dict(pending._RUNNERS, {'send_email': lambda *_: self.fail('negated action executed')}):
            reply = runner.run(TriggerEvent(user_id=PHONE, type='user_message', payload={'body': "Yes, don't send it", 'return_reply': True}, ts=NOW))
        self.assertIn('No action', reply)
        self.assertEqual([a.id for a in state.get_pending(PHONE)], [action.id])

    def test_next_sms_confirmation_can_authorize_after_delivery_and_restart(self):
        async def offer():
            engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, self.path, None)
            await engine.text_offer(PHONE, engine.state['items'][0]['id'], 'sms-first')
            await engine.mark_text_delivery(PHONE, 'sms-first', True)
        asyncio.run(offer())
        with patch('app.brain.runner.send_sms_now', return_value=True):
            reply = runner.run(TriggerEvent(user_id=PHONE, type='user_message', payload={'body': 'next'}, ts=NOW))
            self.assertIn('Reply Y to confirm', reply)
            calling._service = None
            reply = runner.run(TriggerEvent(user_id=PHONE, type='user_message', payload={'body': 'Y'}, ts=NOW))
        self.assertIn('Simulation only:', reply)

    def test_unrelated_reply_does_not_mark_undelivered_task_as_delivered(self):
        async def check():
            engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, self.path, None)
            await engine.text_offer(PHONE, engine.state['items'][0]['id'], 'sms-undelivered')
            reply = await engine.text_reply(PHONE, 'change the recipient')
            await engine.mark_text_reply_delivery(PHONE, reply, True)
            self.assertFalse(engine.state['text_proposals'][PHONE]['delivered'])
            self.assertIn('not delivered', await engine.text_reply(PHONE, 'Y'))
        asyncio.run(check())

    def test_failed_delivery_of_next_task_cannot_authorize_action(self):
        async def offer():
            engine = Engine({'LIVE_CALLS_ENABLED': 'false'}, self.path, None)
            await engine.text_offer(PHONE, engine.state['items'][0]['id'], 'sms-first')
            await engine.mark_text_delivery(PHONE, 'sms-first', True)
        asyncio.run(offer())
        with patch('app.brain.runner.send_sms_now', return_value=False):
            runner.run(TriggerEvent(user_id=PHONE, type='user_message', payload={'body': 'next'}, ts=NOW))
            reply = runner.run(TriggerEvent(user_id=PHONE, type='user_message', payload={'body': 'Y'}, ts=NOW))
        self.assertIn('not delivered', reply)
