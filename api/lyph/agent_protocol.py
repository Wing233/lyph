"""Official AG-UI events and authenticated, stateless MCP Streamable HTTP."""
from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
from contextlib import closing
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.routing import Route
from ag_ui.core import (RunAgentInput, RunStartedEvent, RunFinishedEvent, RunErrorEvent,
                        TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent,
                        StateSnapshotEvent, ToolCallStartEvent, ToolCallArgsEvent, ToolCallEndEvent, ToolCallResultEvent)
from ag_ui.encoder import EventEncoder
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool, ToolAnnotations, TextContent, CallToolResult

from . import db, security, chat
from .auth import require_owner
from .chat_tools import TOOL_DEFINITIONS, execute_tool
from .config import settings

router = APIRouter(tags=["agent-protocol"])
_credential = contextvars.ContextVar("mcp_credential", default=None)


def init_schema():
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_credentials (
          id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL,
          label TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT,
          revoked INTEGER NOT NULL DEFAULT 0, last_used_at TEXT);
        CREATE TABLE IF NOT EXISTS mcp_calls (
          id TEXT PRIMARY KEY, credential_id TEXT NOT NULL, tool_name TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS mcp_calls_created ON mcp_calls(created_at);
        """)


def _metadata(row):
    return {key: row[key] for key in ("id", "prefix", "label", "created_at", "expires_at", "revoked", "last_used_at")}


class CredentialInput(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    expires_at: datetime | None = None


@router.get("/api/v1/admin/mcp/credentials", dependencies=[Depends(require_owner)])
def credentials():
    with closing(db.connect()) as conn:
        return {"items": [_metadata(row) for row in conn.execute("SELECT * FROM mcp_credentials ORDER BY created_at DESC")]}


@router.post("/api/v1/admin/mcp/credentials", dependencies=[Depends(require_owner)], status_code=201)
def issue(body: CredentialInput):
    expiry = body.expires_at
    if expiry and (expiry.tzinfo is None or expiry <= datetime.now(timezone.utc)):
        raise HTTPException(422, "有效期必须为带时区的未来时间")
    token = "LYPH-MCP-" + secrets.token_urlsafe(32)
    identity = db.uid()
    with db.transaction() as conn:
        conn.execute("INSERT INTO mcp_credentials(id,token_hash,prefix,label,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                     (identity, security.digest(token), token[:16], body.label, db.now(), expiry.astimezone(timezone.utc).isoformat() if expiry else None))
        row = conn.execute("SELECT * FROM mcp_credentials WHERE id=?", (identity,)).fetchone()
    db.audit("mcp.issue", identity)
    return _metadata(row) | {"token": token}


@router.delete("/api/v1/admin/mcp/credentials/{identity}", dependencies=[Depends(require_owner)])
def revoke(identity: str):
    with db.transaction() as conn:
        if not conn.execute("UPDATE mcp_credentials SET revoked=1 WHERE id=?", (identity,)).rowcount:
            raise HTTPException(404, "凭证不存在")
    db.audit("mcp.revoke", identity)
    return {"revoked": True}


@router.get("/api/v1/admin/mcp/calls", dependencies=[Depends(require_owner)])
def call_history():
    with closing(db.connect()) as conn:
        return {"items": [dict(row) for row in conn.execute("SELECT * FROM mcp_calls ORDER BY created_at DESC LIMIT 100")]}


def _authenticate(request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not token.startswith("LYPH-MCP-") or len(token) > 128:
        raise HTTPException(401, "需要独立 MCP Bearer 凭证", headers={"WWW-Authenticate": "Bearer"})
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM mcp_credentials WHERE token_hash=?", (security.digest(token),)).fetchone()
        if not row or row["revoked"] or (row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)):
            raise HTTPException(401, "MCP 凭证无效、过期或已吊销", headers={"WWW-Authenticate": "Bearer"})
        conn.execute("UPDATE mcp_credentials SET last_used_at=? WHERE id=?", (db.now(), row["id"]))
    chat.rate_limit("mcp.credential", row["id"], 60)
    return row["id"]


def make_manager():
    server = Server("lyph-public-tools")

    @server.list_tools()
    async def list_tools():
        return [Tool(name=entry["function"]["name"], description=entry["function"]["description"],
                     inputSchema=entry["function"]["parameters"],
                     annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
                for entry in TOOL_DEFINITIONS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        allowed = {entry["function"]["name"] for entry in TOOL_DEFINITIONS}
        # Recheck revocation for each invocation, including immediately after discovery.
        identity = _credential.get()
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT * FROM mcp_credentials WHERE id=?", (identity,)).fetchone()
        if not row or row["revoked"] or (row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)):
            return CallToolResult(isError=True, content=[TextContent(type="text", text="凭证已失效")])
        result = await asyncio.to_thread(execute_tool, name, arguments) if name in allowed else {"error": {"message": "工具未开放"}}
        # Protocol returns public query data; internal view actions and state never cross this boundary.
        public = {key: value for key, value in result.items() if key in {"data", "source", "updated_at", "error"}}
        with db.transaction() as conn:
            conn.execute("INSERT INTO mcp_calls VALUES(?,?,?,?,?)", (db.uid(), identity, name[:80], "failed" if result.get("error") else "completed", db.now()))
        return CallToolResult(isError=bool(result.get("error")), structuredContent=public,
                              content=[TextContent(type="text", text=json.dumps(public, ensure_ascii=False))])

    # Origin and Host validated at the application boundary below; no DNS-derived allowlist.
    return StreamableHTTPSessionManager(server, stateless=True, json_response=True,
                                       max_request_body_size=65536,
                                       security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))


class MCPTransport:
    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        try:
            origin = request.headers.get("origin")
            if origin and origin != settings.frontend_origin:
                raise HTTPException(403, "不允许的请求来源")
            chat.rate_limit("mcp.ip", request.client.host if request.client else "unknown", 120)
            identity = _authenticate(request)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, send)
            return
        token = _credential.set(identity)
        try:
            await scope["app"].state.mcp_manager.handle_request(scope, receive, send)
        finally:
            _credential.reset(token)


def register(app):
    app.router.routes.append(Route("/api/v1/mcp", MCPTransport(), methods=["GET", "POST", "DELETE"]))


@router.post("/api/v1/agent/runs")
async def agent_run(body: RunAgentInput, request: Request):
    users = [item for item in body.messages if item.role == "user"]
    if not users or not isinstance(users[-1].content, str):
        raise HTTPException(422, "需要一条文本用户消息")
    if body.tools or body.context or body.state or body.resume:
        raise HTTPException(422, "工具与上下文由服务端管理，请只发送文本消息")
    try:
        payload = chat.ChatInput(request_id=body.run_id, conversation_id=body.thread_id,
                                 message=users[-1].content, cache_mode=body.forwarded_props.get("cache_mode", "auto"))
    except (ValidationError, AttributeError):
        raise HTTPException(422, "运行标识或文本参数无效")
    response = await chat.submit(payload, request)
    encoder = EventEncoder()

    async def events():
        message_id = body.run_id + "-answer"
        started = False
        tool_ids = {}
        state = {"blocks": [], "suggestions": [], "citations": []}
        async for frame in response.body_iterator:
            if isinstance(frame, bytes):
                frame = frame.decode()
            kind, data = None, None
            for line in frame.splitlines():
                if line.startswith("event: "):
                    kind = line[7:]
                if line.startswith("data: "):
                    data = json.loads(line[6:])
            if kind == "start":
                yield encoder.encode(RunStartedEvent(thread_id=body.thread_id, run_id=body.run_id))
            elif kind == "delta":
                if not started:
                    started = True
                    yield encoder.encode(TextMessageStartEvent(message_id=message_id, role="assistant"))
                yield encoder.encode(TextMessageContentEvent(message_id=message_id, delta=data["text"]))
            elif kind == "tool":
                name, status = data["name"], data["status"]
                if status == "running":
                    tool_id = data.get("tool_call_id") or db.uid()
                    tool_ids[name] = tool_id
                    yield encoder.encode(ToolCallStartEvent(tool_call_id=tool_id, tool_call_name=name, parent_message_id=message_id))
                    if "arguments" in data:
                        yield encoder.encode(ToolCallArgsEvent(tool_call_id=tool_id, delta=json.dumps(data["arguments"], ensure_ascii=False)))
                elif name in tool_ids:
                    tool_id = tool_ids.pop(name)
                    yield encoder.encode(ToolCallEndEvent(tool_call_id=tool_id))
                    yield encoder.encode(ToolCallResultEvent(message_id=db.uid(), tool_call_id=tool_id, role="tool", content=json.dumps({"status": status})))
            elif kind in {"blocks", "suggestions", "citations"}:
                state[kind] = data["items"]
                yield encoder.encode(StateSnapshotEvent(snapshot=dict(state)))
            elif kind in {"done", "error"}:
                if data.get("answer") and not started:
                    started = True
                    yield encoder.encode(TextMessageStartEvent(message_id=message_id, role="assistant"))
                    yield encoder.encode(TextMessageContentEvent(message_id=message_id, delta=data["answer"]))
                if started:
                    yield encoder.encode(TextMessageEndEvent(message_id=message_id))
                if kind == "done":
                    yield encoder.encode(StateSnapshotEvent(snapshot={key: data.get(key) for key in ("blocks", "suggestions", "citations", "cache")}))
                    yield encoder.encode(RunFinishedEvent(thread_id=body.thread_id, run_id=body.run_id, result=data))
                else:
                    yield encoder.encode(RunErrorEvent(message=data.get("error") or "问答未完成"))
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
