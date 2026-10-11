"""Start the one main app for calendar polling or one manual call."""
import argparse
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from datetime import datetime

import httpx
from dotenv import dotenv_values
from setup import setup

ROOT = Path(__file__).resolve().parent
AUTH = ROOT / '.local/share/dial/auth.v1.json'


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()



def wait_for_public_health(origin, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(origin + '/health', timeout=3)
            if response.is_success and response.json().get('ok') is True:
                print('Public callback tunnel is reachable.', flush=True)
                return
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        time.sleep(1)
    raise ValueError('Public callback tunnel was not reachable within 60 seconds; inspect .timo-tunnel.log.')


def manual_call(origin, operator):
    response = httpx.post(origin + '/voice/trigger', headers={'Authorization': 'Bearer ' + operator},
                          json={'event_id': 'manual:' + str(uuid.uuid4()), 'available_minutes': 20}, timeout=20)
    response.raise_for_status()
    result = response.json()
    if result.get('status') != 'queued':
        raise ValueError('Manual call not queued: ' + str(result.get('reason') or result.get('status')))
    print('One call queued. Answer your enrolled phone; respond naturally, then answer the task-specific confirmation.', flush=True)
    for _ in range(150):
        response = httpx.get(origin + '/voice/sessions', headers={'Authorization': 'Bearer ' + operator}, timeout=5)
        response.raise_for_status()
        session = next((s for s in response.json() if s['sessionId'] == result['sessionId']), None)
        if session and session['status'] in ('resolved', 'skipped', 'deferred', 'needs_review', 'failed', 'finished'):
            print('Call result: ' + session['status'], flush=True)
            if session.get('receiptBody'):
                print(session['receiptBody'], flush=True)
            return
        time.sleep(2)
    raise ValueError('Completion unverified; keep saved state and reconcile Dial before another call.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Permit one real call; reads alone never enable calls.')
    parser.add_argument('--manual-call', action='store_true', help='Trigger one recommendation call without calendar polling.')
    parser.add_argument('--mock-calendar', action='store_true', help='Use mock calendar reads, with calls disabled.')
    parser.add_argument('--demo-calendar', type=Path, help='Explicit local calendar snapshot; --live permits a demo phone call, actions still simulated.')
    parser.add_argument('--python', type=Path)
    parser.add_argument('--demo-window', help="Limit real-calendar calls to today's HH:MM-HH:MM window.")
    args = parser.parse_args(argv)
    if args.manual_call and not args.live:
        raise ValueError('Manual dialing requires --live.')
    if args.live and args.mock_calendar and not args.demo_calendar:
        raise ValueError('Mock calendar ticks cannot enable live calls.')
    if args.demo_calendar:
        if args.manual_call or args.demo_window:
            raise ValueError('Demo calendar uses the single poller; do not combine it with manual calling or a real-calendar window.')
        args.demo_calendar = args.demo_calendar.resolve()
        if not args.demo_calendar.is_file():
            raise ValueError('Demo calendar snapshot file not found.')
        args.mock_calendar = True
    project = ROOT.parent
    runtimes = [args.python] if args.python else [project / '.venv/bin/python', project.parent / 'timo/.scaffold-venv/bin/python']
    runtime = next((p for p in runtimes if p and p.is_file()), None)
    if not runtime:
        raise ValueError('Run uv sync in the repository or pass --python PATH.')
    demo_window = None
    if args.demo_window:
        if args.manual_call or args.mock_calendar:
            raise ValueError('A demo calling window uses the real calendar poller.')
        match = re.fullmatch(r'(\d{2}):(\d{2})-(\d{2}):(\d{2})', args.demo_window)
        if not match:
            raise ValueError('Use --demo-window HH:MM-HH:MM in Los Angeles time.')
        stamp = subprocess.check_output([str(runtime), '-c', 'from app import clock; print(clock.now().isoformat())'], cwd=project, text=True).strip()
        current = datetime.fromisoformat(stamp)
        h1, m1, h2, m2 = map(int, match.groups())
        start = current.replace(hour=h1, minute=m1, second=0, microsecond=0)
        end = current.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if end <= start or (end - max(start, current)).total_seconds() < 13 * 60:
            raise ValueError('Demo window needs at least 13 minutes remaining; choose a later 15-minute window.')
        demo_window = (start.isoformat(), end.isoformat())
        print('Demo calling window: ' + args.demo_window + '; actual clock and real Google events.', flush=True)
    private = dotenv_values(ROOT / '.env', interpolate=False)
    phone = os.getenv('DEMO_PHONE') or private.get('USER_PHONE') or dotenv_values(project / '.env').get('DEMO_PHONE')
    if not phone or not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
        raise ValueError('Set your enrolled phone in calling-mvp/.env USER_PHONE or the main .env DEMO_PHONE.')
    auth = json.loads(AUTH.read_text()) if AUTH.is_file() else {}
    if args.live and (not auth.get('email') or not auth.get('apiKey')):
        raise ValueError('Complete local Dial sign-in with verify_login.py first.')
    if args.live:
        with httpx.Client(headers={'Authorization': 'Bearer ' + auth['apiKey']}, timeout=5) as client:
            response = client.get('https://api.getdial.ai/api/v1/numbers')
            response.raise_for_status()
            number = next((n for n in response.json().get('numbers', []) if n.get('id') == 'cmv44iugl00mt0ftbpptzmudz'), None)
        if (not number or number.get('callingEnabled') is False
                or number.get('setupStatus') != 'ready' or 'call' not in number.get('capabilities', [])
                or not re.fullmatch(r'\+[1-9]\d{7,14}', number.get('number', ''))):
            raise ValueError('The existing outgoing number is unavailable; no number was purchased.')
    if args.manual_call and (ROOT / '.timo-running.json').is_file():
        running = json.loads((ROOT / '.timo-running.json').read_text())
        existing = running.get('origin', '')
        if re.fullmatch(r'http://127\.0\.0\.1:\d+', existing):
            try:
                response = httpx.get(existing + '/voice/readiness', headers={'Authorization': 'Bearer ' + private.get('TRIGGER_TOKEN','')}, timeout=2)
            except httpx.HTTPError:
                response = None
            if response is not None and response.is_success:
                if not response.json()['live_calls_enabled']:
                    raise ValueError('The running service has calls disabled. Stop it and start serve_calls.py --live before manual dialing.')
                return manual_call(existing, private['TRIGGER_TOKEN'])
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', 3000))
        except OSError:
            probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = 'http://127.0.0.1:' + str(port)
    print('Calls offer eligible tasks with up to three confirmed actions. Calendar availability: ' + ('explicit mock snapshot' if args.demo_calendar else 'mock fixtures' if args.mock_calendar else 'Google Calendar') + '.', flush=True)
    server = tunnel = None
    context_registered = False
    env = {**os.environ, 'XDG_DATA_HOME': str(ROOT / '.local/share'), 'PYTHONUNBUFFERED': '1'}
    # The operator token is local; it is never printed. A calendar producer has a separate credential.
    import secrets
    operator = private.get('TRIGGER_TOKEN') or secrets.token_hex(32)
    calendar_token = os.environ.get('CALENDAR_TRIGGER_TOKEN') or dotenv_values(project / '.env').get('CALENDAR_TRIGGER_TOKEN') or ''
    server_env = {**env, 'DEMO_PHONE': phone, 'CALL_TRIGGER_TOKEN': operator, 'CALENDAR_TRIGGER_TOKEN': calendar_token,
                  'DIAL_FROM_NUMBER_ID': 'cmv44iugl00mt0ftbpptzmudz', 'DIAL_NUMBER': number['number'] if args.live else '',
                  'LIVE_CALLS_ENABLED': 'true' if args.live else 'false', 'CALL_OPT_IN': 'true' if args.live else 'false',
                  'USE_MOCKS': 'true' if args.mock_calendar else 'false', 'NOW_OVERRIDE': '',
                  'POLLER_ENABLED': 'false' if args.live or args.manual_call else 'true', 'RECEIPT_MODE': 'dashboard'}
    server_env.update(ALLOW_DEMO_CALENDAR_CALLS='true' if args.demo_calendar and args.live else 'false',
                      MOCK_CALENDAR_PATH=str(args.demo_calendar) if args.demo_calendar else '')
    if args.demo_calendar:
        server_env['GOOGLE_CALENDAR_IDS'] = '["demo-local"]'
        server_env.update(CALL_DEMO_WINDOW_START='', CALL_DEMO_WINDOW_END='')
    if demo_window:
        server_env.update(CALL_DEMO_WINDOW_START=demo_window[0], CALL_DEMO_WINDOW_END=demo_window[1])
    with (ROOT / '.timo-tunnel.log').open('w') as tunnel_log:
        try:
            public = None
            if args.live:
                tunnel = subprocess.Popen(['bash', str(ROOT / 'tools.sh'), 'cloudflared', 'tunnel', '--url', origin],
                                          cwd=ROOT, env=env, stdout=tunnel_log, stderr=subprocess.STDOUT)
                for _ in range(90):
                    match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', (ROOT / '.timo-tunnel.log').read_text())
                    if match:
                        public = match[0]
                        break
                    if tunnel.poll() is not None:
                        break
                    time.sleep(0.5)
                if not public:
                    raise ValueError('Callback tunnel did not start; inspect .timo-tunnel.log.')
                with contextlib.redirect_stdout(io.StringIO()):
                    setup(public, auth_path=AUTH)
                private = dotenv_values(ROOT / '.env', interpolate=False)
                operator = private['TRIGGER_TOKEN']
                server_env.update(DIAL_API_KEY=auth['apiKey'], DIAL_WEBHOOK_SECRET=private['DIAL_WEBHOOK_SECRET'], CALL_TRIGGER_TOKEN=operator)
            server = subprocess.Popen([str(runtime), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port), '--workers', '1'],
                                      cwd=project, env=server_env)
            for _ in range(60):
                if server.poll() is not None:
                    raise ValueError('Main app failed to start; see server output.')
                try:
                    if httpx.get(origin + '/health', timeout=1).is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise ValueError('Main app startup timed out.')
            if args.live:
                print('Waiting for the public callback tunnel before sending a signed ping...', flush=True)
                wait_for_public_health(public)
                context_response = httpx.post(origin + '/voice/runtime/context',
                    headers={'Authorization': 'Bearer ' + operator}, json={'public_origin': public}, timeout=60)
                context_registered = True  # A failed registration may still need owned-connection cleanup.
                context_response.raise_for_status()
                if context_response.json().get('connected') is not True:
                    raise ValueError('Private voice tools were not connected; calls stayed blocked.')
                print('Private voice tools connected for conversation and simulated background tasks.', flush=True)
                setup('--ping', auth_path=AUTH)
                ready = httpx.post(origin + '/voice/runtime/ready', headers={'Authorization': 'Bearer ' + operator},
                                  json={'poll_calendar': not args.manual_call}, timeout=20)
                ready.raise_for_status()
                if demo_window:
                    # Evaluate actual availability immediately after callback readiness.
                    tick = httpx.post(origin + '/demo/tick', headers={'Authorization': 'Bearer ' + operator}, json={}, timeout=20)
                    tick.raise_for_status()
            print('\nMain app: ' + origin, flush=True)
            print('Calendar source: ' + ('mock' if args.mock_calendar else 'Google Calendar; missing credentials suppress calls.'), flush=True)
            print('Calls: ' + ('LIVE, subject to the persistent session budget.' if args.live else 'DISABLED; eligible gaps reserve dry-run sessions.'), flush=True)
            print('Calendar writes and SMS disabled; receipts are dashboard-only.', flush=True)
            if public:
                print('Optional external producer: ' + public + '/voice/calendar-gap (CALENDAR_TRIGGER_TOKEN required).', flush=True)
            from app.agent.engine import atomic_json
            running = ROOT / '.timo-running.json'
            atomic_json(running, {'origin': origin, 'pid': server.pid})
            if args.manual_call:
                manual_call(origin, operator)
                return
            print('Keep Terminal open. Ctrl-C stops polling and callbacks.', flush=True)
            while server.poll() is None and (tunnel is None or tunnel.poll() is None):
                time.sleep(1)
            if server.poll() is not None:
                raise ValueError('Main app exited with code ' + str(server.returncode) + '; inspect server output.')
            raise ValueError('Callback tunnel exited with code ' + str(tunnel.returncode) + '; inspect .timo-tunnel.log.')
        finally:
            if context_registered and server and server.poll() is None:
                try:
                    cleanup = httpx.delete(origin + '/voice/runtime/context',
                        headers={'Authorization': 'Bearer ' + operator}, timeout=20)
                    if not cleanup.is_success:
                        print('Owned voice connection cleanup will be reconciled on the next startup.', flush=True)
                except httpx.HTTPError:
                    print('Owned voice connection cleanup will be reconciled on the next startup.', flush=True)
            running = ROOT / '.timo-running.json'
            if server and running.is_file():
                try:
                    if json.loads(running.read_text()).get('pid') == server.pid:
                        running.unlink()
                except (OSError, ValueError):
                    pass
            stop(server)
            stop(tunnel)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nService stopped.')
    except Exception as error:
        print(str(error) if isinstance(error, ValueError) else 'Service setup failed; no automatic call retry.', file=sys.stderr)
        sys.exit(1)
