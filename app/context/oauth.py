"""Google access token from the refresh token saved by scripts/google_auth_setup.py."""

import json
import logging
import time
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
_ROOT = Path(__file__).resolve().parents[2]


def token_path() -> Path:
    path = Path(settings.GOOGLE_TOKEN_PATH or "token.json")
    return path if path.is_absolute() else _ROOT / path


def _expires_at(token: dict) -> float:
    try:
        return float(token["expires_at"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def get_access_token() -> str:
    path = token_path()
    if not path.exists():
        raise RuntimeError(f"{path} not found. Run scripts/google_auth_setup.py")
    token = json.loads(path.read_text())

    # Wall clock on purpose: NOW_OVERRIDE must not change when a token expires.
    if token.get("access_token") and _expires_at(token) - 60 > time.time():
        return token["access_token"]

    resp = httpx.post(
        TOKEN_URI,
        data={
            "client_id": token["client_id"],
            "client_secret": token["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=5.0,
    )
    resp.raise_for_status()
    fresh = resp.json()
    token["access_token"] = fresh["access_token"]
    token["expires_at"] = time.time() + fresh.get("expires_in", 3600)
    path.write_text(json.dumps(token, indent=2))
    log.info("refreshed google access token")
    return token["access_token"]
