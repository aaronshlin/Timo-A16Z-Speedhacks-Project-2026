from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Situation = Literal["audio", "phone", "laptop"]
TaskContext = Literal["noscreen", "phone", "laptop"]
CandidateKind = Literal["task", "content"]
Channel = Literal["sms", "call"]
TriggerType = Literal["new_event", "free_slot", "pre_commute", "morning_plan", "user_message"]
FactSource = Literal["user_said", "gmail", "scrub", "inferred"]


class Event(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    location: str | None = None


class Task(BaseModel):
    id: str
    title: str
    due: datetime | None
    est_minutes: int
    context: TaskContext
    course: str | None = None
    url: str | None = None


class FreeWindow(BaseModel):
    start: datetime
    end: datetime
    minutes: int
    next_event_title: str | None = None


class Candidate(BaseModel):
    kind: CandidateKind
    id: str
    title: str
    url: str | None = None
    minutes: int
    source: str
    due: datetime | None = None
    course: str | None = None


class Recommendation(BaseModel):
    pick: Candidate
    reason: str
    speech_text: str
    sms_text: str
    auto_actions: list[str] = Field(default_factory=list)


class UserProfile(BaseModel):
    phone: str
    name: str | None = None
    courses: list[str] = Field(default_factory=list)
    channel: Channel = "sms"
    quiet_hours: tuple[int, int] | None = None
    affinity: dict[str, float] = Field(default_factory=dict)


class TriggerEvent(BaseModel):
    user_id: str
    type: TriggerType
    payload: dict = Field(default_factory=dict)
    ts: datetime


class Fact(BaseModel):
    key: str
    value: str
    source: FactSource
    confidence: float = 1.0
    updated_at: datetime


class Interaction(BaseModel):
    ts: datetime
    direction: Literal["out", "in"]
    channel: Channel
    text: str
    proactive: bool = False
    pick_id: str | None = None


class PendingAction(BaseModel):
    id: str
    kind: str
    summary: str
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class Memory(BaseModel):
    profile: UserProfile
    facts: list[Fact] = Field(default_factory=list)
    recent: list[Interaction] = Field(default_factory=list)
    pending: list[PendingAction] = Field(default_factory=list)
