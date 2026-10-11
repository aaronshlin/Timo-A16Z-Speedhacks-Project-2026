"""Dedicated during-call tools: authenticated, call-bound, local simulations only."""
import asyncio
import hmac
import json
import re
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from pydantic import BaseModel

from app.agent.engine import atomic_json

TOOL_NAMES = {"timo_tasks", "timo_prepare_task", "timo_confirm_task", "timo_task_status", "timo_disposition"}
mcp = FastMCP("Timo voice tasks", instructions="Use these tools only for this call's conversation_id. Never infer task approval from consent to talk or requests for other tasks. Prepare and read the exact task-specific confirmation before confirming a task. Actions are simulated locally; report their actual tool-returned status.")
router = APIRouter()
_engine = None


def bind_engine(engine):
    global _engine
    _engine = engine


def _connection_authorized(headers):
    token = _engine.env.get("OPERATOR_TOKEN", "") if _engine else ""
    return bool(token and hmac.compare_digest(headers.get("authorization", ""), "Bearer " + token))


def authorize_conversation(conversation_id):
    headers = get_http_headers(include={"authorization", "x-dial-direction", "x-dial-user-number", "x-dial-agent-number"})
    if not _connection_authorized(headers):
        raise ValueError("Private connection credential required")
    agent_number = _engine.env.get("DIAL_NUMBER", "")
    if (not re.fullmatch(r"\+[1-9]\d{7,14}", agent_number)
            or headers.get("x-dial-direction") != "outbound"
            or headers.get("x-dial-user-number") != _engine.env.get("USER_PHONE")
            or headers.get("x-dial-agent-number") != agent_number):
        raise ValueError("This tool is restricted to the enrolled outbound conversation")
    if (_engine.env.get("LIVE_CALLS_ENABLED") != "true"
            or _engine.env.get("VOICE_TOOLS_ENABLED") != "true"
            or not _engine.voice_context_valid(conversation_id)):
        raise ValueError("Conversation is not active")
    return _engine


@mcp.tool
async def timo_tasks(conversation_id: str) -> dict:
    """Return eligible tasks and the calendar overview for this active conversation. Hearing options never approves an action."""
    return await authorize_conversation(conversation_id).voice_tasks(conversation_id)


@mcp.tool
async def timo_prepare_task(conversation_id: str, item_id: str) -> dict:
    """Prepare the selected task's exact confirmation, recipient and full draft. Read them before asking for approval. Keep the confirmation_token private."""
    return await authorize_conversation(conversation_id).voice_prepare(conversation_id, item_id)


@mcp.tool
async def timo_confirm_task(conversation_id: str, item_id: str, confirmation_token: str,
                            user_text: str, spoken_confirmation: str, draft_read: str = "") -> dict:
    """Submit the caller's latest verbatim approval after the exact prepared confirmation. Include exactly what was spoken and the full draft read. Never invent approval; questions, corrections and negations require clarification. Queues only a local simulated job, capped at three approved tasks."""
    return await authorize_conversation(conversation_id).voice_confirm(
        conversation_id, item_id, confirmation_token, user_text, spoken_confirmation, draft_read)


@mcp.tool
async def timo_task_status(conversation_id: str) -> dict:
    """Read queued/running/finished/failed task statuses. Never claim an external message, booking or purchase actually happened."""
    return await authorize_conversation(conversation_id).voice_status(conversation_id)


@mcp.tool
async def timo_disposition(conversation_id: str, item_id: str, disposition: str = "next") -> dict:
    """Skip this task (next), defer this task (hold), or end the conversation (end). A task deferral can be followed by another task; only end when the caller wants to stop talking."""
    if disposition not in ("next", "hold", "end"):
        raise ValueError("Unknown disposition")
    return await authorize_conversation(conversation_id).voice_disposition(conversation_id, item_id, disposition)


def _owned_path(engine):
    return Path(engine.path).with_name(".timo-context-mcp.json")


def _owned(engine):
    path = _owned_path(engine)
    data = json.loads(path.read_text()) if path.exists() else {"owner_id": str(uuid.uuid4())}
    if not isinstance(data, dict) or not isinstance(data.get("owner_id"), str):
        raise ValueError("Private Context MCP ownership record is invalid")
    data["name"] = "Timo voice tasks " + data["owner_id"]
    return data


def _main_owned(engine):
    path = Path(engine.path).with_name('.timo-main-context-mcp.json')
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def _verify_connection(engine, owned, tool_count):
    if owned.get('status') != 'connected' or not owned.get('id'):
        return False
    observed = (await engine.api('GET', '/context-mcps/' + quote(owned['id'], safe=''))).get('contextMcp', {})
    return bool(observed.get('name') == owned.get('name') and observed.get('url') == owned.get('url')
                and observed.get('status') == 'connected' and observed.get('authMode') == 'static'
                and observed.get('toolCount') == tool_count and not observed.get('lastError'))


async def connect_context(engine, public_url):
    """Recover this installation's connection and discover the five safe tools."""
    parsed = urlsplit(public_url)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("Context MCP requires the HTTPS tunnel origin")
    if engine.env.get("LIVE_CALLS_ENABLED") != "true":
        raise ValueError("Context MCP registration is enabled only for explicit live startup")
    engine.env["VOICE_TOOLS_ENABLED"] = "false"
    owned = _owned(engine)
    path = _owned_path(engine)
    url = public_url.rstrip("/") + "/voice-mcp/"
    # The stable random ownership name lets a restart reconcile a POST whose response was lost.
    existing = await engine.api("GET", "/context-mcps")
    for connection in existing.get("contextMcps", []):
        if connection.get("name") == owned["name"]:
            await engine.api("DELETE", "/context-mcps/" + quote(connection["id"], safe=""))
    owned.update(url=url, status="connecting")
    owned.pop("id", None)
    atomic_json(path, owned)
    result = await engine.api("POST", "/context-mcps", {
        "name": owned["name"], "url": url,
        "headers": {"Authorization": "Bearer " + engine.env["OPERATOR_TOKEN"]},
        "timeoutMs": 20000,
    })
    connection = result.get("contextMcp", {})
    if not isinstance(connection.get("id"), str) or not connection["id"]:
        raise ValueError("Context MCP creation response was incomplete; restart to reconcile")
    owned.update(id=connection["id"], status="verifying")
    atomic_json(path, owned)
    observed = (await engine.api("GET", "/context-mcps/" + quote(connection["id"], safe=""))).get("contextMcp", {})
    if (observed.get("name") != owned["name"] or observed.get("url") != url
            or observed.get("status") != "connected" or observed.get("authMode") != "static"
            or observed.get("toolCount") != len(TOOL_NAMES) or observed.get("lastError")):
        raise ValueError("The private voice tools were not discovered; calling remains disabled")
    owned["status"] = "connected"
    atomic_json(path, owned)
    return {"connected": True, "tool_count": len(TOOL_NAMES)}


async def disconnect_context(engine):
    """Remove only the connection owned by this local installation."""
    engine.env["VOICE_TOOLS_ENABLED"] = "false"
    engine.env["MAIN_TOOLS_ENABLED"] = "false"
    owned = _owned(engine)
    connections = (await engine.api("GET", "/context-mcps")).get("contextMcps", [])
    for connection in connections:
        if connection.get("name") == owned["name"]:
            await engine.api("DELETE", "/context-mcps/" + quote(connection["id"], safe=""))
    owned.pop("id", None)
    owned.update(status="disconnected")
    atomic_json(_owned_path(engine), owned)
    return {"connected": False}


def _operator(request):
    if not _connection_authorized(request.headers):
        raise HTTPException(401, "Private operator credential required")
    return _engine


class ConnectIn(BaseModel):
    public_origin: str


class ReadyIn(BaseModel):
    poll_calendar: bool = False


@router.post("/voice/runtime/context")
async def runtime_connect(body: ConnectIn, request: Request):
    engine = _operator(request)
    try:
        return await connect_context(engine, body.public_origin)
    except Exception:
        raise HTTPException(503, "Private voice tool discovery failed; calls remain disabled") from None


@router.delete("/voice/runtime/context")
async def runtime_disconnect(request: Request):
    engine = _operator(request)
    task = getattr(request.app.state, "calendar_poller", None)
    if task is not None:
        task.cancel()
        request.app.state.calendar_poller = None
    try:
        return await disconnect_context(engine)
    except Exception:
        raise HTTPException(503, "Owned voice connection cleanup needs retry") from None


@router.post("/voice/runtime/ready")
async def runtime_ready(body: ReadyIn, request: Request):
    engine = _operator(request)
    from app.agent.calling import service
    engine.env["VOICE_TOOLS_ENABLED"] = "false"
    engine.env["MAIN_TOOLS_ENABLED"] = "false"
    owned = _owned(engine)
    if not service().callbacks_ready or owned.get("status") != "connected" or not owned.get("id"):
        raise HTTPException(409, "Signed callbacks and private voice tools must both be ready")
    if not await _verify_connection(engine, owned, len(TOOL_NAMES)):
        raise HTTPException(409, "Private voice tools are not connected")
    if not await _verify_connection(engine, _main_owned(engine), 8):
        raise HTTPException(409, "Main conversation tools are not connected")
    engine.env["VOICE_TOOLS_ENABLED"] = "true"
    engine.env["MAIN_TOOLS_ENABLED"] = "true"
    if body.poll_calendar and getattr(request.app.state, "calendar_poller", None) is None:
        from app.agent.poller import poll_loop
        request.app.state.calendar_poller = asyncio.create_task(poll_loop())
    return {"ready": True, "voice_tools_enabled": True, "main_tools_enabled": True, "poller_enabled": bool(getattr(request.app.state, "calendar_poller", None))}
