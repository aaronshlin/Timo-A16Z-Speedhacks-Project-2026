"""Mock Dial SMS. Replace with the real client."""

from app.config import settings

outbox: list[str] = []


def send_sms(to: str, body: str) -> bool:
    line = f"SMS {to}: {body}"
    outbox.append(line)
    print(line)
    return True


def get_client():
    if settings.USE_MOCKS:
        return None
    # TODO: real Dial client.
    return None
