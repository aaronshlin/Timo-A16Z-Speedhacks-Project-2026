import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import clock
from app.agent import calling
from app.brain import planner, runner, state
from app.config import settings
from app.context import calendar, router
from app.models import Event, TriggerEvent
from app.phone import dial, test_user

LA = ZoneInfo('America/Los_Angeles')
NOW = datetime(2026, 10, 11, 7, 0, tzinfo=LA)
PHONE = '+15555550100'


def at(hour, minute=0):
    return NOW.replace(hour=hour, minute=minute)


DAY = [
    Event(id='primary:lecture', title='CS 61B Lecture', start=at(8, 30), end=at(10)),
    Event(id='primary:section', title='Math 54 Discussion', start=at(10, 45), end=at(12)),
    Event(id='primary:coffee', title='Coffee with Sarah', start=at(13), end=at(13, 45), location='Blue Bottle, Oxford St'),
    Event(id='primary:standup', title='Team standup', start=at(14), end=at(14, 15), location='https://zoom.us/j/123'),
    Event(id='primary:lab', title='EE 16B Lab', start=at(15), end=at(17)),
    Event(id='primary:block', title='Timo: travel', start=at(12, 10), end=at(12, 50)),
    Event(id='primary:holiday', title='Holiday', start=at(0), end=at(0) + timedelta(days=1)),
]


def trigger(kind, payload=None):
    return TriggerEvent(user_id=PHONE, type=kind, payload=payload or {}, ts=NOW)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.multiple(settings, USE_MOCKS=True, LIVE_CALLS_ENABLED=False, NOW_OVERRIDE='', DEMO_PHONE=PHONE,
                           ANTHROPIC_API_KEY='', SMS_ENABLED=False, CALL_STATE_PATH=str(root / 'calls.db'),
                           CALL_SESSION_LIMIT=5, CALL_COOLDOWN_SECONDS=0),
            patch.object(state, '_DB', root / 'memory.db'),
            patch.object(calendar, 'get_events', side_effect=lambda day: list(DAY) if day == NOW.date() else []),
        ]
        for p in self.patches:
            p.start()
        clock.set_override(NOW.isoformat())
        self.old_service, calling._service = calling._service, None
        dial.outbox.clear(); router.reset()
        test_user.seed()  # the Mission -> the Berkeley campus by BART, usually leaves 8:15am

    def tearDown(self):
        calling._service = self.old_service
        clock.set_override(None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_a_trip_is_a_change_of_place(self):
        trips = planner.trips(planner.events(NOW.date()), planner.user_facts(PHONE))
        self.assertEqual([(t.event.title, t.origin, t.destination, t.depart_at) for t in trips], [
            ('CS 61B Lecture', 'the Mission', 'the Berkeley campus', at(7, 40)),
            ('Coffee with Sarah', 'the Berkeley campus', 'Blue Bottle, Oxford St', at(12, 10)),
            ('EE 16B Lab', 'Blue Bottle, Oxford St', 'the Berkeley campus', at(14, 10)),
        ])

    def test_a_trip_cannot_leave_before_the_last_event_ends(self):
        day = [Event(id='coffee', title='Coffee with Sarah', start=at(12, 15), end=at(12, 45), location='Blue Bottle'),
               Event(id='lab', title='Physics Lab', start=at(13), end=at(15))]
        facts = planner.user_facts(PHONE)
        lab = planner.trips(day, facts)[-1]
        self.assertEqual((lab.depart_at, lab.late_minutes), (at(12, 45), 25))
        self.assertEqual(planner.say(lab, facts), "Leave right when Coffee with Sarah ends at 12:45pm for Physics Lab "
                         "at 1pm. It's about 40 minutes to the Berkeley campus by BART, so you'll be about 25 minutes late.")

    def test_travel_time_follows_the_commute_mode(self):
        self.assertEqual(planner.travel('BART'), (40, 'by BART'))
        self.assertEqual(planner.travel('I usually drive'), (30, 'by car'))
        self.assertEqual(planner.travel('bike to BART'), (40, 'by BART'))
        self.assertEqual(planner.travel(None), (30, None))

    def test_pre_commute_says_when_to_leave(self):
        planned = planner.enrich(trigger('pre_commute'))
        self.assertEqual(planned.payload['plan'], "Leave at 7:40am for CS 61B Lecture at 8:30am. It's about 40 minutes "
                         "to the Berkeley campus by BART. That's 35 minutes earlier than usual.")
        self.assertEqual(planned.payload['depart_at'], at(7, 40).isoformat())
        given = trigger('pre_commute', {'plan': 'Leave now.'})
        self.assertEqual(planner.enrich(given), given)  # a payload that already says what to do wins

    def test_new_event_plans_the_trip_to_it(self):
        planned = planner.enrich(trigger('new_event', {'event_id': 'coffee', 'source': 'google'}))
        self.assertEqual(planned.payload['plan'], "Leave at 12:10pm for Coffee with Sarah at 1pm. It's about 40 minutes "
                         "to Blue Bottle, Oxford St by BART.")
        self.assertEqual(planned.payload['source'], 'google')
        # Already on campus for the lecture before it: no trip, nothing to say.
        self.assertNotIn('plan', planner.enrich(trigger('new_event', {'event_id': 'section'})).payload)

    def test_morning_plan_adds_the_rest_of_the_day(self):
        plan = planner.enrich(trigger('morning_plan')).payload['plan']
        self.assertTrue(plan.startswith('Leave at 7:40am for CS 61B Lecture at 8:30am.'))
        self.assertTrue(plan.endswith('Today: CS 61B Lecture 8:30am, Math 54 Discussion 10:45am, '
                                      'Coffee with Sarah 1pm, Team standup 2pm, and 1 more.'))

    def test_day_plan_calls_twice_then_texts(self):
        plan = planner.day_plan(PHONE)
        self.assertEqual([(o.at, o.channel) for o in plan],
                         [(at(7, 20), 'call'), (at(11, 50), 'call'), (at(13, 50), 'sms')])
        shown = planner.preview(PHONE)
        self.assertIn('7:20am: Timo calls Sam and says:\n  "Leave at 7:40am for CS 61B Lecture at 8:30am.', shown)
        self.assertIn("Then offers: Send your drafted reply to Sarah saying you'll be 10 minutes late", shown)
        self.assertIn('1:50pm: Timo texts Sam:\n  "Leave at 2:10pm for EE 16B Lab at 3pm.', shown)

    def test_the_runner_calls_with_the_planned_time(self):
        self.assertEqual(runner.run(trigger('pre_commute')), 'briefed by call')
        script = calling.service().sessions()[0]['script']
        self.assertIn('Plan: Leave at 7:40am for CS 61B Lecture at 8:30am.', script)

    def test_nothing_to_plan_sends_nothing(self):
        self.assertIsNone(runner.run(trigger('new_event', {'event_id': 'not-on-the-calendar'})))
        with patch.object(calendar, 'get_events', side_effect=calendar.CalendarReadError('down')):
            self.assertIsNone(runner.run(trigger('pre_commute')))
        self.assertEqual((dial.outbox, calling.service().sessions()), ([], []))


if __name__ == '__main__':
    unittest.main()
