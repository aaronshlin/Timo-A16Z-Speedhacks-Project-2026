"""POST a signed message.received webhook to the local server.

Usage: python scripts/fake_inbound.py <to> <from> <body> [event_id]
"""

import hashlib
import hmac
import json
import sys
import time
import urllib.request
import uuid

from app.config import settings


def main() -> int:
    if len(sys.argv) not in (4, 5):
        print("usage: fake_inbound.py <to> <from> <body> [event_id]", file=sys.stderr)
        return 2
    to, sender, body = sys.argv[1:4]
    event_id = sys.argv[4] if len(sys.argv) == 5 else uuid.uuid4().hex
    payload = {
        "type": "message.received",
        "data": {"from": sender, "to": to, "body": body},
    }
    raw = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    secret = settings.DIAL_SMS_WEBHOOK_SECRET
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + raw, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        "http://127.0.0.1:8000/sms/inbound",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Dial-Event-Type": "message.received",
            "X-Dial-Event-ID": event_id,
            "X-Dial-Signature": f"t={stamp},v1={digest}",
        },
    )
    with urllib.request.urlopen(request) as response:
        print(response.status)
        print(response.read().decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
