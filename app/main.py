import asyncio
import logging
import hmac
import fcntl
import httpx
from pathlib import Path
from app.agent.engine import Engine
from app.agent.calling import validate_clock
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.agent.calling import service

from app.agent.mcp_server import mcp
from app.agent.poller import poll_loop
from app.agent.routes import router as agent_router
from app.config import settings
from app.phone.routes import router as phone_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

mcp_app = mcp.http_app(path="/", transport="streamable-http")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_clock(settings)
    if settings.RECEIPT_MODE != "dashboard":
        raise ValueError("Only dashboard receipts are enabled")
    if settings.LIVE_CALLS_ENABLED and settings.USE_MOCKS:
        raise ValueError("Live calendar calling requires USE_MOCKS=0")
    root = Path(__file__).resolve().parents[1]
    engine_path = Path(settings.CALL_ENGINE_STATE_PATH or root / "calling-mvp/.timo-state.json")
    engine_path.parent.mkdir(parents=True, exist_ok=True)
    instance = engine_path.with_suffix(".lock").open("a")
    try:
        fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        instance.close()
        raise RuntimeError("Timo is already running; use its manual command instead of starting another poller")
    async with mcp_app.lifespan(_app), httpx.AsyncClient() as client:
        calls = service()
        env = {"DIAL_API_KEY": settings.DIAL_API_KEY, "DIAL_FROM_NUMBER_ID": settings.DIAL_FROM_NUMBER_ID,
               "USER_PHONE": settings.DEMO_PHONE, "DIAL_WEBHOOK_SECRET": settings.DIAL_WEBHOOK_SECRET,
               "LIVE_CALLS_ENABLED": "true" if settings.LIVE_CALLS_ENABLED else "false", "RECEIPT_MODE": "dashboard"}
        engine = Engine(env, engine_path, client)
        calls.bind(engine, asyncio.get_running_loop())
        worker = asyncio.create_task(calls.worker())
        poller = asyncio.create_task(poll_loop()) if settings.POLLER_ENABLED else None
        try:
            yield
        finally:
            for task in (poller, worker):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            instance.close()


app = FastAPI(lifespan=lifespan)
app.include_router(phone_router)
app.include_router(agent_router)


@app.middleware("http")
async def protect_live_routes(request, call_next):
    if request.url.path.startswith("/sms"):
        return JSONResponse({"error": "SMS dispatch is disabled"}, 403)
    if request.url.path in ("/health", "/webhooks/dial", "/voice/webhooks/dial", "/voice/calendar-gap"):
        return await call_next(request)
    if settings.LIVE_CALLS_ENABLED or not settings.USE_MOCKS:
        expected = "Bearer " + settings.CALL_TRIGGER_TOKEN
        if not settings.CALL_TRIGGER_TOKEN or not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            return JSONResponse({"error": "A private operator token is required"}, 401)
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


app.mount("/mcp", mcp_app)
