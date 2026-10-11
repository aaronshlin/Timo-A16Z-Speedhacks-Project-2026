"""Onboarding answers plus reply-signal counts. No inferred personality."""

import logging

from app.brain import state
from app.models import Recommendation, UserProfile

log = logging.getLogger(__name__)
_DEFAULT_COURSES = ["CS 106B", "Math 51", "Physics 41"]


def get_profile(phone: str) -> UserProfile:
    saved = state.load_profile(phone)
    if saved is not None:
        return saved
    return UserProfile(phone=phone, name=None, courses=list(_DEFAULT_COURSES), channel="sms")


def upsert_profile(phone: str, **fields) -> UserProfile:
    data = get_profile(phone).model_dump()
    data.update(fields)
    data["phone"] = phone
    updated = UserProfile.model_validate(data)
    state.save_profile(updated)
    return updated


def record_feedback(phone: str, rec: Recommendation, signal: str) -> None:
    profile = get_profile(phone)
    affinity = dict(profile.affinity)
    affinity[signal] = float(affinity.get(signal, 0.0)) + 1.0
    upsert_profile(phone, affinity=affinity)
    log.info("feedback phone=%s pick=%s signal=%s", phone, rec.pick.id, signal)
