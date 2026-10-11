"""Manual trigger for a running main app; the same Engine handles calendar triggers.

    python trigger.py          # one go/next/hold recommendation call
    python trigger.py brief    # one briefing call (plan, what's done, offers to act on mid-call)
"""
import json
import re
import sys
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import httpx
from dotenv import dotenv_values

root = Path(__file__).resolve().parent
private = dotenv_values(root / '.env')
running = json.loads((root / '.timo-running.json').read_text())
origin = running['origin']
if not re.fullmatch(r'http://127\.0\.0\.1:\d+', origin):
    raise SystemExit('Invalid local service origin')
headers = {'Authorization': 'Bearer ' + private['TRIGGER_TOKEN']}
if sys.argv[1:] == ['brief']:
    # A briefing call through the router, as the scheduler would send before a commute.
    leave = (datetime.now(ZoneInfo('America/Los_Angeles')) + timedelta(minutes=15)).isoformat()
    response = httpx.post(origin + '/demo/trigger', headers=headers, timeout=30, json={
        'type': 'pre_commute', 'payload': {'depart_at': leave, 'destination': 'the Berkeley campus',
                                           'done': ['Drafted your reply to Sarah']}})
else:
    response = httpx.post(origin + '/voice/trigger', headers=headers,
                          json={'event_id': 'manual:' + str(uuid.uuid4()), 'available_minutes': 20}, timeout=20)
print(response.status_code, response.json())
