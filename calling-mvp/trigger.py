"""Manual trigger for a running main app; the same Engine handles calendar triggers."""
import json
import re
import uuid
from pathlib import Path
import httpx
from dotenv import dotenv_values

root = Path(__file__).resolve().parent
private = dotenv_values(root / '.env')
running = json.loads((root / '.timo-running.json').read_text())
origin = running['origin']
if not re.fullmatch(r'http://127\.0\.0\.1:\d+', origin):
    raise SystemExit('Invalid local service origin')
response = httpx.post(origin + '/voice/trigger', headers={'Authorization': 'Bearer ' + private['TRIGGER_TOKEN']},
                      json={'event_id': 'manual:' + str(uuid.uuid4()), 'available_minutes': 20}, timeout=20)
print(response.status_code, response.json())
