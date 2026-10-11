import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.agent.mcp_server import mcp
from app.agent.poller import poll_loop
from app.agent.routes import router as agent_router
from app.config import settings
from app.phone.routes import router as phone_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

mcp_app = mcp.http_app(path="/", transport="streamable-http")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp_app.lifespan(_app):
        task = None
        if settings.POLLER_ENABLED:
            task = asyncio.create_task(poll_loop())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


app = FastAPI(lifespan=lifespan)
app.include_router(phone_router)
app.include_router(agent_router)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


app.mount("/mcp", mcp_app)
