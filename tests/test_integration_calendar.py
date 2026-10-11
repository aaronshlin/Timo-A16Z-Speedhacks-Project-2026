"""Integration checks for one calendar opportunity and shared SMS/call tasks."""
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.agent.calling import CallService
from app.brain import memory, state
from app.config import Settings
from app.models import Candidate, FreeWindow, Recommendation, TriggerEvent, UserProfile

PHONE = '+15555550100'
NOW = datetime(2026, 10, 11, 13, 0, tzinfo=ZoneInfo('America/Los_Angeles'))


class CalendarIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Settings(_env_file=None, USE_MOCKS=True, LIVE_CALLS_ENABLED=False,
            SMS_ENABLED=False, MOCK_DIAL=True, CALL_OPT_IN=False, DEMO_PHONE=PHONE,
            CALL_STATE_PATH=str(self.root/'calls.db'), CALL_ENGINE_STATE_PATH=str(self.root/'engine.json'))
        self.patch = patch.object(state, '_DB', self.root/'memory.db'); self.patch.start()
        self.clock = patch('app.clock.now', return_value=NOW); self.clock.start()
        self.service = CallService(self.config)

    def tearDown(self):
        self.clock.stop(); self.patch.stop(); self.tmp.cleanup()

    def gap(self, key='gap-1'):
        window = FreeWindow(start=NOW, end=NOW+timedelta(minutes=20), minutes=17, next_event_title='Meeting')
        return TriggerEvent(user_id=PHONE, type='free_slot', ts=NOW,
            payload={'window': window.model_dump(mode='json'), 'calendar_gap': {
                'opportunity_id': key, 'event_id': key+'-event', 'metadata': {'usableMinutes':17}}})

    def test_sms_gap_shared_fixture_approval_survives_restart_without_repeat(self):
        state.save_profile(UserProfile(phone=PHONE, channel='sms'))
        sent=[]
        with patch('app.phone.dial.send_sms_now', side_effect=lambda phone, body: sent.append(body) or True):
            first=self.service.handle_gap(self.gap(), memory.load(PHONE))
            self.assertEqual(first['status'], 'text_simulated')
            self.assertEqual(len(sent), 1)
            self.assertIn('Reply Y to confirm', sent[0])
            second=self.service.handle_gap(self.gap(), memory.load(PHONE))
            self.assertEqual(second['disposition'], 'duplicate')
            self.assertEqual(first['sessionId'], second['sessionId'])
            reply=self.service.text_reply(PHONE, 'Y')
            self.assertIn('Simulation only:', reply)
            restarted=CallService(self.config)
            repeat=restarted.text_reply(PHONE, 'Y')
            self.assertIn('already handled', repeat)
            third=restarted.handle_gap(self.gap(), memory.load(PHONE))
            self.assertEqual(third['disposition'], 'duplicate')
            self.assertEqual(len(sent), 1)
        engine=json.loads((self.root/'engine.json').read_text())
        self.assertEqual(len(engine['mock_results']), 1)
        result=next(iter(engine['mock_results'].values()))
        self.assertIs(result['external_effect'], False)
        self.assertIn('Simulation only:', restarted.text_reply(PHONE, 'status'))
        self.assertEqual(len(state.get_interactions(PHONE, since=NOW-timedelta(days=1))), 2)
        self.assertEqual(restarted.sessions()[0]['approvedCount'], 1)

    def test_saved_call_choice_and_manual_recommendation_are_preserved(self):
        state.save_profile(UserProfile(phone=PHONE, channel='call'))
        rec=Recommendation(pick=Candidate(kind='task',id='main-task',title='Prepare meeting notes',minutes=5,source='main'),
            reason='Fits',speech_text='Prepare the meeting notes before your appointment.',sms_text='Prepare notes.',auto_actions=[])
        state.set_last_pick(PHONE, rec)
        result=self.service.handle_gap(self.gap(), memory.load(PHONE))
        self.assertEqual(result['status'], 'calls_disabled')
        session=self.service.get_session(result['sessionId'])
        self.assertEqual(session['recommendation'], rec.model_dump(mode='json'))
        self.assertIsNone(session['fixtureId'])
        self.assertIn(rec.speech_text, session['script'])
        manual=self.service.submit(PHONE, rec, 'manual-1', 'manual-event')
        self.assertEqual(self.service.get_session(manual['sessionId'])['recommendation']['pick']['id'], 'main-task')

    def test_explicit_fixture_recommendation_selects_its_id(self):
        from app.agent.calling import fixture_recommendation
        fixture=self.service._engine_operation(lambda engine: copy.deepcopy(next(item for item in engine.state['items']
            if item['id']=='demo-sarah-reply')))
        result=self.service.submit(PHONE, fixture_recommendation(fixture), 'fixture-1','fixture-event')
        self.assertEqual(self.service.get_session(result['sessionId'])['fixtureId'], 'demo-sarah-reply')

    def test_unknown_sms_delivery_remains_reserved_and_never_retries(self):
        state.save_profile(UserProfile(phone=PHONE, channel='sms'))
        with patch('app.phone.dial.send_sms_now', return_value=False) as send:
            first=self.service.handle_gap(self.gap(), memory.load(PHONE))
            self.assertEqual(first['status'], 'sms_unknown')
            second=CallService(self.config).handle_gap(self.gap(), memory.load(PHONE))
            self.assertEqual(second['disposition'], 'duplicate')
            self.assertEqual(second['status'], 'sms_unknown')
            send.assert_called_once()
        # Crash between reservation and a response is equally uncertain.
        session=self.service.sessions()[0];session['status']='reserved';self.service._update(session)
        self.assertEqual(CallService(self.config).sessions()[0]['status'], 'sms_unknown')

    def test_saved_preferences_override_legacy_opt_in_and_quiet_hours(self):
        self.config.CALL_OPT_IN=True
        state.save_profile(UserProfile(phone=PHONE, channel='sms', quiet_hours=(12,14)))
        with patch('app.phone.dial.send_sms_now', side_effect=AssertionError('quiet hours')):
            result=self.service.handle_gap(self.gap(), memory.load(PHONE))
        self.assertEqual(result['reason'], 'quiet_hours_or_rate_limit')
        self.assertEqual(self.service.sessions(), [])
        state.save_profile(UserProfile(phone=PHONE, channel='sms'))
        with patch('app.phone.dial.send_sms_now', return_value=True):
            result=self.service.handle_gap(self.gap(), memory.load(PHONE))
        self.assertEqual(result['mode'], 'sms')


if __name__ == '__main__':
    unittest.main()
