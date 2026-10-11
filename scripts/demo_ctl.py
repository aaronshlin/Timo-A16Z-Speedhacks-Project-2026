#!/usr/bin/env python3
# new file, no collision with in-flight app/* work
"""Packaging control only. No auto-dial. Call placement needs `call --once`."""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from demo_qa import ROOT, WORK, read_env, api, qa

EVENTS = ['call.ended', 'call.transcribed', 'message.received']
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'compose.demo.yaml')]
META = WORK / 'metadata.json'
CONFIG = WORK / 'runtime.env'


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, indent=2)
    os.replace(temp, path)


def env_file(values):
    # Single-quoted Compose values are literal: no interpolation of credentials.
    lines = []
    for key, value in values.items():
        value = str(value)
        if '\n' in value or '\r' in value or "'" in value:
            raise ValueError('Use single-line env values without apostrophes; rejected field ' + key)
        lines.append(key + "='" + value + "'")
    temp = CONFIG.with_suffix('.tmp')
    temp.write_text('\n'.join(lines) + '\n')
    os.chmod(temp, 0o600)
    os.replace(temp, CONFIG)


def config():
    result = read_env(CONFIG)
    return result


def metadata():
    return json.loads(META.read_text()) if META.exists() else {}


def save(meta):
    private_json(META, meta)


def compose_env():
    meta = metadata()
    return {**os.environ, 'DEMO_PORT': str(meta.get('port', 3000)),
            'DEMO_GOOGLE_TOKEN': meta.get('google_token', '')}


def compose(*args, capture=False):
    result = subprocess.run(COMPOSE + list(args), cwd=ROOT, env=compose_env(),
                            text=True, capture_output=True)
    if result.returncode:
        # Don't dump compose interpolation/config output, which may contain credentials.
        raise ValueError('Docker Compose failed at ' + ' '.join(args[:2]) + '; inspect local Docker state.')
    return result.stdout if capture else None


def wait_health(origin, token='', timeout=75):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if api(origin, '/health', token).get('ok') is True:
                return
        except Exception:
            pass
        time.sleep(1)
    raise ValueError('Health did not become ready; make logs for app diagnostics')


def dial(path, cfg, method='GET', body=None):
    return api('https://api.getdial.ai', '/api/v1' + path, cfg['DIAL_API_KEY'], method, body)


def preflight(mode):
    if not shutil.which('docker'):
        raise ValueError('Install/start Docker Desktop first')
    subprocess.run(['docker', 'compose', 'version'], check=True, capture_output=True)
    source = Path(os.getenv('DEMO_ENV_FILE', str(ROOT / '.demo.env')))
    if not source.is_file():
        raise ValueError('Create private .demo.env locally; see VERIFY-AFTER-CODEX.md')
    if source.stat().st_mode & 0o077:
        raise ValueError('chmod 600 your private demo env file before continuing')
    cfg = read_env(source)
    mock_demo = str(os.getenv('MOCK_DEMO', cfg.get('MOCK_DEMO', '0'))).lower() in ('1', 'true', 'yes')
    if mock_demo and mode == 'live':
        raise ValueError('MOCK_DEMO supports dry startup only; no live calls or provider setup')
    phone = cfg.get('DEMO_PHONE') or cfg.get('USER_PHONE', '')
    operator = cfg.get('CALL_TRIGGER_TOKEN') or cfg.get('TRIGGER_TOKEN') or secrets.token_hex(32)
    token = Path(cfg.get('GOOGLE_TOKEN_PATH') or ROOT / 'token.json').expanduser().resolve()
    if not mock_demo and not token.is_file():
        raise ValueError('Complete Google consent on laptop first; token.json must exist, or use MOCK_DEMO=1')
    if not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
        raise ValueError('Set DEMO_PHONE to the consenting human phone')
    if not (ROOT / 'calling-mvp/demo_sessions.json').is_file():
        raise ValueError('Missing demo_sessions.json')
    port = int(cfg.get('PORT', '3000'))
    if not 1024 <= port <= 65535:
        raise ValueError('Use a PORT between 1024 and 65535')
    WORK.mkdir(mode=0o700, exist_ok=True)
    os.chmod(WORK, 0o700)
    if mock_demo:
        # Compose still needs a bind source, but no Google credential is read.
        token = WORK / 'mock-google-placeholder.json'
        private_json(token, {})
    # Avoid silently resetting the existing demo cap or losing prior calls.
    previous = metadata()
    if not previous and any((ROOT / p).exists() for p in
                            ['data/calls.db', 'calling-mvp/.timo-state.json']):
        raise ValueError('Existing laptop call state found. Stop its server, then run python3 scripts/demo_ctl.py import-state. Do not wipe state or bypass this check.')
    (WORK / 'state').mkdir(mode=0o700, exist_ok=True)
    os.chmod(WORK / 'state', 0o700)
    meta = {**previous, 'port': port, 'google_token': str(token), 'local_origin': f'http://127.0.0.1:{port}',
            'mode': 'dry', 'mock_demo': mock_demo, 'qa_event_id': previous.get('qa_event_id') or 'packaging-qa:' + secrets.token_hex(8)}
    cfg.update(DEMO_PHONE=phone, USER_NUMBER=phone, CALL_TRIGGER_TOKEN=operator,
               USE_MOCKS='true' if mock_demo else 'false', NOW_OVERRIDE=cfg.get('NOW_OVERRIDE', '') if mock_demo else '', POLLER_ENABLED='false',
               LIVE_CALLS_ENABLED='false', CALL_OPT_IN='false', RECEIPT_MODE='dashboard',
               MOCK_DIAL='true', SMS_ENABLED='false', DIAL_USER_NUMBER_ID='', USER_NUMBER_ID='',
               GOOGLE_TOKEN_PATH='' if mock_demo else '/secrets/google-token.json', CALL_STATE_PATH='/state/calls.db',
               CALL_ENGINE_STATE_PATH='/state/engine.json', CALL_SESSION_LIMIT='1')
    for key in ['CALL_DEMO_WINDOW_START', 'CALL_DEMO_WINDOW_END', 'MOCK_CALENDAR_PATH']:
        cfg[key] = ''
    cfg['ALLOW_DEMO_CALENDAR_CALLS'] = 'false'
    if mock_demo:
        # USE_MOCKS alone does not disable every optional client (notably LLM).
        for key in ['ANTHROPIC_API_KEY', 'DIAL_API_KEY', 'DARWIN_API_KEY', 'TODOIST_TOKEN']:
            cfg[key] = ''
    # These launcher-only aliases should not enter the app config or logs.
    for key in ['USER_PHONE', 'TRIGGER_TOKEN', 'PORT', 'MOCK_DEMO']:
        cfg.pop(key, None)
    if mode == 'live':
        if not cfg.get('DIAL_API_KEY') or not cfg.get('DIAL_FROM_NUMBER_ID'):
            raise ValueError('Live setup requires DIAL_API_KEY and DIAL_FROM_NUMBER_ID in private env')
        number = next((n for n in dial('/numbers', cfg).get('numbers', [])
                       if n.get('id') == cfg['DIAL_FROM_NUMBER_ID']), None)
        if not number or not (number.get('callingEnabled') and number.get('setupStatus') == 'ready'
                              and 'call' in number.get('capabilities', [])):
            raise ValueError('Selected Dial line is not ready for calls')
        cfg['DIAL_NUMBER'] = cfg['TIMO_NUMBER'] = number['number']
    # Keep a local-only ignore file so credentials/state cannot enter a normal git add.
    (WORK / '.gitignore').write_text('*\n')
    save(meta)
    env_file(cfg)
    return cfg, meta


def ensure_webhook(public, cfg, meta):
    target = public + '/webhooks/dial'
    hooks = dial('/webhooks', cfg).get('webhooks', [])
    exact = [h for h in hooks if h.get('targetUrl') == target and sorted(h.get('eventTypes', [])) == sorted(EVENTS)]
    if len(exact) > 1:
        raise ValueError('Duplicate callback subscriptions; reconcile before startup')
    pending = meta.get('pending_webhook')
    if exact:
        if not pending or pending.get('targetUrl') != target or exact[0]['id'] != pending.get('id'):
            raise ValueError('Matching callback not owned by this installation; review ownership first')
        hook = exact[0]
    else:
        # No documented PATCH action. Replace, verify, THEN remove only our old ID.
        # Persist the intended unique tunnel URL before POST for uncertain-response recovery.
        if pending and pending.get('targetUrl') == target and pending.get('uncertain'):
            raise ValueError('Prior webhook create response uncertain; list/reconcile the exact target before retry')
        meta['pending_webhook'] = {'targetUrl': target, 'uncertain': True}
        save(meta)
        created = dial('/webhooks', cfg, 'POST', {'targetUrl': target, 'eventTypes': EVENTS})
        hook = created.get('webhook', {})
        if not hook.get('id'):
            raise ValueError('Webhook creation uncertain; reconcile target in dashboard, no automatic retry')
        meta['pending_webhook'] = {'targetUrl': target, 'id': hook['id'], 'uncertain': False}
        save(meta)
    secret = hook.get('secret') or hook.get('signingSecret')
    if not secret or '*' in secret or '•' in secret:
        secret = dial('/webhooks/' + quote(hook['id'], safe='') + '/secret', cfg).get('secret')
    if not isinstance(secret, str) or not secret.startswith('whsec_') or '*' in secret or '•' in secret:
        raise ValueError('Full webhook secret unavailable')
    cfg['DIAL_WEBHOOK_SECRET'] = secret
    env_file(cfg)
    return hook


def start(mode):
    if CONFIG.exists() and META.exists():
        stop()
    cfg, meta = preflight(mode)
    compose('build', 'app')
    compose('up', '-d', 'app')
    wait_health(meta['local_origin'])
    qa()  # read-only; no fake timestamps or queue reset
    if mode == 'dry':
        result = api(meta['local_origin'], '/voice/trigger', cfg['CALL_TRIGGER_TOKEN'], 'POST',
                     {'event_id': meta['qa_event_id'], 'available_minutes': 20})
        if result.get('mode') != 'dry_run' or result.get('status') != 'calls_disabled':
            raise ValueError('Dry reservation expectation changed; inspect without retrying')
        print('Dry reservation verified. No call placed. Mock calendar and local fixtures only.' if meta.get('mock_demo')
              else 'Dry reservation verified. No call placed. Calendar token presence only, live-read QA still needed.')
        print('Local inbox: ' + meta['local_origin'] + '/inbox')
        return
    compose('--profile', 'live', 'up', '-d', 'ingress', 'tunnel')
    deadline = time.monotonic() + 90
    public = None
    while time.monotonic() < deadline:
        logs = compose('logs', '--no-color', 'tunnel', capture=True)
        matches = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', logs)
        if matches:
            public = matches[-1]
            break
        time.sleep(1)
    if not public:
        raise ValueError('Tunnel URL unavailable')
    wait_health(public)
    meta['public_origin'] = public
    save(meta)
    hook = ensure_webhook(public, cfg, meta)
    cfg.update(LIVE_CALLS_ENABLED='true', CALL_OPT_IN='true', POLLER_ENABLED='false')
    env_file(cfg)
    compose('up', '-d', '--force-recreate', 'app')
    wait_health(meta['local_origin'])
    compose('--profile', 'live', 'restart', 'ingress')
    wait_health(public)
    api(meta['local_origin'], '/voice/runtime/context', cfg['CALL_TRIGGER_TOKEN'], 'POST', {'public_origin': public})
    before = dial('/webhooks/' + quote(hook['id'], safe=''), cfg).get('webhook', {}).get('lastPingSucceededAt')
    dial('/webhooks/' + quote(hook['id'], safe='') + '/ping', cfg, 'POST', {})
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        stamp = dial('/webhooks/' + quote(hook['id'], safe=''), cfg).get('webhook', {}).get('lastPingSucceededAt')
        if stamp and stamp != before:
            break
        time.sleep(2)
    else:
        raise ValueError('Signed callback ping not verified; calling remains gated')
    ready = api(meta['local_origin'], '/voice/runtime/ready', cfg['CALL_TRIGGER_TOKEN'], 'POST', {'poll_calendar': False})
    if ready.get('ready') is not True or ready.get('poller_enabled'):
        raise ValueError('Runtime not ready or automatic polling unexpectedly enabled')
    meta['mode'] = 'live'
    old_id = meta.get('webhook_id')
    meta['webhook_id'] = hook['id']
    save(meta)
    if old_id and old_id != hook['id']:
        dial('/webhooks/' + quote(old_id, safe=''), cfg, 'DELETE')
    qa()
    print('LIVE READY, no call placed. Use make call only after confirming the destination and credit spend.')
    print('Legacy /mcp unregistered and blocked at public ingress. SMS remains mocked.')


def call_once():
    cfg, meta = config(), metadata()
    if meta.get('mode') != 'live':
        raise ValueError('Run make demo-live first; make demo never permits dialing')
    qa()
    if meta.get('call_intent'):
        raise ValueError('A call intent already exists. Reconcile its recorded session; no automatic second call')
    answer = input('Place ONE real demo call to ' + cfg['DEMO_PHONE'] + ' using existing Dial credit? Type CALL: ')
    if answer != 'CALL':
        raise ValueError('Call canceled; nothing dialed')
    event_id = 'packaging-call:' + secrets.token_hex(12)
    meta['call_intent'] = {'event_id': event_id, 'status': 'unknown'}
    save(meta)  # before placement
    result = api(meta['local_origin'], '/voice/trigger', cfg['CALL_TRIGGER_TOKEN'], 'POST',
                 {'event_id': event_id, 'available_minutes': 20})
    meta['call_intent'].update(status=result.get('status', 'unknown'), session_id=result.get('sessionId'), call_id=result.get('callId'))
    save(meta)
    if result.get('status') != 'queued':
        raise ValueError('Call was not queued; inspect session/budget without retrying')
    print('One call queued. Answer naturally; all actions are simulated.')
    deadline = time.monotonic() + 330
    while time.monotonic() < deadline:
        sessions = api(meta['local_origin'], '/voice/sessions', cfg['CALL_TRIGGER_TOKEN'])
        session = next((s for s in sessions if s.get('sessionId') == result.get('sessionId')), None)
        if session and session.get('status') in ('resolved', 'finished', 'needs_review', 'failed', 'skipped', 'deferred'):
            print('Call status: ' + session['status'])
            print('Inspect receipt/transcript in authenticated /voice/sessions; no real external task ran.')
            return
        time.sleep(2)
    raise ValueError('Call outcome not yet verified; preserve state and reconcile Dial before any retry')


def stop():
    if CONFIG.exists() and META.exists():
        cfg, meta = config(), metadata()
        if meta.get('mode') == 'live':
            try:
                api(meta['local_origin'], '/voice/runtime/context', cfg['CALL_TRIGGER_TOKEN'], 'DELETE')
            except Exception:
                print('Owned voice cleanup unverified; next startup must reconcile it.', file=sys.stderr)
    compose('--profile', 'live', 'down')  # never -v
    print('Stopped. State and one-call intent preserved.')


def import_state():
    if META.exists() or (WORK / 'state').exists():
        raise ValueError('Packaging state already exists; no overwrite permitted')
    answer = input('Has the laptop Timo server been stopped? Type STOPPED to copy its state: ')
    if answer != 'STOPPED':
        raise ValueError('State import canceled')
    source_db = ROOT / 'data/calls.db'
    source_engine = ROOT / 'calling-mvp/.timo-state.json'
    if not source_db.is_file() or not source_engine.is_file():
        raise ValueError('Both existing call-state files are required; ask Codex to inspect partial state')
    import fcntl, sqlite3
    lock_path = source_engine.with_suffix('.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Laptop app still owns the call-state lock') from None
        payload = json.loads(source_engine.read_text())
        if any(i.get('status') in ('placing', 'queued', 'awaiting_transcript', 'call_unknown') for i in payload.get('items', [])):
            raise ValueError('Active/unknown laptop call must be reconciled before migration')
        WORK.mkdir(mode=0o700, exist_ok=True)
        (WORK / '.gitignore').write_text('*\n')
        state_dir = WORK / 'state'
        state_dir.mkdir(mode=0o700)
        # Backup includes WAL safely instead of copying only the SQLite main file.
        with sqlite3.connect('file:' + str(source_db) + '?mode=ro', uri=True) as source:
            with sqlite3.connect(state_dir / 'calls.db') as dest:
                source.backup(dest)
        shutil.copy2(source_engine, state_dir / 'engine.json')
        for name in ['.timo-context-mcp.json']:
            source = source_engine.parent / name
            if source.exists():
                shutil.copy2(source, state_dir / name)
        for file in state_dir.iterdir():
            os.chmod(file, 0o600)
        save({'imported_from_laptop': True})
    print('Copied existing state safely. Original files unchanged; keep the old laptop app stopped.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'call', 'stop', 'status', 'logs', 'import-state'])
    parser.add_argument('--mode', choices=['dry', 'live'], default='dry')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.action == 'import-state':
        import_state()
    elif args.action == 'start':
        start(args.mode)
    elif args.action == 'call':
        if not args.once:
            raise ValueError('Only call --once is supported')
        call_once()
    elif args.action == 'stop':
        stop()
    elif args.action == 'status':
        print(compose('ps', capture=True))
    else:
        print('Local logs can contain private app context; do not paste them publicly.')
        print(compose('logs', '--tail', '80', 'app', capture=True))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted. Use make stop; preserve state and reconcile any call intent.', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Exception strings from network/subprocess must never expose credential-bearing output.
        print(str(exc) if isinstance(exc, ValueError) else 'Packaging failed; inspect local Docker/configuration.', file=sys.stderr)
        if CONFIG.exists() and META.exists():
            try:
                cfg, meta = config(), metadata()
                if cfg.get('LIVE_CALLS_ENABLED') == 'true':
                    api(meta['local_origin'], '/voice/runtime/context', cfg['CALL_TRIGGER_TOKEN'], 'DELETE')
            except Exception:
                pass
        sys.exit(1)
