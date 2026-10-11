"""Laptop-only setup. Never prints secrets or places calls."""
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, quote
import httpx
from dotenv import dotenv_values
from server import atomic_json

ROOT = Path(__file__).resolve().parent
EMAIL = os.getenv('DIAL_LOGIN_EMAIL', '')


def setup(argument, root=ROOT, auth_path=None, transport=None):
    root = Path(root)
    auth_path = auth_path or Path(os.getenv('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'dial/auth.v1.json'
    try:
        auth = json.loads(Path(auth_path).read_text())
    except (OSError, ValueError):
        raise ValueError('Sign in locally with dial auth login YOUR_EMAIL and verify_login.py first.')
    if not auth.get('apiKey') or not auth.get('email') or (EMAIL and auth.get('email') != EMAIL):
        raise ValueError('Wrong or incomplete local Dial login.')
    env_path, meta_path = root / '.env', root / '.timo-setup.json'
    previous = dotenv_values(env_path, interpolate=False) if env_path.exists() else {}
    metadata = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    with httpx.Client(transport=transport, timeout=15) as client:
        def api(method, endpoint, body=None):
            opts = {'headers': {'Authorization': 'Bearer ' + auth['apiKey']}}
            if body is not None:
                opts['json'] = body
            response = client.request(method, 'https://api.getdial.ai/api/v1' + endpoint, **opts)
            if not response.is_success:
                raise ValueError(f'Dial {method} {endpoint}: HTTP {response.status_code}. No automatic write retry.')
            return response.json()
        if argument == '--ping':
            if not metadata.get('webhookId'):
                raise ValueError('Run tunnel URL setup first.')
            endpoint = '/webhooks/' + quote(metadata['webhookId'], safe='')
            before = api('GET', endpoint).get('webhook', {}).get('lastPingSucceededAt')
            api('POST', endpoint + '/ping', {})
            for attempt in range(30):
                time.sleep(2)
                stamp = api('GET', endpoint).get('webhook', {}).get('lastPingSucceededAt')
                if stamp and stamp != before:
                    print('Signed webhook ping VERIFIED at ' + stamp, flush=True)
                    return
                if attempt % 5 == 4:
                    print('Waiting for signed webhook ping (' + str((attempt + 1) * 2) + '/60 seconds)...', flush=True)
            raise ValueError('Signed callback not verified after 60 seconds; calls stayed blocked. Check receiver HTTP status and .timo-tunnel.log.')
        url = urlsplit(argument)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/', '/webhooks/dial'):
            raise ValueError('Pass a plain public HTTPS tunnel origin.')
        target = 'https://' + url.netloc + '/webhooks/dial'
        event_types = ['call.ended', 'call.transcribed']
        matches = [h for h in api('GET', '/webhooks').get('webhooks', []) if h.get('targetUrl') == target and sorted(h.get('eventTypes', [])) == sorted(event_types)]
        if len(matches) > 1:
            raise ValueError('Duplicate subscriptions exist. Remove duplicates in dashboard before proceeding.')
        created = {}
        if matches:
            hook = matches[0]
        else:
            created = api('POST', '/webhooks', {'targetUrl': target, 'eventTypes': event_types})
            hook = created.get('webhook', {})
        if not hook.get('id'):
            raise ValueError('Missing webhook.id; inspect dashboard before retrying.')
        atomic_json(meta_path, {'webhookId': hook['id'], 'targetUrl': target})
        secret = hook.get('secret') or hook.get('signingSecret') or created.get('secret')
        def full(value):
            return isinstance(value, str) and value.startswith('whsec_') and '•' not in value and '*' not in value
        if not full(secret):
            secret = api('GET', '/webhooks/' + quote(hook['id'], safe='') + '/secret').get('secret')
        if not full(secret):
            raise ValueError('Full signing secret unavailable. No .env written.')
        values = {'DIAL_API_KEY': auth['apiKey'], 'DIAL_FROM_NUMBER_ID': 'cmv44iugl00mt0ftbpptzmudz',
                  'USER_PHONE': previous.get('USER_PHONE') or os.getenv('DEMO_PHONE', '+15555550100'), 'DIAL_WEBHOOK_SECRET': secret, 'PORT': '3000',
                  'RECEIPT_MODE': 'dashboard', 'TRIGGER_TOKEN': previous.get('TRIGGER_TOKEN') or secrets.token_hex(32),
                  'LIVE_CALLS_ENABLED': previous.get('LIVE_CALLS_ENABLED', 'false')}
        # Single-quoted python-dotenv values: escape slash/quote, reject newline.
        lines = []
        for key, value in values.items():
            if not isinstance(value, str) or '\n' in value or '\r' in value:
                raise ValueError('Unexpected multiline credential')
            lines.append(key + "='" + value.replace('\\', '\\\\').replace("'", "\\'") + "'")
        temp = root / '.env.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write('\n'.join(lines) + '\n')
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, env_path)
        os.chmod(env_path, 0o600)
        ignore = root / '.gitignore'
        existing = ignore.read_text() if ignore.exists() else ''
        ignores = ['.env', '.env.tmp', '.timo-state.json', '.timo-state.json.tmp', '.timo-setup.json', '.venv/', '__pycache__/', '.pytest_cache/']
        ignore.write_text(existing + '\n' + '\n'.join(i for i in ignores if i not in existing.splitlines()) + '\n')
        print('Private .env written. No calls, texts, purchases, or credit spend.')
        print('Webhook: ' + target)
        print('Next: python server.py in another terminal; then python setup.py --ping.')
        print('Live calling defaults OFF. Approve spend before setting LIVE_CALLS_ENABLED=true and restarting.')
        if metadata.get('webhookId') and metadata['webhookId'] != hook['id']:
            print('After new ping is verified, remove OLD webhook ' + metadata['webhookId'] + ' in dashboard.')
        return {'webhookId': hook['id'], 'targetUrl': target}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Usage: python setup.py https://YOUR_TUNNEL_HOST OR python setup.py --ping')
        setup(sys.argv[1])
    except Exception as error:
        # Never dump raw request/response or secrets in tracebacks.
        print(str(error) if isinstance(error, ValueError) else 'Setup failed; check network/configuration. No automatic write retry.', file=sys.stderr)
        sys.exit(1)
