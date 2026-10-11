"""No-call preview and durable voice recovery checks using isolated state."""
import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.agent.demo_sessions import load_fixtures

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 11, 15, 0, tzinfo=ZoneInfo('America/Los_Angeles'))


class VoiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'engine.json'
        spec = importlib.util.spec_from_file_location('voice_queue_preview',
            ROOT / 'calling-mvp/preview_sessions.py')
        self.preview_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.preview_module)

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_preserves_old_results_history_and_existing_expiry(self):
        item = copy.deepcopy(load_fixtures()[1])
        item.update(status='pending', created_at=(NOW - timedelta(minutes=20)).isoformat(),
                    expires_at=(NOW + timedelta(minutes=10)).isoformat())
        history = {'id': 'old-ride', 'user': 'johnson', 'priority': 0,
                   'status': 'resolved', 'decision': 'go',
                   'resultId': 'mock:historical-call', 'callId': 'historical-call'}
        old_results = {'mock:historical-call': {'id': 'mock:historical-call',
                        'external_effect': False, 'receipt': 'Demo only: saved old result'}}
        old_events = {'historical-event': {'status': 'processed',
                                         'event': {'type': 'call.transcribed'}}}
        self.path.write_text(json.dumps({'items': [history, item], 'events': old_events,
                                        'mock_results': old_results}))
        with patch('app.clock.now', return_value=NOW), \
             patch('httpx.AsyncClient.request', side_effect=AssertionError('no HTTP allowed')), \
             patch('httpx.Client.request', side_effect=AssertionError('no HTTP allowed')):
            first = self.preview_module.preview(self.path)
        self.assertFalse(first['calls_enabled'])
        self.assertFalse(first['opportunity_reserved'])
        before = json.loads(self.path.read_text())
        with patch('app.clock.now', return_value=NOW + timedelta(minutes=1)):
            self.preview_module.preview(self.path)
        after = json.loads(self.path.read_text())
        self.assertEqual(before['mock_results'], old_results)
        self.assertEqual(after['mock_results'], old_results)
        self.assertEqual(after['events'], old_events)
        self.assertIn(history, after['items'])
        refreshed = next(i for i in after['items'] if i['id'] == item['id'])
        self.assertEqual(refreshed['created_at'], item['created_at'])
        self.assertEqual(refreshed['expires_at'], item['expires_at'])
        self.assertEqual(after, before)

    def test_preview_is_disabled_even_when_operator_settings_enable_calls(self):
        from app.config import settings
        with patch('app.clock.now', return_value=NOW), \
             patch.object(settings, 'LIVE_CALLS_ENABLED', True), \
             patch('app.agent.engine.Engine.api', side_effect=AssertionError('no Dial allowed')), \
             patch('app.agent.demo_sessions.simulate', side_effect=AssertionError('no action allowed')):
            result = self.preview_module.preview(self.path)
        self.assertFalse(result['calls_enabled'])
        self.assertEqual(result['next_session']['id'], 'demo-ride-home')
        state = json.loads(self.path.read_text())
        self.assertEqual(state['mock_results'], {})
        self.assertFalse(any(i.get('callId') for i in state['items']))


    def make_conversation(self):
        from app.agent.engine import Engine
        engine = Engine({'LIVE_CALLS_ENABLED': 'false', 'VOICE_TOOLS_ENABLED': 'true'},
                        self.path, client=None)
        catalog = copy.deepcopy(engine.state['items'])
        conversation = {'id': 'isolated-conversation', 'primaryItemId': catalog[0]['id'],
                        'user': 'johnson', 'status': 'queued', 'callId': 'fake-call',
                        'sessions': catalog, 'dispositions': {}, 'approvals': {},
                        'actionIds': [], 'maxActions': 3, 'createdAt': NOW.isoformat()}
        engine.state['conversations'][conversation['id']] = conversation
        engine.state['items'][0].update(status='queued', callId='fake-call')
        engine.save()
        return engine, conversation

    def test_confirmed_queued_job_survives_restart_without_duplicate_execution(self):
        import asyncio
        from app.agent.engine import Engine, simulate as real_simulate

        async def exercise():
            engine, conversation = self.make_conversation()
            prepared = await engine.voice_prepare(conversation['id'], 'task-1')
            queued = await engine.voice_confirm(conversation['id'], 'task-1',
                prepared['confirmation_token'], 'yes', prepared['confirmation'],
                prepared.get('draft') or '')
            self.assertEqual(queued['status'], 'queued')
            self.assertEqual(engine.state['mock_results'], {})
            restarted = Engine(engine.env, self.path, client=None)
            result_id = queued['result_id']
            with patch('app.agent.engine.simulate', wraps=real_simulate) as simulate:
                await restarted._run_action(result_id)
                await restarted._run_action(result_id)
                again = Engine(engine.env, self.path, client=None)
                await again._run_action(result_id)
                self.assertEqual(simulate.call_count, 1)
            self.assertEqual(len(again.state['mock_results']), 1)
            self.assertEqual(again.state['action_jobs'][result_id]['status'], 'finished')
            self.assertFalse(again.state['mock_results'][result_id]['external_effect'])
            duplicate = await again.voice_confirm(conversation['id'], 'task-1',
                prepared['confirmation_token'], 'yes', prepared['confirmation'])
            self.assertTrue(duplicate['duplicate'])
            self.assertEqual(len(again.state['conversations'][conversation['id']]['actionIds']), 1)

        with patch('app.clock.now', return_value=NOW), \
             patch('httpx.AsyncClient.request', side_effect=AssertionError('no call allowed')):
            asyncio.run(exercise())

    def test_three_approvals_cap_survives_restart_and_skips_do_not_count(self):
        import asyncio
        from app.agent.engine import Engine

        async def exercise():
            engine, conversation = self.make_conversation()
            await engine.voice_disposition(conversation['id'], 'task-5', 'next')
            self.assertEqual(conversation['actionIds'], [])
            for task in ('task-1', 'task-2', 'task-3'):
                prepared = await engine.voice_prepare(conversation['id'], task)
                result = await engine.voice_confirm(conversation['id'], task,
                    prepared['confirmation_token'], 'yes', prepared['confirmation'],
                    prepared.get('draft') or '')
                self.assertEqual(result['status'], 'queued')
            restarted = Engine(engine.env, self.path, client=None)
            fourth = await restarted.voice_prepare(conversation['id'], 'task-4')
            self.assertEqual(fourth['status'], 'limit_reached')
            self.assertEqual(len(restarted.state['action_jobs']), 3)
            self.assertEqual(len(restarted.state['conversations'][conversation['id']]['actionIds']), 3)
            self.assertEqual(restarted.state['conversations'][conversation['id']]
                             ['dispositions']['demo-austin-flight']['decision'], 'next')

        with patch('app.clock.now', return_value=NOW):
            asyncio.run(exercise())

    def test_confirmation_after_task_expiry_cannot_queue_a_job(self):
        import asyncio

        async def exercise():
            engine, conversation = self.make_conversation()
            prepared = await engine.voice_prepare(conversation['id'], 'task-1')
            with patch('app.clock.now', return_value=NOW + timedelta(minutes=21)):
                with self.assertRaisesRegex(ValueError, 'expired'):
                    await engine.voice_confirm(conversation['id'], 'task-1',
                        prepared['confirmation_token'], 'yes', prepared['confirmation'])
            self.assertEqual(engine.state['action_jobs'], {})
            self.assertEqual(conversation['actionIds'], [])

        with patch('app.clock.now', return_value=NOW):
            asyncio.run(exercise())

if __name__ == '__main__':
    unittest.main()
