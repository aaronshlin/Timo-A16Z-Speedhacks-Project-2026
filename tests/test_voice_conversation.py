"""Multi-task conversational consent checks: local fixtures, no calls."""
import copy
import unittest

from app.agent.conversation import (build_instruction, classify_intent,
                                    extract_conversation, public_task_id, speech_item)
from app.agent.demo_sessions import intent, load_fixtures


def turn(speaker, text):
    return {'speaker': speaker, 'text': text}


def confirmed(item, reply='yes'):
    data = speech_item(item)
    turns = [turn('agent', data['say'])]
    if data['recipient']:
        turns.append(turn('agent', data['recipient']))
    if data['draft']:
        turns.append(turn('agent', data['draft']))
    turns.extend([turn('agent', data['confirmation']), turn('user', reply)])
    return turns


class VoiceConversationTests(unittest.TestCase):
    def setUp(self):
        self.sessions = load_fixtures()

    def decisions(self, turns, sessions=None):
        return extract_conversation({'transcriptTurns': turns}, sessions or self.sessions)

    def test_prompt_has_no_spoken_demo_data_and_waits_for_consent(self):
        prompt = build_instruction(self.sessions[0], self.sessions)
        self.assertNotIn('demo', prompt.lower())
        self.assertIn('Then WAIT', prompt)
        self.assertIn('THREE actions', prompt)
        self.assertIn('AFTER the attributed call transcript is verified', prompt)
        self.assertNotIn('options', str([speech_item(item) for item in self.sessions]))
        for item in self.sessions:
            data = speech_item(item)
            self.assertIn(data['say'], prompt)
            self.assertIn(data['confirmation'], prompt)
            self.assertNotIn('demo', str(data).lower())
            self.assertEqual(public_task_id(item), 'task-' + str(item['priority']))
        self.assertEqual(self.sessions[2]['action']['args']['body'].count('demo'), 1)

    def test_tools_prompt_binds_prepare_token_and_truthful_background_status(self):
        prompt = build_instruction(self.sessions[0], self.sessions, 'call-123', True)
        for text in ('timo_prepare_task', 'confirmation_token', 'spoken_confirmation', 'draft_read',
                     'timo_task_status(conversation_id)', 'disposition="hold"', 'Queued is not completed'):
            self.assertIn(text, prompt)
        self.assertNotIn('NO live conversation tools', prompt)

    def test_actual_approval_and_next_question_keep_current_task_approved(self):
        sarah = self.sessions[1]
        data = speech_item(sarah)
        speech = 'Here is the exact message to Sarah:\nTo: ' + data['recipient'] + '\n' + data['draft'] + '\n' + data['confirmation']
        turns = [turn('agent', speech), turn('user', "Yeah. That sounds good. What's next?"),
                 turn('user', 'What are the other tasks?')]
        self.assertEqual(self.decisions(turns), [{'itemId': sarah['id'], 'decision': 'go'}])
        self.assertEqual(intent("Yeah. That sounds good. What's next?", sarah), 'go')
        for reply in ("No thanks, what's next?", "Yeah, don't send it. What's next?",
                      "Yes for $25. What's next?", "What's next?", "Maybe, what else?"):
            with self.subTest(reply=reply):
                self.assertFalse(any(r['decision'] == 'go' for r in self.decisions(
                    [turn('agent', speech), turn('user', reply)])))

    def test_actual_compound_refusal_skips_and_allows_second_task(self):
        reply = "No. I'm good. Let's let's talk about other tasks that I have. What what are the things that you can help me with?"
        turns = [turn('agent', self.sessions[0]['say']), turn('user', reply)]
        turns += confirmed(self.sessions[1], 'that would be great')
        self.assertEqual(self.decisions(turns), [
            {'itemId': self.sessions[0]['id'], 'decision': 'next'},
            {'itemId': self.sessions[1]['id'], 'decision': 'go'}])
        self.assertEqual(intent(reply, self.sessions[0]), 'next')

    def test_natural_offer_paraphrase_and_provider_person_role(self):
        turns = [turn('agent', "Want me to order an Uber back to your office for eighteen dollars?"),
                 turn('Person', "No, I'm good. What else can you help with?")]
        turns += confirmed(self.sessions[1])
        self.assertEqual(self.decisions(turns), [
            {'itemId': self.sessions[0]['id'], 'decision': 'next'},
            {'itemId': self.sessions[1]['id'], 'decision': 'go'}])
        plain = 'Agent: ' + self.sessions[0]['confirmation'] + '\nPerson: yes'
        self.assertEqual(extract_conversation({'transcript': plain}, self.sessions),
                         [{'itemId': self.sessions[0]['id'], 'decision': 'go'}])

    def test_task_deferral_continues_while_busy_ends(self):
        ride, sarah = self.sessions[:2]
        turns = [turn('agent', ride['say']), turn('user', 'Hold this one, what else have you got?')]
        turns += confirmed(sarah)
        self.assertEqual(self.decisions(turns), [
            {'itemId': ride['id'], 'decision': 'hold'}, {'itemId': sarah['id'], 'decision': 'go'}])
        busy = [turn('agent', ride['say']), turn('user', "I'm busy, I have to go.")] + confirmed(sarah)
        self.assertEqual(self.decisions(busy), [{'itemId': ride['id'], 'decision': 'hold'}])

    def test_max_three_approved_actions_skips_do_not_consume_limit(self):
        turns = []
        for item in self.sessions[:2]:
            turns.extend([turn('agent', speech_item(item)['say']), turn('user', 'Skip this one')])
        for item in self.sessions[2:6]:
            turns += confirmed(item)
        results = self.decisions(turns)
        self.assertEqual([r['decision'] for r in results], ['next', 'next', 'go', 'go', 'go'])
        self.assertEqual([r['itemId'] for r in results if r['decision'] == 'go'],
                         [item['id'] for item in self.sessions[2:5]])

    def test_overviews_and_questions_never_approve_or_revoke_approved_work(self):
        ride = self.sessions[0]
        for reply in ('What do I have going on?', 'What can you help me with?',
                      'What else have you got?', 'Yes, can you explain?', 'Can you do it?'):
            with self.subTest(reply=reply):
                self.assertFalse(any(r['decision'] == 'go' for r in self.decisions(confirmed(ride, reply))))
        turns = confirmed(ride) + [turn('agent', 'I recorded that for verification after this call.'),
                                  turn('user', 'What do I have going on?')]
        self.assertEqual(self.decisions(turns), [{'itemId': ride['id'], 'decision': 'go'}])

    def test_task_specific_confirmation_separate_answer_and_recipient_draft_required(self):
        sarah = self.sessions[1]
        data = speech_item(sarah)
        examples = [
            [turn('agent', data['say']), turn('user', 'yes')],
            [turn('agent', data['draft']), turn('agent', data['confirmation']), turn('user', 'yes')],
            [turn('agent', data['recipient']), turn('agent', data['confirmation']), turn('user', 'yes')],
            confirmed(sarah)[:-1] + [turn('user', 'yes, read it')],
            confirmed(sarah)[:-1] + [turn('unknown', 'yes')],
        ]
        for turns in examples:
            with self.subTest(turns=turns):
                self.assertFalse(any(r['decision'] == 'go' for r in self.decisions(turns)))
        self.assertEqual(self.decisions(confirmed(sarah)), [{'itemId': sarah['id'], 'decision': 'go'}])

    def test_wrong_question_cannot_reuse_previous_task_confirmation(self):
        ride = self.sessions[0]
        turns = [turn('agent', ride['confirmation']), turn('agent', 'Would you like to hear another task?'),
                 turn('user', 'yes')]
        self.assertEqual(self.decisions(turns), [])
        turns = [turn('agent', 'Can you talk?'), turn('user', 'yes')]
        self.assertEqual(self.decisions(turns), [])

    def test_negations_and_changed_proposals_do_not_go(self):
        ride = self.sessions[0]
        for reply in ("don't do it", "yes, don't book it", 'yes for $25',
                      "don't take me to the office, take me home", 'maybe', 'not sure',
                      'go ahead and send Sarah something else instead'):
            with self.subTest(reply=reply):
                self.assertFalse(any(r['decision'] == 'go' for r in self.decisions(confirmed(ride, reply))))
        for prefix in ("Don't ", 'Do not ', 'Never '):
            turns = [turn('agent', prefix + ride['confirmation']), turn('user', 'yes')]
            self.assertEqual(self.decisions(turns), [])

    def test_natural_affirmatives_and_projected_exact_draft(self):
        ride = self.sessions[0]
        for reply in ('sounds like a plan', 'please take care of that', 'you have my permission',
                      "I'm happy with that", 'that works for me', 'make it happen', 'go for it'):
            with self.subTest(reply=reply):
                self.assertEqual(self.decisions(confirmed(ride, reply)), [{'itemId': ride['id'], 'decision': 'go'}])
        zacua = self.sessions[2]
        self.assertNotIn('demo', speech_item(zacua)['draft'])
        self.assertEqual(self.decisions(confirmed(zacua)), [{'itemId': zacua['id'], 'decision': 'go'}])

    def test_repeated_confirmations_and_explicit_withdrawal(self):
        ride = self.sessions[0]
        self.assertEqual(self.decisions(confirmed(ride) + confirmed(ride)),
                         [{'itemId': ride['id'], 'decision': 'go'}])
        self.assertEqual(self.decisions(confirmed(ride) + [turn('user', 'Actually, not sure.')]), [])
        self.assertEqual(self.decisions(confirmed(ride) + [turn('user', "Actually don't do it.")]),
                         [{'itemId': ride['id'], 'decision': 'next'}])


if __name__ == '__main__':
    unittest.main()
