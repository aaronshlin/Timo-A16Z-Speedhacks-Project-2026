from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.phone.sms_handler import handle_reply

router = APIRouter()


class Inbound(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_number: str = Field(alias="from")
    body: str


@router.post("/sms/inbound")
def inbound(payload: Inbound) -> dict[str, str]:
    return {"reply": handle_reply(payload.from_number, payload.body)}
