"""Consent regressions for fictional sessions; no network or real state access."""
import unittest

from app.agent.demo_sessions import extract_decision, load_fixtures


def turn(speaker, text):
    return {'speaker': speaker, 'text': text}


class DemoSessionEdgeTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = load_fixtures()

    def decision(self, item, question, reply='yes'):
        turns = []
        body = item['action']['args'].get('body')
        if body:
            turns.append(turn('agent', body))
        turns.extend([turn('agent', question), turn('user', reply)])
        return extract_decision({'transcriptTurns': turns}, item)

    def test_negated_task_confirmation_never_authorizes_go(self):
        for item in self.fixtures:
            for prefix in ("Don't ", 'Do not ', "I won't ", 'Never '):
                with self.subTest(session=item['id'], prefix=prefix):
                    question = prefix + item['confirmation'][0].lower() + item['confirmation'][1:]
                    self.assertIsNone(self.decision(item, question))

    def test_positive_framed_confirmation_still_authorizes_go(self):
        for item in self.fixtures:
            with self.subTest(session=item['id']):
                self.assertEqual(self.decision(item, 'To confirm, ' + item['confirmation']), 'go')

    def test_recipient_price_and_destination_corrections_require_clarification(self):
        cases = [
            (self.fixtures[1], "don't send to Sarah, send to Pri"),
            (self.fixtures[0], "don't take me to the office, take me home"),
            (self.fixtures[0], "don't book the $18 ride, book the $25 ride"),
        ]
        for item, reply in cases:
            with self.subTest(session=item['id'], reply=reply):
                self.assertIsNone(self.decision(item, item['confirmation'], reply))

    def test_task_refusal_remains_a_skip(self):
        item = self.fixtures[0]
        for reply in ("don't book it", "don't do it", 'no thanks'):
            with self.subTest(reply=reply):
                self.assertEqual(self.decision(item, item['confirmation'], reply), 'next')


if __name__ == '__main__':
    unittest.main()
