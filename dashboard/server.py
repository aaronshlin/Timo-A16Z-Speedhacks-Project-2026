"""Read-only demo dashboard: Timo's day replayed through the real decision code, plus a live
mirror of the running app. Start the app on :8000 as usual, then:

    uv run uvicorn dashboard.server:app --port 8100

Nothing here writes to Timo: the snapshot runs in a subprocess on temp copies of state, and the
live view only GETs from the app.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app import clock
from app.config import settings

log = logging.getLogger(__name__)
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SAMPLE = _HERE / "snapshot.json"
_BASE = os.environ.get("TIMO_BASE", "http://127.0.0.1:8000").rstrip("/")
_CACHE_SECONDS = 5.0
_cache: tuple[float, dict] | None = None

app = FastAPI(title="Timo dashboard", docs_url=None, redoc_url=None)


def _build() -> dict:
    done = subprocess.run(
        [sys.executable, "-m", "dashboard.snapshot"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "snapshot failed")
    return json.loads(done.stdout)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_HERE / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/snapshot.json")
def sample():
    if not _SAMPLE.is_file():
        return JSONResponse({"error": "no saved snapshot"}, 404)
    return FileResponse(_SAMPLE, headers={"Cache-Control": "no-store"})


@app.get("/hackathon")
@app.get("/hackathon.html")
def hackathon() -> FileResponse:
    return FileResponse(_HERE / "hackathon.html", headers={"Cache-Control": "no-store"})


@app.get("/api/snapshot")
async def snapshot(fresh: bool = False):
    global _cache
    if not fresh and _cache is not None and time.monotonic() - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    try:
        data = await asyncio.to_thread(_build)
    except Exception as exc:
        log.warning("snapshot build failed: %s", exc)
        if not _SAMPLE.is_file():
            return JSONResponse({"error": f"snapshot build failed: {exc}"}, 500)
        data = json.loads(_SAMPLE.read_text())
        data.setdefault("errors", []).append({"section": "server", "error": f"live build failed, showing saved snapshot: {exc}"})
    _cache = (time.monotonic(), data)
    return data


async def _get(client: httpx.AsyncClient, path: str):
    response = await client.get(_BASE + path)
    response.raise_for_status()
    return response.json()


@app.get("/api/live")
async def live() -> dict:
    headers = {"authorization": "Bearer " + settings.CALL_TRIGGER_TOKEN} if settings.CALL_TRIGGER_TOKEN else {}
    result: dict = {"connected": False, "base": _BASE, "fetched_at": clock.now().isoformat(), "error": None,
                    "outbox": [], "thread": [], "sessions": [], "readiness": None}
    async with httpx.AsyncClient(timeout=2.0, headers=headers) as client:
        try:
            await _get(client, "/health")
        except Exception as exc:
            result["error"] = type(exc).__name__
            return result
        result["connected"] = True
        paths = {"outbox": "/outbox", "thread": "/inbox/thread", "sessions": "/voice/sessions", "readiness": "/voice/readiness"}
        replies = await asyncio.gather(*(_get(client, path) for path in paths.values()), return_exceptions=True)
    for key, reply in zip(paths, replies):
        if isinstance(reply, Exception):
            log.info("live %s unavailable: %s", paths[key], type(reply).__name__)
            continue
        if key == "outbox":
            result[key] = reply.get("outbox", [])
        elif key == "thread":
            result[key] = reply.get("entries", [])
        else:
            result[key] = reply
    return result
