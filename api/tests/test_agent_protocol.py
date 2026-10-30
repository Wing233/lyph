"""Exercise official MCP client over HTTP transport and shared AG-UI quota settlement."""
from contextlib import closing
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from lyph import agent_protocol, chat, db, settings_routes
from test_intelligence import service, owner_headers, save_model, visitor


def setup_protocol(service):
    agent_protocol.init_schema()
    settings_routes.init_schema()
    service.app_fixture.include_router(agent_protocol.router)
    agent_protocol.register(service.app_fixture)


def test_mcp_credential_metadata_csrf_and_chat_password_are_separate(service):
    setup_protocol(service)
    headers = owner_headers(service)
    assert service.post("/api/v1/admin/mcp/credentials", json={"label": "test"}).status_code == 403
    issued = service.post("/api/v1/admin/mcp/credentials", headers=headers, json={"label": "test"})
    assert issued.status_code == 201
    data = issued.json()
    assert data["token"].startswith("LYPH-MCP-")
    assert data["token"] not in service.get("/api/v1/admin/mcp/credentials").text
    denied = service.post("/api/v1/mcp", headers={"Authorization": "Bearer LYPH-chat-password"}, json={})
    assert denied.status_code == 401
    assert service.post("/api/v1/chat/authorize", json={"password": data["token"]}, headers={"Origin": "http://localhost:3000"}).status_code in {401, 403}


@pytest.mark.asyncio
async def test_official_mcp_client_discovers_calls_and_revokes_without_chat_quota(service):
    setup_protocol(service)
    owner = owner_headers(service)
    issued = service.post("/api/v1/admin/mcp/credentials", headers=owner, json={"label": "SDK integration"}).json()
    manager = agent_protocol.make_manager()
    service.app_fixture.state.mcp_manager = manager
    async with manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app_fixture), base_url="http://testserver",
                                     headers={"Authorization": "Bearer " + issued["token"]}) as http_client:
            async with streamable_http_client("http://testserver/api/v1/mcp", http_client=http_client) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} >= {"get_public_profile", "search_companies"}
                    assert all(tool.annotations.readOnlyHint for tool in tools.tools)
                    result = await session.call_tool("get_public_profile", {"section": "projects"})
                    assert not result.isError and "data" in result.structuredContent
                    invalid = await session.call_tool("execute_sql", {"sql": "SELECT * FROM chat_grants"})
                    assert invalid.isError
            assert service.delete("/api/v1/admin/mcp/credentials/" + issued["id"], headers=owner).status_code == 200
            revoked = await http_client.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert revoked.status_code == 401
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chat_requests").fetchone()[0] == 0
    assert service.get("/api/v1/admin/mcp/calls").json()["items"][0]["status"] in {"completed", "failed"}


def test_agui_official_events_share_cache_and_idempotent_quota(service):
    setup_protocol(service)
    save_model(service, owner_headers(service), "chat")
    guest = visitor(service)
    body = {"threadId": "protocol-thread", "runId": "protocol-run", "state": {}, "messages": [{"id": "u1", "role": "user", "content": "你的项目"}], "tools": [], "context": [], "forwardedProps": {}}
    headers = {"Origin": "http://localhost:3000"}
    first = service.post("/api/v1/agent/runs", json=body, headers=headers)
    assert first.status_code == 200
    frames = [json.loads(line[6:]) for line in first.text.splitlines() if line.startswith("data: ")]
    assert frames[0]["type"] == "RUN_STARTED" and frames[-1]["type"] == "RUN_FINISHED", first.text
    assert any(frame["type"] == "STATE_SNAPSHOT" and "blocks" in frame["snapshot"] for frame in frames)
    assert any(frame["type"] == "TOOL_CALL_START" for frame in frames)
    second = service.post("/api/v1/agent/runs", json=body, headers=headers)
    assert second.status_code == 200 and '"RUN_FINISHED"' in second.text
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()
        assert tuple(row) == (1, 0)
        assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
    body["runId"] = "other"
    body["state"] = {"private": "injected"}
    assert service.post("/api/v1/agent/runs", json=body, headers=headers).status_code == 422
