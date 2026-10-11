"""Dial client. Texts go out through Dial's REST API only with USE_MOCKS=0 and SMS_ENABLED=1;
otherwise they land in the mock outbox. Every text is recorded in the outbox either way."""

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)
API = "https://api.getdial.ai/api/v1"
outbox: list[str] = []


def api(method: str, path: str, body: dict | None = None) -> dict | None:
    """One Dial REST call. None on any failure: logged, never raised."""
    try:
        response = httpx.request(
            method,
            API + path,
            json=body,
            headers={"Authorization": f"Bearer {settings.DIAL_API_KEY}"},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("dial %s %s failed: %s", method, path, type(exc).__name__)
        return None


def send_sms(to: str, body: str, from_number_id: str | None = None) -> bool:
    """Text `to` from Timo's number, or from `from_number_id` (the test user's number)."""
    line = f"SMS {to}: {body}"
    outbox.append(line)
    print(line)
    if settings.USE_MOCKS or not settings.SMS_ENABLED:
        return True
    sent = api(
        "POST",
        "/messages",
        {"to": to, "fromNumberId": from_number_id or settings.DIAL_FROM_NUMBER_ID, "body": body},
    )
    return sent is not None


def get_client():
    if settings.USE_MOCKS:
        return None
    # TODO: real Dial client.
    return None
