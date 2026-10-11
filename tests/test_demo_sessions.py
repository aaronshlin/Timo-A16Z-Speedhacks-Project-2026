import asyncio
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from app import clock
from app.agent.demo_sessions import (extract_decision, instruction, load_fixtures,
                                     migrate_queue, next_session, simulate)
from app.agent.engine import Engine, atomic_json

NOW = datetime(2026, 10, 11, 15, 0, tzinfo=ZoneInfo('America/Los_Angeles'))
ROOT = Path(__file__).resolve().parents[1]


def turn(speaker, text):
    return {'speaker': speaker, 'text': text}


class DemoSessionTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = load_fixtures()
        self.ride = self.fixtures[0]
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'engine.json'
        self.clock_patch = patch('app.clock.now', return_value=NOW)
        self.clock_patch.start()

    def tearDown(self):
        self.clock_patch.stop()
        self.temp.cleanup()

    def decision(self, turns, item=None):
        return extract_decision({'transcriptTurns': turns}, item or self.ride)

    def confirmed(self, reply='yes', first='do it', item=None):
        item = item or self.ride
        return [turn('agent', 'Hi, this is Timo. Can you talk?'), turn('user', 'Yes, I can talk.'),
                turn('agent', item['say']), turn('user', first),
                turn('agent', item['confirmation']), turn('user', reply)]

    def test_twelve_original_sessions_and_executable_local_handlers(self):
        self.assertEqual(len(self.fixtures), 12)
        state = {'items': [], 'events': {}}
        migrate_queue(state)
        self.assertEqual(next_session(state)['id'], 'demo-ride-home')
        for item in self.fixtures:
            with self.subTest(item=item['id']), \
                 patch('httpx.Client.request', side_effect=AssertionError('outbound vendor')), \
                 patch('httpx.AsyncClient.request', side_effect=AssertionError('outbound vendor')), \
                 patch('app.context.calendar.create_block', side_effect=AssertionError('calendar write')), \
                 patch('app.phone.dial.send_sms', side_effect=AssertionError('SMS')):
                result = simulate(item, 'mock-test:' + item['id'])
                self.assertFalse(result['external_effect'])
                self.assertEqual(result['execution_mode'], 'simulate')
                self.assertTrue(result['receipt'].startswith('Demo only:'))
                self.assertEqual(result['context_storage'], 'local_only')
            bad = copy.deepcopy(item)
            bad['action']['execution_mode'] = 'live'
            with self.assertRaises(ValueError):
                simulate(bad, 'forbidden')

    def test_instructions_speak_say_without_menu_and_read_drafts(self):
        for item in self.fixtures:
            prompt = instruction(item)
            from app.agent.conversation import speech_item
            spoken = speech_item(item)
            self.assertIn(spoken['say'], prompt)
            self.assertIn(item['confirmation'], prompt)
            self.assertNotIn('I choose', prompt)
            self.assertNotIn('Allowed choices:', prompt)
            self.assertNotIn('go / next / hold', prompt)
            self.assertIn('THREE actions', prompt)
            self.assertNotIn('demo', prompt.lower())
            self.assertIn('SIMULATED', prompt)
            if item['action']['args'].get('body'):
                self.assertIn(spoken['draft'], prompt)

    def test_talk_consent_and_spontaneous_task_yes_are_not_go(self):
        self.assertIsNone(self.decision([turn('agent', 'Can you talk?'), turn('user', 'Yes, I can talk.')]))
        self.assertIsNone(self.decision([turn('agent', self.ride['say']), turn('user', 'yes')]))
        self.assertIsNone(self.decision(self.confirmed(reply='Yes, I can talk.')))
        self.assertIsNone(self.decision([turn('user', 'do it')]))

    def test_natural_affirmation_and_exact_task_confirmation(self):
        for reply in ('yes', 'yeah', 'sure, do it', 'go ahead', 'book the Uber', 'sounds good'):
            with self.subTest(reply=reply):
                self.assertEqual(self.decision(self.confirmed(reply=reply)), 'go')
        words = self.confirmed()
        words[-2]['text'] = 'To confirm, request the eighteen dollar Uber X from this venue to the office?'
        self.assertEqual(self.decision(words), 'go')
        self.assertEqual(self.decision(self.confirmed() + [turn('agent', 'Demo choice recorded. Goodbye.'), turn('user', 'Thanks.')]), 'go')

    def test_negations_uncertainty_changed_terms_and_questions(self):
        for reply in ("don't do it", "yes, don't book it", 'no thanks'):
            with self.subTest(reply=reply):
                self.assertEqual(self.decision(self.confirmed(reply=reply)), 'next')
        for reply in ('maybe', 'not sure', 'yes for $25', 'yes, take me home instead', 'yes, can you explain?'):
            with self.subTest(reply=reply):
                self.assertIsNone(self.decision(self.confirmed(reply=reply)))
        self.assertEqual(self.decision(self.confirmed(reply="don't skip it, do it")), 'go')
        self.assertEqual(self.decision(self.confirmed(reply="don't do it yet")), 'hold')
        wrong = self.confirmed()
        wrong[-2]['text'] = 'Request the $25 UberX from this venue to the office?'
        self.assertIsNone(self.decision(wrong))
        self.assertIsNone(self.decision(self.confirmed() + [turn('user', 'Actually, not sure.')]))

    def test_next_hold_and_busy_stay_distinct(self):
        for reply in ('skip this', 'another one', 'next'):
            self.assertEqual(self.decision([turn('agent', self.ride['say']), turn('user', reply)]), 'next')
        for reply in ('later', 'not now', 'hold off', "I'm busy", "can't talk"):
            self.assertEqual(self.decision([turn('agent', self.ride['say']), turn('user', reply)]), 'hold')

    def test_generic_confirmation_wrong_task_and_unattributed_speech(self):
        words = self.confirmed()
        words[-2]['text'] = 'To confirm, your choice is go, correct?'
        self.assertIsNone(self.decision(words))
        words[-2]['text'] = self.fixtures[1]['confirmation']
        self.assertIsNone(self.decision(words))
        words = self.confirmed()
        words[-1]['speaker'] = 'unknown'
        self.assertIsNone(self.decision(words))
        text = '\n'.join(f"{t['speaker']}: {t['text']}" for t in self.confirmed())
        self.assertEqual(extract_decision({'transcript': text}, self.ride), 'go')

    def test_hearing_draft_is_not_send_approval_and_full_draft_required(self):
        item = self.fixtures[2]
        self.assertIsNone(self.decision([turn('agent', item['say']), turn('user', 'yes, let me hear it')], item))
        self.assertIsNone(self.decision(self.confirmed(item=item), item))
        words = [turn('agent', item['say']), turn('user', 'yes, let me hear it'),
                 turn('agent', item['action']['args']['body']), turn('user', 'send that'),
                 turn('agent', item['confirmation']), turn('user', 'yes')]
        self.assertEqual(self.decision(words, item), 'go')
        words[-1]['text'] = 'yes, read it'
        self.assertIsNone(self.decision(words, item))

    def test_pending_migration_by_id_preserves_history_and_ttl(self):
        active = {**self.ride, 'status': 'queued', 'decision': 'pending', 'callId': 'active-call', 'say': 'Original active wording'}
        resolved = {**self.fixtures[1], 'status': 'resolved', 'decision': 'go', 'resultId': 'previous-result'}
        pending = {**self.fixtures[2], 'status': 'pending', 'created_at': NOW.isoformat(),
                   'expires_at': (NOW + timedelta(minutes=30)).isoformat(), 'say': 'Old pending wording'}
        legacy = {'id': 'avatar-heads', 'user': 'johnson', 'priority': 2, 'status': 'pending'}
        state = {'items': [active, resolved, pending, legacy], 'events': {'old-event': {'status': 'processed'}},
                 'mock_results': {'previous-result': {'saved': True}}}
        before_active, before_resolved = copy.deepcopy(active), copy.deepcopy(resolved)
        migrate_queue(state)
        self.assertEqual(active, before_active)
        self.assertEqual(resolved, before_resolved)
        self.assertEqual(pending['say'], self.fixtures[2]['say'])
        self.assertEqual(pending['expires_at'], (NOW + timedelta(minutes=30)).isoformat())
        self.assertEqual(legacy['status'], 'superseded')
        self.assertEqual(state['events'], {'old-event': {'status': 'processed'}})
        self.assertEqual(state['mock_results'], {'previous-result': {'saved': True}})
        before = copy.deepcopy(state)
        self.assertEqual(migrate_queue(state, current=NOW + timedelta(minutes=1)), [])
        self.assertEqual(state, before)

    def test_expired_invalid_and_deferred_sessions_are_skipped(self):
        state = {'items': [], 'events': {}}
        migrate_queue(state)
        state['items'][0]['expires_at'] = NOW.isoformat()
        self.assertEqual(next_session(state)['id'], 'demo-sarah-reply')
        state['items'][1].update(status='deferred', decision='hold')
        self.assertEqual(next_session(state)['id'], 'demo-zacua-follow-up')
        state['items'][2]['action']['execution_mode'] = 'live'
        self.assertEqual(next_session(state)['id'], 'demo-trial-renewal')

    def test_verified_transcript_creates_one_result_after_duplicates_and_restart(self):
        async def exercise():
            requests = []
            def provider(request):
                requests.append(request)
                self.assertEqual(request.method, 'GET')
                return httpx.Response(200, json={'call': {'id': 'verified-demo-call', 'transcriptTurns': self.confirmed()}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                engine = Engine({'LIVE_CALLS_ENABLED': 'false', 'DIAL_API_KEY': 'unit-only'}, self.path, client)
                item = engine.state['items'][0]
                item.update(status='awaiting_transcript', callId='verified-demo-call', idempotencyKey='unique-key')
                event = {'id': 'transcribed-1', 'type': 'call.transcribed', 'data': {'callId': 'verified-demo-call'}}
                self.assertEqual(engine.state['mock_results'], {})
                await engine.event(event)
                self.assertEqual(item['status'], 'resolved')
                self.assertEqual(item['decision'], 'go')
                self.assertTrue(item['receiptBody'].startswith('Demo only:'))
                await engine.event(event)
                restarted = Engine({'LIVE_CALLS_ENABLED': 'false', 'DIAL_API_KEY': 'unit-only'}, self.path, client)
                await restarted.event({**event, 'id': 'duplicate-new-event-id'})
                self.assertEqual(len(restarted.state['mock_results']), 1)
                self.assertEqual(len(requests), 1)
                self.assertEqual(restarted.next_pending()['id'], 'demo-sarah-reply')
                output = subprocess.check_output([sys.executable, '-c',
                    "from app.agent.engine import Engine; import sys; e=Engine({'LIVE_CALLS_ENABLED':'false'},sys.argv[1],None); print(len(e.state['mock_results']))",
                    str(self.path)], cwd=ROOT, text=True)
                self.assertEqual(output.strip(), '1')
        asyncio.run(exercise())

    def test_skip_defer_and_uncertain_never_execute_mock_action(self):
        async def exercise():
            for reply, lifecycle, decision in [('skip this', 'skipped', 'next'), ('later', 'deferred', 'hold'),
                                                ('not sure', 'needs_review', 'pending')]:
                with self.subTest(reply=reply):
                    path = Path(self.temp.name) / (lifecycle + '.json')
                    def provider(request):
                        return httpx.Response(200, json={'call': {'id': lifecycle,
                            'transcriptTurns': [turn('agent', self.ride['say']), turn('user', reply)]}})
                    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                        engine = Engine({'LIVE_CALLS_ENABLED': 'false', 'DIAL_API_KEY': 'unit-only'}, path, client)
                        item = engine.state['items'][0]
                        item.update(status='awaiting_transcript', callId=lifecycle)
                        with patch('app.agent.engine.simulate', side_effect=AssertionError('must not execute')):
                            await engine.event({'type': 'call.transcribed', 'data': {'callId': lifecycle}})
                        self.assertEqual(item['status'], lifecycle)
                        self.assertEqual(item['decision'], decision)
                        self.assertEqual(engine.state['mock_results'], {})
        asyncio.run(exercise())

    def test_stale_pending_instruction_cannot_override_new_voice_builder(self):
        async def exercise():
            requests = []
            def provider(request):
                requests.append(request)
                prompt = json.loads(request.content)['outboundInstruction']
                self.assertIn(self.ride['say'], prompt)
                self.assertIn(self.ride['confirmation'], prompt)
                self.assertNotIn('OLD MENU', prompt)
                self.assertNotIn('I choose', prompt)
                return httpx.Response(201, json={'call': {'id': 'stale-instruction-test'}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                engine = Engine({'LIVE_CALLS_ENABLED': 'true', 'DIAL_API_KEY': 'unit',
                    'DIAL_FROM_NUMBER_ID': 'unit-number', 'USER_PHONE': '+15555550100',
                    'DIAL_WEBHOOK_SECRET': 'whsec_unit'}, self.path, client)
                engine.state['items'][0]['instruction'] = 'OLD MENU: say I choose go'
                status, result = await engine.trigger('johnson')
                self.assertEqual(status, 202)
                self.assertEqual(len(requests), 1)
        asyncio.run(exercise())

    def test_configured_adapter_failure_is_local_only_and_not_retried(self):
        async def exercise():
            class Adapter:
                count = 0
                def record_result(self, result):
                    self.count += 1
                    raise RuntimeError('Unavailable')
            adapter = Adapter()
            def provider(request):
                return httpx.Response(200, json={'call': {'id': 'adapter-call', 'transcriptTurns': self.confirmed()}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                engine = Engine({'LIVE_CALLS_ENABLED': 'false', 'DIAL_API_KEY': 'unit-only'}, self.path, client, alike_adapter=adapter)
                engine.state['items'][0].update(status='awaiting_transcript', callId='adapter-call')
                event = {'type': 'call.transcribed', 'data': {'callId': 'adapter-call'}}
                await engine.event(event)
                await engine.event(event)
                result = next(iter(engine.state['mock_results'].values()))
                self.assertEqual(adapter.count, 1)
                self.assertEqual(result['context_storage'], 'local_only')
                self.assertEqual(result['mirror_status'], 'unavailable')
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
