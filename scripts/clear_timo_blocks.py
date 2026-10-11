#!/usr/bin/env python3
"""Remove the calendar holds Timo created. Demo runs leave one per fired gap.

    python scripts/clear_timo_blocks.py            # dry run
    python scripts/clear_timo_blocks.py --delete

Only touches events Timo tagged itself, never anything else on the calendar.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clock import now  # noqa: E402
from app.context.calendar import _API  # noqa: E402
from app.context.oauth import get_access_token  # noqa: E402


def main() -> int:
    delete = "--delete" in sys.argv
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    start: datetime = now() - timedelta(days=1)
    resp = httpx.get(
        _API,
        params={
            "timeMin": start.isoformat(),
            "timeMax": (start + timedelta(days=8)).isoformat(),
            "singleEvents": "true",
            "privateExtendedProperty": "timo=1",
            "maxResults": 250,
        },
        headers=headers,
        timeout=5.0,
    )
    resp.raise_for_status()
    blocks = [b for b in resp.json().get("items", []) if b.get("status") != "cancelled"]
    if not blocks:
        print("no Timo blocks")
        return 0
    for block in blocks:
        print(f"{'deleting' if delete else 'would delete'}  {block['start'].get('dateTime')}  {block.get('summary')}")
        if delete:
            httpx.delete(f"{_API}/{block['id']}", headers=headers, timeout=5.0).raise_for_status()
    if not delete:
        print(f"{len(blocks)} block(s). Rerun with --delete to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
