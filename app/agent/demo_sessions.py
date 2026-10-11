"""Fictional queue, attributed confirmations, and local simulated dispatch only."""
import copy
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

def _now():
    # The launcher imports Engine for atomic JSON before selecting its core runtime.
    from app import clock
    return clock.now()

FIXTURES = Path(__file__).resolve().parents[2] / 'calling-mvp/demo_sessions.json'
LEGACY_FIXTURES = {'alice-follow-up', 'avatar-heads', 'coffee-shop'}
ACTIVE = {'placing', 'queued', 'awaiting_transcript', 'call_unknown', 'resolving'}
TOOLS = {
    'rides.request', 'email.reply', 'email.send', 'subscription.cancel_renewal',
    'travel.save_option', 'invite.respond', 'documents.prepare_brief',
    'calendar.create_event', 'returns.initiate', 'agents.resume_work',
}


def executable(item):
    action = item.get('action', {})
    return (item.get('demo') is True and action.get('execution_mode') == 'simulate'
            and action.get('tool') in TOOLS and isinstance(action.get('args'), dict))


def load_fixtures():
    items = json.loads(FIXTURES.read_text())
    seen = set()
    for item in items:
        if (item['id'] in seen or not executable(item)
                or item['options'] != ['go', 'next', 'hold']
                or item['decision'] != 'pending' or item['expires_minutes'] <= 0
                or not item['confirmation'] or not item['say']
                or not item['receipt'].startswith('Demo only:')):
            raise ValueError('Invalid simulated session fixture')
        seen.add(item['id'])
    return items


def migrate_queue(state, fixtures=None, current=None):
    """Update pending IDs, retain active/resolved history, never renew an existing TTL."""
    current = current or _now()
    fixtures = load_fixtures() if fixtures is None else fixtures
    state.setdefault('items', [])
    state.setdefault('events', {})
    state.setdefault('mock_results', {})
    by_id = {item['id']: item for item in state['items']}
    changed = []
    for item in state['items']:
        if item['id'] in LEGACY_FIXTURES and item.get('status') == 'pending':
            item.update(status='superseded', decision='pending', superseded_at=current.isoformat())
            changed.append(item['id'])
    for template in fixtures:
        existing = by_id.get(template['id'])
        if existing is not None and (existing.get('status') != 'pending'
                                     or existing.get('decision', 'pending') != 'pending'):
            continue
        item = existing if existing is not None else {}
        created = item.get('created_at') or current.isoformat()
        expires = item.get('expires_at') or (
            datetime.fromisoformat(created) + timedelta(minutes=template['expires_minutes'])).isoformat()
        before = copy.deepcopy(item)
        item.update(copy.deepcopy(template), status='pending', created_at=created, expires_at=expires)
        if existing is None:
            state['items'].append(item)
            by_id[item['id']] = item
        if item != before:
            changed.append(item['id'])
    if changed:
        state.setdefault('queue_migrations', []).append({'at': current.isoformat(), 'updated_ids': changed})
    return changed


def eligible(item, current=None):
    if not (item.get('status') == 'pending' and item.get('decision') == 'pending' and executable(item)):
        return False
    try:
        expires = datetime.fromisoformat(item['expires_at'])
        return expires.tzinfo is not None and (current or _now()) < expires
    except (KeyError, ValueError, TypeError):
        return False


def next_session(state, user='johnson', current=None):
    items = [item for item in state['items'] if item.get('user') == user and eligible(item, current)]
    return copy.deepcopy(min(items, key=lambda item: (item['priority'], item['id']))) if items else None


def instruction(item, sessions=None, conversation_id=None, tools_enabled=False):
    from app.agent.conversation import build_instruction
    return build_instruction(item, sessions, conversation_id, tools_enabled)


def normalize(text, confirmation=False):
    text = str(text).lower().replace('’', "'").replace('–', '-').replace('—', '-')
    numbers = {'eighteen': '18', 'fifteen': '15', 'thirty five': '35', 'thirty-five': '35',
               'one hundred forty two': '142', 'one hundred and forty two': '142',
               'three twenty five': '3 25', 'four': '4', 'two': '2', 'one': '1', 'five': '5'}
    for word, value in sorted(numbers.items(), key=lambda pair: -len(pair[0])):
        text = re.sub(r'\b' + re.escape(word) + r'\b', value, text)
    text = re.sub(r'\buber\s+x\b', 'uberx', text)
    text = re.sub(r'(\d)(am|pm)\b', r'\1 \2', text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    if confirmation:
        text = re.sub(r'\b(the|a|an|this|that|to|from|at|for|and|i|you|your|me|just|can|may|should|would|do|want|please|is|it|dollar|dollars)\b', ' ', text)
    return ' '.join(text.split())


def task_confirmation(text, item):
    target = normalize(item['confirmation'], True)
    spoken = normalize(text, True)
    spoken = re.sub(r'\s+(?:correct|right)$', '', spoken)
    if not target or not (spoken == target or spoken.endswith(' ' + target)):
        return False
    framing = spoken[:-len(target)].strip()
    body = item.get('action', {}).get('args', {}).get('body')
    if body:
        framing = framing.replace(normalize(body, True), '').strip()
    if re.search(r"\b(?:not|never|cannot|don t|won t|can t)\b", framing):
        return False
    harmless = {'confirm', 'confirming', 'confirmation', 'okay', 'ok', 'so', 'then',
                'let', 's', 'before', 'proceed', 'ready', 'like', 'here', 'draft'}
    return not framing or set(framing.split()) <= harmless


def intent(text, item):
    text = str(text).lower().replace('’', "'")
    request = re.search(r'\b(?:what.s next|what else|what other|anything else|other tasks)\b', text)
    if request:
        prefix = text[:request.start()].strip(' .,!')
        if prefix and intent(prefix, item) == 'go':
            return 'go'
    if re.search(r'\b(can talk|ready to talk|hear it|hear the draft|read it|read the draft|tell me)\b', text):
        return None
    task_defer = re.sub(r"\b(?:don't|do not|never)\s+(?:hold|wait)(?:\s+off)?\b", ' ', text)
    if re.search(r'\b(?:hold (?:this|that|it|off)|save (?:this|that|it) for later|not now|not yet|another time|later)\b', task_defer):
        return 'hold'
    # A compound refusal plus a request for another task is a skip, even when
    # the conversational request ends in a question. Questions alone cannot go.
    if (re.search(r"\b(?:other tasks|other things|another (?:task|one)|something else|anything else|what else|next task)\b", text)
            and not re.search(r'\b(?:instead|change|different|make it)\b', text)):
        return 'next'
    if ('?' in text and (re.match(r'^\s*no\b', text) or re.search(r"\bi(?:'m| am) good\b", text))
            and re.search(r'\b(?:what|other|next|help)\b', text)):
        return 'next'
    if '?' in text:
        return None
    # A negated alternative is not a positive command by itself.
    text = re.sub(r"\b(?:don't|do not|never)\s+(?:skip|hold|wait)(?:\s+off)?\b", ' ', text)
    if re.search(r"\b(later|another time|not now|not yet|not today|hold off|hold|wait|busy|can't talk|cannot talk)\b", text) or re.search(r"(?:don't|do not).*\byet\b", text):
        return 'hold'
    if re.search(r'\b(maybe|not sure|unsure|probably|possibly|i guess|i think|might)\b', text):
        return None
    if re.search(r'\b(instead|change|different (?:price|recipient|address|destination)|make it(?! happen))\b', text) or (re.search(r"\b(?:don't|do not|not)\b", text) and len(re.findall(r'\b(?:send|reply|book|request|get|take|add|start|continue)\b', text)) > 1):
        return None
    if re.search(r'\b(skip|next|pass|another one|something else|not this|no thanks|not interested)\b', text):
        return 'next'
    if re.search(r"\b(don't|do not|never|cannot|can't|won't|not)\b", text):
        return 'next'
    if re.fullmatch(r'\s*no[.! ,]*', text):
        return 'next'
    if re.search(r'\b(cancel|stop)\b', text) and item['action']['tool'] != 'subscription.cancel_renewal':
        return 'next'
    verbs = {
        'rides.request': r'get|book|request|order',
        'subscription.cancel_renewal': r'cancel|stop',
        'email.reply': r'send|reply', 'email.send': r'send',
        'travel.save_option': r'save', 'invite.respond': r'accept|tell',
        'documents.prepare_brief': r'prepare|make|create',
        'calendar.create_event': r'add|protect|create',
        'returns.initiate': r'start|initiate', 'agents.resume_work': r'continue|finish|resume',
    }.get(item['action']['tool'], '')
    affirmative = r'yes|yeah|yep|yup|sure|absolutely|fine|perfect|sounds good|sounds great|sounds fine|sounds lovely|sounds like a plan|that works|works for me|that would be great|happy with that|take care of that|make it happen|you have my permission|i approve|i agree|go ahead|do it|proceed|okay|ok|go'
    if re.search(r'\b(?:' + affirmative + ('|' + verbs if verbs else '') + r')\b', text):
        allowed = set(normalize(item['confirmation']).split()) | set(
            'yes yeah yep yup sure absolutely sounds sound good great fine perfect lovely works would be for with happy '
            'go ahead do it that this please thanks thank you okay ok proceed let s '
            'send reply get book request order save accept tell prepare make create add '
            'protect start initiate continue finish resume cancel stop just the me her him uber '
            'i m d a an like to you have my permission approve agree take care of plan happen then exactly'.split())
        # New amounts, destinations, recipients or content require another clarification.
        if set(normalize(text).split()) <= allowed:
            return 'go'
    return None


def transcript_turns(call):
    turns = call.get('transcriptTurns')
    if not isinstance(turns, list) or not turns:
        turns = []
        for line in str(call.get('transcript') or '').splitlines():
            match = re.match(r'\s*(user|human|caller|person|agent|assistant)\s*:\s*(.+)', line, re.I)
            if match:
                turns.append({'speaker': match[1].lower(), 'text': match[2]})
    return turns


def extract_decision(call, item):
    awaiting = False
    confirmed = None
    agent_words = []
    body = item['action']['args'].get('body')
    for turn in transcript_turns(call):
        if not isinstance(turn, dict):
            awaiting = False
            confirmed = None
            continue
        speaker = str(turn.get('speaker') or turn.get('role') or '').lower()
        text = str(turn.get('text', ''))
        if speaker in ('agent', 'assistant'):
            agent_words.append(normalize(text))
            full_draft = not body or normalize(body) in ' '.join(agent_words)
            awaiting = task_confirmation(text, item) and full_draft
        elif speaker in ('user', 'human', 'caller', 'person'):
            if confirmed and re.fullmatch(r'(?:ok(?:ay)?[ ,]*)?(?:thanks|thank you|bye|goodbye)[.! ,]*', text.lower().strip()):
                continue
            choice = intent(text, item)
            if choice in ('next', 'hold'):
                confirmed = choice
            elif choice == 'go':
                if awaiting:
                    confirmed = 'go'
                elif confirmed != 'go':
                    confirmed = None
            else:
                confirmed = None
            awaiting = False
        else:
            # Unattributed speech is never task authorization.
            awaiting = False
            confirmed = None
    return confirmed


def _mock_record(args):
    return {'mock': True, 'args': copy.deepcopy(args)}


def _mock_brief(args):
    return {'mock': True, 'format': args['format'], 'source_ids': list(args['source_ids']),
            'bullets': [
                'Demo only: agenda reference demo-agenda.',
                'Demo only: status reference demo-status.',
                'Demo only: open decisions reference demo-open-decisions.',
                'Demo only: supplied source IDs are fixtures; no real project facts asserted.',
                'Demo only: verify source documents before any future live brief.',
            ]}


MOCK_HANDLERS = {tool: _mock_record for tool in TOOLS}
MOCK_HANDLERS['documents.prepare_brief'] = _mock_brief


def simulate(item, result_id):
    if not executable(item):
        raise ValueError('Only executable simulated fixtures are supported')
    output = MOCK_HANDLERS[item['action']['tool']](copy.deepcopy(item['action']['args']))
    return {'id': result_id, 'fixture_id': item['id'], 'decision': 'go',
            'execution_mode': 'simulate', 'external_effect': False, 'created_at': _now().isoformat(),
            'tool': item['action']['tool'], 'output': output, 'receipt': item['receipt'],
            'context_storage': 'local_only', 'mirror_status': 'not_configured'}
