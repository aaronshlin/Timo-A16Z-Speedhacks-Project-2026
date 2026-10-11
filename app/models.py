from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Situation = Literal["audio", "phone", "laptop"]
TaskContext = Literal["noscreen", "phone", "laptop"]
CandidateKind = Literal["task", "content"]
Channel = Literal["sms", "call"]


class Event(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime


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
