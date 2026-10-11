from app.models import Recommendation
from app.phone.dial import send_sms


def notify_user(phone: str, rec: Recommendation, channel: str = "sms") -> bool:
    if channel == "call":
        from app.agent.call import place_call

        return place_call(phone, rec)
    return send_sms(phone, rec.sms_text)
