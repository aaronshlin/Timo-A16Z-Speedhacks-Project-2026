from fastapi import APIRouter, Body
from pydantic import BaseModel

from app.agent.poller import run_tick
from app.phone.dial import outbox

router = APIRouter()


class TickIn(BaseModel):
    now: str | None = None


@router.post("/demo/tick")
def demo_tick(body: TickIn | None = Body(default=None)) -> dict:
    when = body.now if body is not None else None
    rec = run_tick(when)
    if rec is None:
        return {"recommendation": None}
    return rec.model_dump(mode="json")


@router.get("/outbox")
def get_outbox() -> dict:
    return {"outbox": list(outbox)}
