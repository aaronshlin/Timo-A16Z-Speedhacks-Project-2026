"""Send one SMS each way between the Timo number and the user proxy. Requires MOCK_DIAL=0."""

import asyncio
import sys

from app.config import settings
from app.phone.dial import timo_says, user_says


async def main() -> int:
    if settings.MOCK_DIAL:
        print("MOCK_DIAL=1, so nothing was sent. Set MOCK_DIAL=0 and rerun.")
        return 1
    timo = await timo_says("Timo pair test")
    user = await user_says("User pair test")
    print({"direction": "timo", "status": timo["status"], "id": timo["id"]})
    print({"direction": "user", "status": user["status"], "id": user["id"]})
    return 0 if timo["status"] == "sent" and user["status"] == "sent" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
