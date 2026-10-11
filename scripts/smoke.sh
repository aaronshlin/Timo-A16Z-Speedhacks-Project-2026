#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE:-http://127.0.0.1:8000}"

if ! curl -sf "$BASE/health" >/dev/null; then
  echo "Start the server first: uv run uvicorn app.main:app --reload"
  exit 1
fi

rm -f data/timo.db
curl -sf -X POST "$BASE/inbox/reset" >/dev/null

NOW="$(uv run python -c 'from datetime import datetime; from zoneinfo import ZoneInfo; z=ZoneInfo("America/Los_Angeles"); n=datetime.now(z).replace(hour=10, minute=15, second=0, microsecond=0); print(n.isoformat())')"
PHONE="$(uv run python -c 'from app.config import settings; print(settings.DEMO_PHONE)')"
TIMO="$(uv run python -c 'from app.config import settings; print(settings.TIMO_NUMBER)')"
USER="$(uv run python -c 'from app.phone.dial import user_e164; print(user_e164())')"

echo "=== POST /demo/tick now=$NOW ==="
TICK="$(curl -sS -X POST "$BASE/demo/tick" -H 'content-type: application/json' -d "{\"now\":\"$NOW\"}")"
echo "$TICK"
echo

echo "=== webhook next from USER_NUMBER ==="
uv run python scripts/fake_inbound.py "$TIMO" "$USER" "next"
echo
sleep 0.6
WEBHOOK_NEXT="$(curl -sS "$BASE/inbox/thread")"

echo "=== POST /sms/inbound bus ==="
BUS="$(curl -sS -X POST "$BASE/sms/inbound" -H 'content-type: application/json' -d "{\"from\":\"$PHONE\",\"body\":\"bus\"}")"
echo "$BUS"
echo

echo "=== POST /sms/inbound next ==="
NEXT="$(curl -sS -X POST "$BASE/sms/inbound" -H 'content-type: application/json' -d "{\"from\":\"$PHONE\",\"body\":\"next\"}")"
echo "$NEXT"
echo

echo "=== POST /sms/inbound done ==="
DONE="$(curl -sS -X POST "$BASE/sms/inbound" -H 'content-type: application/json' -d "{\"from\":\"$PHONE\",\"body\":\"done\"}")"
echo "$DONE"
echo

echo "=== webhook next after done ==="
uv run python scripts/fake_inbound.py "$TIMO" "$USER" "next"
echo
sleep 0.6
AFTER_DONE="$(curl -sS "$BASE/inbox/thread")"

echo "=== GET /outbox ==="
curl -sS "$BASE/outbox"
echo

FAIL=0
python_check() {
  if uv run python -c "$1"; then
    echo "PASS $2"
  else
    echo "FAIL $2"
    FAIL=1
  fi
}

export TICK WEBHOOK_NEXT BUS NEXT DONE AFTER_DONE
python_check '
import json, os
tick = json.loads(os.environ["TICK"])
title = tick["pick"]["title"]
entries = json.loads(os.environ["WEBHOOK_NEXT"])["entries"]
replies = [entry["body"] for entry in entries if entry["direction"] == "timo" and title not in entry["body"]]
raise SystemExit(0 if replies else 1)
' "webhook next from USER_NUMBER is not the item just picked"

python_check '
import json, os
from app.phone.messages import FOOTER
bus = json.loads(os.environ["BUS"])["reply"]
done = json.loads(os.environ["DONE"])["reply"]
nxt = json.loads(os.environ["NEXT"])["reply"]
raise SystemExit(0 if bus.startswith("Phone-friendly for the bus:") else 1)
' "bus reply starts with Phone-friendly for the bus:"

python_check '
import json, os
done = json.loads(os.environ["DONE"])["reply"]
raise SystemExit(0 if "http" not in done.lower() else 1)
' "done reply contains no http"

python_check '
import json, os, re
done = json.loads(os.environ["DONE"])["reply"]
title = re.search(r"Done\. Marked '"'"'(.+)'"'"' complete\.", done).group(1)
entries = json.loads(os.environ["AFTER_DONE"])["entries"]
seen = False
later = []
for entry in entries:
    if entry["direction"] == "timo" and entry["body"] == done:
        seen = True
        continue
    if seen and entry["direction"] == "timo":
        later.append(entry["body"])
raise SystemExit(0 if later and all(title not in body for body in later) else 1)
' "webhook next after done is not the completed item"

if grep -R -i -q todoist app/phone; then
  echo "FAIL app/phone contains no todoist"
  FAIL=1
else
  echo "PASS app/phone contains no todoist"
fi

python_check '
import json, os
from app.phone.messages import FOOTER
texts = [
    json.loads(os.environ["BUS"])["reply"],
    json.loads(os.environ["NEXT"])["reply"],
    json.loads(os.environ["DONE"])["reply"],
]
raise SystemExit(0 if all(text.count(FOOTER) <= 1 for text in texts) else 1)
' "no message contains the footer twice"

echo "=== inbound USER -> TIMO ==="
uv run python scripts/fake_inbound.py "$TIMO" "$USER" "scooter"
echo
sleep 0.6

echo "=== inbound TIMO -> USER ==="
BEFORE="$(curl -sS "$BASE/inbox/thread")"
uv run python scripts/fake_inbound.py "$USER" "$TIMO" "status ping from timo"
echo
sleep 0.6
echo "=== GET /inbox/thread ==="
AFTER="$(curl -sS "$BASE/inbox/thread")"
echo "$AFTER"
echo

export BEFORE AFTER
python_check '
import json, os
before = {entry["id"] for entry in json.loads(os.environ["BEFORE"])["entries"] if entry["direction"] == "timo"}
after = [entry for entry in json.loads(os.environ["AFTER"])["entries"] if entry["direction"] == "timo"]
fresh = [entry for entry in after if entry["id"] not in before]
reply = [entry for entry in fresh if not entry["body"].startswith("status ping from timo")]
raise SystemExit(0 if not reply else 1)
' "TIMO -> USER event produced no new Timo reply"

echo "=== dedupe ==="
curl -sf -X POST "$BASE/inbox/reset" >/dev/null
uv run python scripts/fake_inbound.py "$TIMO" "$USER" "dedupe-check" evt-dedupe
uv run python scripts/fake_inbound.py "$TIMO" "$USER" "dedupe-check" evt-dedupe
sleep 0.6
DEDUPE="$(curl -sS "$BASE/inbox/thread")"
echo "$DEDUPE"
export DEDUPE
python_check '
import json, os
entries = json.loads(os.environ["DEDUPE"])["entries"]
users = [entry for entry in entries if entry["direction"] == "user" and entry["body"] == "dedupe-check"]
timos = [entry for entry in entries if entry["direction"] == "timo"]
raise SystemExit(0 if len(users) == 1 and len(timos) == 1 else 1)
' "duplicate event id stored one user entry"

if [[ "$FAIL" -ne 0 ]]; then
  exit 1
fi
