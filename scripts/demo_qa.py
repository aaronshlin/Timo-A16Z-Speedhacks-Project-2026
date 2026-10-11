#!/usr/bin/env python3
# new file, no collision with in-flight app/* work
"""Read-only checks. No tick, trigger, reset, callback ping or vendor request."""
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.timo-demo-package'


def read_env(path):
    import shlex
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not key.strip().replace('_', '').isalnum():
            raise ValueError('Invalid private env-file line')
        value = value.strip()
        if value.startswith(('"', "'")):
            parts = shlex.split(value)
            if len(parts) != 1:
                raise ValueError('Invalid quoted env value')
            value = parts[0]
        values[key.strip()] = value
    return values


def api(origin, path, token='', method='GET', body=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request(origin + path, headers=headers, method=method,
                      data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        raise ValueError('Request failed: HTTP ' + str(exc.code) + ' at ' + path) from None


def qa():
    cfg = read_env(WORK / 'runtime.env')
    meta = json.loads((WORK / 'metadata.json').read_text())
    origin = meta['local_origin']
    token = cfg['CALL_TRIGGER_TOKEN']
    if api(origin, '/health').get('ok') is not True:
        raise ValueError('Health is not ready')
    ready = api(origin, '/voice/readiness', token)
    preview = api(origin, '/voice/preview', token)
    sessions = api(origin, '/voice/sessions', token)
    if ready.get('receipt_mode') != 'dashboard' or ready.get('calendar_writes_enabled') is not False:
        raise ValueError('Dashboard-only/no-calendar-write expectation changed')
    if ready.get('sms_enabled'):
        raise ValueError('Live SMS is not part of this package')
    mock_demo = meta.get('mock_demo') is True
    if mock_demo:
        if (ready.get('calendar_source') != 'mock_calendar' or ready.get('real_calendar_reads')
                or ready.get('live_calls_enabled') or ready.get('call_opt_in')
                or cfg.get('MOCK_DIAL', '').lower() not in ('true', '1')):
            raise ValueError('Mock demo must use mock calendar, mock Dial and disabled live calls')
    elif not ready.get('real_calendar_reads') or not ready.get('google_token_present'):
        raise ValueError('Real-calendar configuration or token file missing')
    if any(s.get('status') in ('reserved', 'placing', 'queued', 'awaiting_transcript', 'call_unknown')
           and s.get('mode') == 'live' for s in sessions):
        raise ValueError('A live session is active/unknown; reconcile before dialing')
    if not preview.get('next_session'):
        raise ValueError('No eligible fixture: expired/resolved queue needs owner review; do not wipe state')
    if meta['mode'] == 'live':
        if not (ready.get('live_calls_enabled') and ready.get('call_opt_in')
                and ready.get('signed_callback_ready') and ready.get('voice_tools_ready')):
            raise ValueError('Live readiness gates are incomplete')
        public = meta.get('public_origin')
        if not public or api(public, '/health').get('ok') is not True:
            raise ValueError('Current public tunnel is unavailable')
        for path in ['/inbox', '/inbox/send', '/sms/inbound', '/mcp/']:
            try:
                api(public, path)
            except ValueError as exc:
                if 'HTTP 404' not in str(exc):
                    raise ValueError('Public exclusion did not return 404: ' + path) from None
            else:
                raise ValueError('Unsafe route is public: ' + path)
    elif ready.get('live_calls_enabled'):
        raise ValueError('Dry mode unexpectedly permits calls')
    print('QA PASS: health, configuration, next fixture and persistent session checks.')
    print('Next fixture: ' + preview['next_session']['id'])
    print('Mock demo: calendar and actions use local fixtures; no provider credentials needed.' if mock_demo
          else 'Calendar token presence is NOT proof of a successful live read.')
    print('Fixtures/actions are simulated; Alike is local-only; outbound SMS is not enabled.')
    return ready, preview


if __name__ == '__main__':
    try:
        qa()
    except Exception as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'QA failed; inspect local configuration.', file=sys.stderr)
        sys.exit(1)
