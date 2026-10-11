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

import httpx
from dotenv import dotenv_values
from setup import connect_mcp, setup

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


def manual_call(origin, operator):
    response = httpx.post(origin + '/voice/trigger', headers={'Authorization': 'Bearer ' + operator},
                          json={'event_id': 'manual:' + str(uuid.uuid4()), 'available_minutes': 20}, timeout=20)
    response.raise_for_status()
    result = response.json()
    if result.get('status') != 'queued':
        raise ValueError('Manual call not queued: ' + str(result.get('reason') or result.get('status')))
    print('One call queued. Answer your enrolled phone; choose go, next or hold, then confirm yes.', flush=True)
    for _ in range(150):
        response = httpx.get(origin + '/voice/sessions', headers={'Authorization': 'Bearer ' + operator}, timeout=5)
        response.raise_for_status()
        session = next((s for s in response.json() if s['sessionId'] == result['sessionId']), None)
        if session and session['status'] in ('resolved', 'needs_review', 'failed'):
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
    parser.add_argument('--python', type=Path)
    args = parser.parse_args(argv)
    if args.manual_call and not args.live:
        raise ValueError('Manual dialing requires --live.')
    if args.live and args.mock_calendar:
        raise ValueError('Mock calendar ticks cannot enable live calls.')
    project = ROOT.parent
    runtimes = [args.python] if args.python else [project / '.venv/bin/python', project.parent / 'timo/.scaffold-venv/bin/python']
    runtime = next((p for p in runtimes if p and p.is_file()), None)
    if not runtime:
        raise ValueError('Run uv sync in the repository or pass --python PATH.')
    private = dotenv_values(ROOT / '.env', interpolate=False)
    phone = os.getenv('DEMO_PHONE') or private.get('USER_PHONE') or dotenv_values(project / '.env').get('DEMO_PHONE')
    if not phone or not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
        raise ValueError('Set your enrolled phone in calling-mvp/.env USER_PHONE or the main .env DEMO_PHONE.')
    auth = json.loads(AUTH.read_text()) if AUTH.is_file() else {}
    if args.live and (not auth.get('email') or not auth.get('apiKey')):
        raise ValueError('Complete local Dial sign-in with verify_login.py first.')
    timo_number = ''
    if args.live:
        with httpx.Client(headers={'Authorization': 'Bearer ' + auth['apiKey']}, timeout=5) as client:
            response = client.get('https://api.getdial.ai/api/v1/numbers')
            response.raise_for_status()
            numbers = response.json().get('numbers', [])
        number = next((n for n in numbers if n.get('id') == 'cmv44iugl00mt0ftbpptzmudz'), None)
        if not number or number.get('callingEnabled') is False:
            raise ValueError('The existing outgoing number is unavailable; no number was purchased.')
        timo_number = number.get('number') or ''
        # Two-agent demo: the second Dial number plays the user, so Timo must call that number.
        user_id = os.getenv('DIAL_USER_NUMBER_ID') or dotenv_values(project / '.env').get('DIAL_USER_NUMBER_ID')
        if user_id and next((n.get('number') for n in numbers if n.get('id') == user_id), None) != phone:
            raise ValueError('DEMO_PHONE must be the E.164 of the test user number in DIAL_USER_NUMBER_ID.')
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
    server = tunnel = None
    env = {**os.environ, 'XDG_DATA_HOME': str(ROOT / '.local/share'), 'PYTHONUNBUFFERED': '1'}
    # The operator token is local; it is never printed. A calendar producer has a separate credential.
    import secrets
    operator = private.get('TRIGGER_TOKEN') or secrets.token_hex(32)
    calendar_token = os.environ.get('CALENDAR_TRIGGER_TOKEN') or dotenv_values(project / '.env').get('CALENDAR_TRIGGER_TOKEN') or ''
    server_env = {**env, 'DEMO_PHONE': phone, 'CALL_TRIGGER_TOKEN': operator, 'CALENDAR_TRIGGER_TOKEN': calendar_token,
                  'DIAL_FROM_NUMBER_ID': 'cmv44iugl00mt0ftbpptzmudz', 'DIAL_NUMBER': timo_number,
                  'LIVE_CALLS_ENABLED': 'true' if args.live else 'false', 'CALL_OPT_IN': 'true' if args.live else 'false',
                  'USE_MOCKS': 'true' if args.mock_calendar else 'false', 'NOW_OVERRIDE': '',
                  'POLLER_ENABLED': 'false' if args.manual_call else 'true', 'RECEIPT_MODE': 'dashboard'}
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
                server_env.update(DIAL_API_KEY=auth['apiKey'], DIAL_WEBHOOK_SECRET=private['DIAL_WEBHOOK_SECRET'], CALL_TRIGGER_TOKEN=operator,
                                  MCP_TOKEN=private['MCP_TOKEN'])
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
                setup('--ping', auth_path=AUTH)
                connect_mcp(public, auth_path=AUTH)
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
        finally:
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
