"""Verify the existing Dial login with a code entered privately in a terminal."""
import getpass
import json
import os
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
code = getpass.getpass('Dial email code (hidden): ').strip()
if not re.fullmatch(r'\d{6}', code):
    raise SystemExit('Enter the six-digit code from the Dial email.')
env = {**os.environ, 'XDG_DATA_HOME': str(root / '.local/share')}
result = subprocess.run(['bash', str(root / 'tools.sh'), 'dial', 'auth', 'verify-otp', '--code', code, '--json'],
                        env=env, cwd=root, capture_output=True, text=True)
try:
    data = json.loads(result.stdout)
except ValueError:
    raise SystemExit('Verification failed. Request a fresh code and try again; no credentials were displayed.')
if result.returncode or not data.get('ok'):
    raise SystemExit('Code verification failed or expired. Request a fresh code and try again.')
if data.get('pendingPhone'):
    raise SystemExit('This account still needs phone enrollment. Stop here and use the intended existing account.')
print('Dial sign-in saved locally. No call was placed. Return to the chat and say signed in.')
