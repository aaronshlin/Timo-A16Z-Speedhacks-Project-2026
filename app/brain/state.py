"""SQLite: fired gaps, last picks, profiles, facts, interactions, pending actions."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.clock import now
from app.models import (
    Fact,
    FreeWindow,
    Interaction,
    PendingAction,
    Recommendation,
    UserProfile,
)

_DB = Path(__file__).resolve().parents[2] / "data" / "timo.db"


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS integration_events (
            event_key TEXT PRIMARY KEY,
            recorded_at TEXT NOT NULL
        );
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
        CREATE TABLE IF NOT EXISTS facts (
            phone TEXT NOT NULL,
            key TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (phone, key)
        );
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            ts TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_actions (
            id TEXT PRIMARY KEY,
            phone TEXT NOT NULL,
            status TEXT NOT NULL,
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


def get_facts(phone: str) -> list[Fact]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM facts WHERE phone = ? ORDER BY key", (phone,)
        ).fetchall()
    return [Fact.model_validate_json(row["payload"]) for row in rows]


def save_fact(phone: str, fact: Fact) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO facts (phone, key, payload) VALUES (?, ?, ?)",
            (phone, fact.key, fact.model_dump_json()),
        )


def add_interaction(phone: str, item: Interaction) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO interactions (phone, ts, payload) VALUES (?, ?, ?)",
            (phone, item.ts.isoformat(), item.model_dump_json()),
        )


def get_interactions(phone: str, since: datetime, limit: int = 50) -> list[Interaction]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM interactions WHERE phone = ? AND ts >= ? "
            "ORDER BY id DESC LIMIT ?",
            (phone, since.isoformat(), limit),
        ).fetchall()
    return [Interaction.model_validate_json(row["payload"]) for row in reversed(rows)]


def add_pending(phone: str, action: PendingAction) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_actions (id, phone, status, payload) "
            "VALUES (?, ?, 'pending', ?)",
            (action.id, phone, action.model_dump_json()),
        )


def get_pending(phone: str, status: str = "pending") -> list[PendingAction]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM pending_actions WHERE phone = ? AND status = ? "
            "ORDER BY rowid",
            (phone, status),
        ).fetchall()
    return [PendingAction.model_validate_json(row["payload"]) for row in rows]


def set_pending_status(action_id: str, status: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE pending_actions SET status = ? WHERE id = ?", (status, action_id)
        )


def claim_pending(action_id: str) -> bool:
    """Move one action from pending to running. False if another run already took it."""
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE pending_actions SET status = 'running' WHERE id = ? AND status = 'pending'",
            (action_id,),
        )
    return cursor.rowcount == 1


def claim_inbound_event(event_id: str) -> bool:
    """Reserve a validated SMS before routing, shared by both webhook paths."""
    with _conn() as conn:
        cursor = conn.execute('INSERT OR IGNORE INTO integration_events VALUES (?,?)',
                              ('sms:' + event_id, now().isoformat()))
    return cursor.rowcount == 1


def add_interaction_once(phone: str, item: Interaction, reference: str) -> bool:
    """Result projection and its idempotency key commit in the same transaction."""
    with _conn() as conn:
        cursor = conn.execute('INSERT OR IGNORE INTO integration_events VALUES (?,?)',
                              ('interaction:' + reference, now().isoformat()))
        if cursor.rowcount != 1:
            return False
        conn.execute('INSERT INTO interactions(phone,ts,payload) VALUES (?,?,?)',
                     (phone, item.ts.isoformat(), item.model_dump_json()))
    return True
