"""Explicit SDK HTTP/SSE fixtures for the bounded public-data tool loop."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from lyph import chat, chat_agent, chat_tools, db, maintenance, models_routes, settings_routes
from lyph.config import settings
from test_intelligence import ProviderFixture, owner_headers, save_model, service, visitor


KNOWN_USAGE = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


def tool(name, arguments, identifier="call-one"):
    return {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else arguments,
            "id": identifier}


def tools_stage(*calls, usage=KNOWN_USAGE):
    return {"calls": list(calls), "usage": usage}


def final_stage(text="已根据公开资料完成回答。", usage=KNOWN_USAGE):
    return {"text": text, "usage": usage}


class ToolStreamFixture(ProviderFixture):
    """Only the test transport returns scripted output; the real SDK parses every chunk."""
    def __init__(self, stages):
        super().__init__()
        self.stages = stages
        self.stream_requests = []

    def handle(self, request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return super().handle(request)
        self.calls.append((str(request.url), body))
        self.stream_requests.append(body)
        stage = self.stages[min(len(self.stream_requests) - 1, len(self.stages) - 1)]
        if stage.get("http_error"):
            return httpx.Response(stage["http_error"], json={"error": {"message": "provider-secret-not-for-user", "type": "server_error", "code": stage.get("error_code")}})
        packets = []

        def chunk(delta, reason=None):
            packets.append({"id": "explicit-tool-stream", "object": "chat.completion.chunk", "created": 1,
                            "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]})

        if "calls" in stage:
            # Interleave calls, and split both names and JSON arguments across packets.
            for index, call in enumerate(stage["calls"]):
                chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                        "function": {"name": call["name"][:4], "arguments": call["arguments"][:3]}}]})
            for index, call in enumerate(stage["calls"]):
                chunk({"tool_calls": [{"index": index, "function": {"name": call["name"][4:], "arguments": call["arguments"][3:]}}]})
            chunk({}, "tool_calls")
        else:
            text = stage.get("text", "")
            chunk({"content": text[:3]})
            chunk({"content": text[3:]})
            if not stage.get("cutoff"):
                chunk({}, "stop")
        if stage.get("usage") is not None:
            packets.append({"id": "explicit-tool-stream", "object": "chat.completion.chunk", "created": 1,
                            "model": body["model"], "choices": [], "usage": stage["usage"]})
        wire = "".join("data: " + json.dumps(packet, ensure_ascii=False) + "\n\n" for packet in packets) + "data: [DONE]\n\n"
        return httpx.Response(200, content=wire.encode(), headers={"content-type": "text/event-stream"})


def configure(service, stages):
    headers = owner_headers(service)
    save_model(service, headers)
    guest = visitor(service)
    settings_routes.init_schema()
    site = {"profile": {"name": "lyph", "bio": "公开资料"},
            "projects": [{"id": "real-project", "title": "数据库项目", "description": "真实公开项目"}],
            "stack": [{"name": "SQLite", "description": "只读查询"}], "interests": []}
    with db.transaction() as conn:
        conn.execute("UPDATE site_settings SET payload_json=? WHERE id=1", (json.dumps(site, ensure_ascii=False),))
    provider = ToolStreamFixture(stages)
    models_routes.set_test_http_transport(httpx.MockTransport(provider.handle))
    return provider, guest


def submit(service, identifier="agent-request", *, retry=False):
    return service.post("/api/v1/chat/requests", headers={"Origin": settings.frontend_origin},
                        json={"request_id": identifier, "conversation_id": "agent-conversation", "message": "请介绍公开项目", "retry": retry})


def events(response):
    parsed = []
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        name = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        data = next((line[5:].strip() for line in lines if line.startswith("data:")), None)
        if name and data:
            parsed.append((name, json.loads(data)))
    return parsed


def stored(service, identifier="agent-request"):
    return service.get("/api/v1/chat/requests/" + identifier).json()


def test_fragmented_multiple_tools_errors_and_followups_persist_and_replay_once(service):
    suggestions = ["这个项目用了哪些技术？", "还有哪些相关公开文章？"]
    provider, guest = configure(service, [
        tools_stage(tool("get_public_profile", {"section": "projects"}, "project"),
                    tool("get_public_profile", {"section": "stack"}, "stack")),
        tools_stage(tool("unknown_tool", {}, "unknown"), tool("get_public_profile", "{invalid-json", "invalid"),
                    tool("suggest_followups", {"questions": suggestions}, "questions")),
        final_stage(),
    ])
    response = submit(service)
    assert response.status_code == 200
    names = [name for name, _ in events(response)]
    assert {"tool", "blocks", "suggestions", "delta", "done"}.issubset(names)
    result = stored(service)
    assert result["status"] == "completed" and result["answer"] == "已根据公开资料完成回答。"
    assert result["suggestions"] == suggestions and len(result["blocks"]) == 2
    assert result["blocks"][0]["items"][0]["title"] == "数据库项目"
    assert result["usage"] == {"prompt_tokens": 30, "completion_tokens": 6, "total_tokens": 36}
    assert len(provider.stream_requests) == 3
    tool_messages = [message for message in provider.stream_requests[-1]["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 5
    assert json.loads(tool_messages[2]["content"])["error"]["code"] == "unknown_tool"
    assert json.loads(tool_messages[3]["content"])["error"]["code"] == "invalid_arguments"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (1, 0)
        assert conn.execute("SELECT count(*) FROM model_calls WHERE request_id='agent-request'").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM chat_tool_calls WHERE request_id='agent-request'").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM chat_tool_calls WHERE status='failed'").fetchone()[0] == 2
        conn.execute("UPDATE model_configs SET enabled=0")
    replay = submit(service)
    assert dict(events(replay))["done"]["blocks"] == result["blocks"]
    assert dict(events(replay))["done"]["suggestions"] == suggestions
    assert len(provider.stream_requests) == 3
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 0


@pytest.mark.parametrize("first,second,expected", [
    (KNOWN_USAGE, KNOWN_USAGE, {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}),
    (None, KNOWN_USAGE, None), (KNOWN_USAGE, None, None), (None, None, None),
    ({"prompt_tokens": 10, "completion_tokens": None, "total_tokens": None}, KNOWN_USAGE, None),
])
def test_any_unknown_model_usage_keeps_request_total_unknown(service, first, second, expected):
    configure(service, [tools_stage(tool("get_public_profile", {}, "profile"), usage=first), final_stage(usage=second)])
    assert "event: done" in submit(service).text
    assert stored(service)["usage"] == expected


@pytest.mark.parametrize("questions", [
    ["仅一个问题？"], ["重复问题？", "重复问题？"],
    ["第一个？", "第二个？", "第三个？", "第四个？"],
    ["正确问题？", "x" * 121], ["第一个？", "第二个？", 3], None,
])
def test_invalid_followups_never_leave_partial_suggestions(service, questions):
    configure(service, [tools_stage(tool("suggest_followups", {"questions": questions})), final_stage()])
    assert "event: done" in submit(service).text
    result = stored(service)
    assert result["suggestions"] == chat_agent.fallback_suggestions([])
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT status FROM chat_tool_calls").fetchone()[0] == "failed"


@pytest.mark.parametrize("failure", [{"http_error": 500}, {"text": "未完成的文字", "cutoff": True}, {"http_error": 400}])
def test_failure_after_tools_releases_quota_and_retry_replaces_partial_blocks(service, failure):
    provider, guest = configure(service, [tools_stage(tool("get_public_profile", {"section": "projects"})), failure])
    response = submit(service)
    assert "event: error" in response.text and "provider-secret-not-for-user" not in response.text
    assert stored(service)["status"] == "failed"
    assert len(stored(service)["blocks"]) == 1
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (0, 0)
        assert conn.execute("SELECT count(*) FROM model_calls WHERE status='running'").fetchone()[0] == 0
    provider.stages = [final_stage("重试完成。")]
    assert "event: done" in submit(service, retry=True).text
    assert stored(service)["blocks"] == [] and stored(service)["attempts"] == 2


def test_expired_provider_subscription_is_clear_safe_and_does_not_charge(service):
    configure(service, [{"http_error": 400, "error_code": "InvalidSubscription"}])
    response = submit(service)
    assert "模型供应商订阅不可用" in response.text
    assert "provider-secret-not-for-user" not in response.text
    assert stored(service)["status"] == "failed"
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1


def test_round_budget_forces_synthesis_and_rejects_disallowed_extra_tools(service, monkeypatch):
    monkeypatch.setattr(chat_agent, "MAX_ROUNDS", 2)
    provider, _ = configure(service, [tools_stage(tool("get_public_profile", {"section": "projects"}))])
    assert "event: error" in submit(service).text
    assert len(provider.stream_requests) == 2 and provider.stream_requests[-1]["tool_choice"] == "none"
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM chat_tool_calls").fetchone()[0] == 1


def test_total_tool_budget_forces_final_answer_without_executing_more_tools(service, monkeypatch):
    monkeypatch.setattr(chat_agent, "MAX_TOOLS", 2)
    provider, _ = configure(service, [tools_stage(tool("get_public_profile", {"section": "projects"}))])
    assert "event: error" in submit(service).text
    assert len(provider.stream_requests) == 3 and provider.stream_requests[-1]["tool_choice"] == "none"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM chat_tool_calls").fetchone()[0] == 2
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1


def test_tool_batch_over_budget_is_rejected_before_any_tool_executes(service, monkeypatch):
    monkeypatch.setattr(chat_agent, "MAX_TOOLS", 2)
    provider, _ = configure(service, [tools_stage(*[tool("get_public_profile", {}, f"id-{i}") for i in range(3)])])
    assert "event: error" in submit(service).text
    assert len(provider.stream_requests) == 1
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM chat_tool_calls").fetchone()[0] == 0
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1


def test_shared_count_grant_charges_once_for_many_tools_and_not_for_replay(service):
    provider, guest = configure(service, [
        tools_stage(tool("get_public_profile", {"section": "projects"}, "project"),
                    tool("get_public_profile", {"section": "stack"}, "stack")), final_stage(),
    ])
    headers = owner_headers(service)
    grant = service.post("/api/v1/admin/grants", json={"mode": "count", "limit": 2}, headers=headers).json()
    with db.transaction() as conn:
        conn.execute("UPDATE chat_visitors SET free_used=1 WHERE id=?", (guest["id"],))
    assert service.post("/api/v1/chat/authorize", json={"password": grant["password"]},
                        headers={"Origin": settings.frontend_origin}).status_code == 200
    assert "event: done" in submit(service).text
    assert "event: done" in submit(service).text
    assert len(provider.stream_requests) == 2
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT used,reserved FROM chat_grants WHERE id=?", (grant["id"],)).fetchone()[:] == (1, 0)
    assert service.get("/api/v1/chat/access").json()["remaining"] == 1


def test_reaching_tool_budget_still_allows_one_successful_synthesis(service, monkeypatch):
    monkeypatch.setattr(chat_agent, "MAX_TOOLS", 2)
    provider, _ = configure(service, [
        tools_stage(tool("get_public_profile", {"section": "projects"}, "project"),
                    tool("get_public_profile", {"section": "stack"}, "stack")), final_stage(),
    ])
    assert "event: done" in submit(service).text
    assert len(provider.stream_requests) == 2 and provider.stream_requests[-1]["tool_choice"] == "none"
    assert stored(service)["status"] == "completed"


def test_cancellation_after_tool_execution_marks_tool_interrupted_and_releases_quota(service, monkeypatch):
    configure(service, [tools_stage(tool("get_public_profile", {"section": "projects"}))])
    original = chat_tools.execute_tool
    def cancel_after_read(name, arguments):
        result = original(name, arguments)
        with db.transaction() as conn:
            conn.execute("UPDATE chat_requests SET cancel_requested=1 WHERE id='agent-request'")
        return result
    monkeypatch.setattr(chat_tools, "execute_tool", cancel_after_read)
    assert "event: error" in submit(service).text
    assert stored(service)["status"] == "cancelled"
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT status FROM chat_tool_calls").fetchone()[0] == "interrupted"


def test_restart_recovery_interrupts_tools_and_releases_only_running_reservations(service):
    _, guest = configure(service, [final_stage()])
    row, attempt = chat.reserve(chat.ChatInput(request_id="recover-tools", conversation_id="recover", message="q"),
                                guest["id"], models_routes.config_for("chat"))
    with db.transaction() as conn:
        conn.execute("INSERT INTO chat_tool_calls(id,request_id,attempt_id,name,status,created_at) VALUES(?,?,?,?,?,?)",
                     ("tool-running", row["id"], attempt, "get_public_profile", "running", db.now()))
        conn.execute("INSERT INTO model_calls(id,request_id,attempt_id,purpose,model_id,model_version,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("model-running", row["id"], attempt, "chat", row["model_id"], row["model_version"], "running", db.now()))
    chat.recover_interrupted()
    chat.recover_interrupted()
    with closing(db.connect()) as conn:
        for table in ("chat_requests", "chat_attempts", "model_calls", "chat_tool_calls"):
            assert conn.execute("SELECT status FROM " + table).fetchone()[0] == "interrupted"
        assert conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (0, 0)


def test_retention_clears_expired_rich_content_even_when_text_already_null(service):
    guest = visitor(service)
    maintenance.init_schema()
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    recent = db.now()
    with db.transaction() as conn:
        for identifier, stamp, status in [("old-rich", old, "completed"), ("recent-rich", recent, "completed"), ("inflight-rich", old, "running")]:
            conn.execute("INSERT INTO chat_requests(id,visitor_id,conversation_id,status,quota_kind,created_at,blocks_json,suggestions_json) VALUES(?,?,?,?,?,?,?,?)",
                         (identifier, guest["id"], "c", status, "free", stamp, '[{"type":"cards","items":[]}]', '["追问一？","追问二？"]'))
            conn.execute("INSERT INTO chat_tool_calls(id,request_id,attempt_id,name,status,created_at) VALUES(?,?,?,?,?,?)",
                         (identifier + "-tool", identifier, "attempt", "get_public_profile", "completed", stamp))
    result = maintenance.cleanup()
    assert result["chat_bodies"] == 1
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT blocks_json,suggestions_json FROM chat_requests WHERE id='old-rich'").fetchone()[:] == (None, None)
        assert conn.execute("SELECT blocks_json FROM chat_requests WHERE id='recent-rich'").fetchone()[0]
        assert conn.execute("SELECT blocks_json FROM chat_requests WHERE id='inflight-rich'").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM chat_tool_calls WHERE id='old-rich-tool'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chat_tool_calls WHERE id='recent-rich-tool'").fetchone()[0] == 1
