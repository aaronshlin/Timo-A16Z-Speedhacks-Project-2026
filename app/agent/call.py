"""Mock outbound call. Speaks the pick into the shared outbox."""

from app.models import Recommendation
from app.phone.dial import outbox


def place_call(phone: str, rec: Recommendation) -> bool:
    line = f"CALL: {rec.speech_text}"
    outbox.append(line)
    print(line)
    _ = phone
    return True
