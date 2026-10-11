#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE:-http://127.0.0.1:8000}"
rm -f data/timo.db

NOW="$(uv run python -c 'from datetime import datetime; from zoneinfo import ZoneInfo; z=ZoneInfo("America/Los_Angeles"); n=datetime.now(z).replace(hour=10, minute=15, second=0, microsecond=0); print(n.isoformat())')"
PHONE="$(uv run python -c 'from app.config import settings; print(settings.DEMO_PHONE)')"

echo "=== GET /health ==="
curl -sS "$BASE/health"
echo

echo "=== POST /demo/tick now=$NOW ==="
curl -sS -X POST "$BASE/demo/tick" -H 'content-type: application/json' -d "{\"now\":\"$NOW\"}"
echo

echo "=== POST /sms/inbound bus ==="
curl -sS -X POST "$BASE/sms/inbound" -H 'content-type: application/json' -d "{\"from\":\"$PHONE\",\"body\":\"bus\"}"
echo

echo "=== POST /sms/inbound next ==="
curl -sS -X POST "$BASE/sms/inbound" -H 'content-type: application/json' -d "{\"from\":\"$PHONE\",\"body\":\"next\"}"
echo

echo "=== POST /sms/inbound done ==="
curl -sS -X POST "$BASE/sms/inbound" -H 'content-type: application/json' -d "{\"from\":\"$PHONE\",\"body\":\"done\"}"
echo

echo "=== GET /outbox ==="
curl -sS "$BASE/outbox"
echo
