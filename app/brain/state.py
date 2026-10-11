"""SQLite: fired gaps, last pick per phone, profiles."""

import json
import sqlite3
from pathlib import Path

from app.clock import now
from app.models import FreeWindow, Recommendation, UserProfile

_DB = Path(__file__).resolve().parents[2] / "data" / "timo.db"


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fired_gaps (
            gap_key TEXT PRIMARY KEY,
            fired_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS last_pick (
            phone TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profiles (
            phone TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        """
    )
    return conn


def gap_key(window: FreeWindow) -> str:
    return f"{window.start.isoformat()}|{window.end.isoformat()}"


def has_fired(window: FreeWindow) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM fired_gaps WHERE gap_key = ?", (gap_key(window),)
        ).fetchone()
    return row is not None


def mark_fired(window: FreeWindow) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fired_gaps (gap_key, fired_at) VALUES (?, ?)",
            (gap_key(window), now().isoformat()),
        )


def get_last_pick(phone: str) -> Recommendation | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT payload FROM last_pick WHERE phone = ?", (phone,)
        ).fetchone()
    if row is None:
        return None
    return Recommendation.model_validate_json(row["payload"])


def set_last_pick(phone: str, rec: Recommendation) -> None:
    payload = rec.model_dump_json()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO last_pick (phone, payload) VALUES (?, ?)",
            (phone, payload),
        )


def load_profile(phone: str) -> UserProfile | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT payload FROM profiles WHERE phone = ?", (phone,)
        ).fetchone()
    if row is None:
        return None
    return UserProfile.model_validate_json(row["payload"])


def save_profile(profile: UserProfile) -> None:
    payload = json.dumps(profile.model_dump(mode="json"))
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO profiles (phone, payload) VALUES (?, ?)",
            (profile.phone, payload),
        )
