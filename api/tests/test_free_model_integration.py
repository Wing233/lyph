"""Free-model integration through real HTTP routes, SDK parsing and SQLite tools.

Only the remote inference transport is a fixture. No production configuration or
credentials are read. These tests do not claim live provider availability.
"""
from contextlib import closing
import json
import sqlite3

import httpx
import pytest

from lyph import chat, chat_cache, chat_views, db, knowledge, models_routes, settings_routes
from lyph.config import settings
from test_intelligence import owner_headers, save_model, service, visitor


QUESTION = "请先调用公开查询工具核实深圳的前三家企业，再简短说明这份记录。"
MODEL = "koboldcpp/Qwen2.5-Coder-3B-Instruct"
FINAL = "公开快照匹配4家深圳企业，下方列出其中3家；岗位数是快照记录。"


class JSONInferenceFixture:
    def __init__(self):
        self.outputs = [json.dumps({"tool": "search_companies", "arguments": {"city": "深圳", "limit": 3}}, ensure_ascii=False),
                        json.dumps({"answer": FINAL}, ensure_ascii=False)]
        self.seen = []

    def handle(self, request):
        assert request.url.host == "oai.aihorde.net"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer 0000000000"
        assert request.headers.get("Client-Agent", "").startswith("lyph-")
        body = json.loads(request.content)
        self.seen.append(body)
        content = self.outputs[min(len(self.seen) - 1, len(self.outputs) - 1)]
        return httpx.Response(200, json={"id": "explicit-free-json-fixture", "object": "chat.completion", "created": 1,
            "model": MODEL, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"kudos": 0.5}})


@pytest.fixture
def free_service(service, tmp_path, monkeypatch):
    settings_routes.init_schema()
    owner = owner_headers(service)
    old = save_model(service, owner)
    with closing(db.connect()) as conn:
        old_secret = conn.execute("SELECT secret FROM model_configs WHERE id=?", (old["id"],)).fetchone()[0]
    created = service.post("/api/v1/admin/models", headers=owner, json={
        "purpose": "chat", "provider": "aihorde-public-json", "base_url": "https://oai.aihorde.net/v1",
        "model": MODEL, "api_key": "0000000000", "enabled": True, "timeout": 30,
        "prompt": "owner-private-prompt-must-not-leave-service",
    })
    assert created.status_code == 201, created.text
    enterprise = tmp_path / "public-companies.sqlite"
    stamp = "2026-10-25T11:50:29+08:00"
    with sqlite3.connect(enterprise) as conn:
        conn.executescript("""
            CREATE TABLE company(id INTEGER PRIMARY KEY,name TEXT,province TEXT,city TEXT,industry TEXT,
                search_text TEXT,active_jobs INTEGER,latest_round TEXT,company_type TEXT,updated_at TEXT,
                description TEXT,website TEXT);
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        """)
        conn.execute("INSERT INTO meta VALUES('built_at',?)", (stamp,))
        for identity, prefix, province, city, jobs in [
            (1, "A", "广东", "深圳", 9), (2, "B", "广东", "深圳", 5),
            (3, "C", "广东", "深圳", 3), (4, "D", "广东", "深圳", 1),
            (5, "E", "广西", "南宁", 20),
        ]:
            conn.execute("INSERT INTO company VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                identity, prefix + "公开企业", province, city, "software-not-for-model", prefix + "公开企业 软件",
                jobs, "A轮", "vc_portfolio", stamp, "company-description-not-for-model", "https://company.example.test"))
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(enterprise))
    provider = JSONInferenceFixture()
    models_routes.set_test_http_transport(httpx.MockTransport(provider.handle))
    guest = visitor(service)

    async def forbid_rag(*args, **kwargs):
        raise AssertionError("Public free inference must never invoke RAG")
    monkeypatch.setattr(knowledge, "retrieve", forbid_rag)
    return service, provider, guest, owner, old["id"], old_secret, enterprise


def submit(client, identity="free-request", question=QUESTION, conversation="free-conversation"):
    return client.post("/api/v1/chat/requests", headers={"Origin": settings.frontend_origin},
                       json={"request_id": identity, "conversation_id": conversation, "message": question})


def stored(client, identity="free-request"):
    result = client.get("/api/v1/chat/requests/" + identity)
    assert result.status_code == 200, result.text
    return result.json()


def outbound_tool_data(body):
    for message in body["messages"]:
        try:
            value = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("tool_result_is_untrusted_data"):
            return json.loads(value["content"])
    raise AssertionError("Expected explicitly wrapped real tool result")


@pytest.mark.parametrize("question", [QUESTION, "请先核实深圳企业的公开记录，再解释token这个词这里只表示文字单位。"])
def test_public_free_route_runs_real_sqlite_tools_and_sdk_json_protocol(free_service, question):
    client, provider, guest, _, old_id, old_secret, enterprise = free_service
    assert client.get("/api/v1/chat/access").json()["model_mode"] == "public-free-test"
    assert chat_views.resolve_task(question, {}) is None  # Exercise the model path, not the whitelist shortcut.
    with sqlite3.connect(enterprise) as conn:
        before_rows = conn.execute("SELECT * FROM company ORDER BY id").fetchall()
    response = submit(client, question=question)
    assert response.status_code == 200 and "event: done" in response.text, response.text
    state = stored(client)
    assert state["status"] == "completed" and state["answer"] == FINAL
    assert state["usage"] is None and state["citations"] == []
    assert 2 <= len(state["suggestions"]) <= 3
    table = next(block for block in state["blocks"] if block["type"] == "table")
    assert [row["name"] for row in table["rows"]] == ["A公开企业", "B公开企业", "C公开企业"]
    assert all(row["city"] == "深圳" and row["province"] == "广东" for row in table["rows"])
    assert table["query_state"]["city"] == "深圳" and table["query_state"]["limit"] == 3
    assert table["view_id"] and table["revision"] == 1
    assert len(provider.seen) == 2
    for body in provider.seen:
        assert body["model"] == MODEL and body["stream"] is False
        assert not {"tools", "tool_choice", "stream_options"} & body.keys()
    model_data = outbound_tool_data(provider.seen[-1])
    assert model_data["total"] == 4 and len(model_data["sample"]) == 3
    assert model_data["sample_only"] is True
    assert all(set(item) <= {"name", "city", "province", "active_jobs"} for item in model_data["sample"])
    wire = json.dumps(provider.seen, ensure_ascii=False)
    assert "company-description-not-for-model" not in wire
    assert "software-not-for-model" not in wire and "company.example.test" not in wire
    assert "owner-private-prompt-must-not-leave-service" not in wire
    with sqlite3.connect(enterprise) as conn:
        assert conn.execute("SELECT * FROM company ORDER BY id").fetchall() == before_rows
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (1, 0)
        calls = conn.execute("SELECT status,usage_json FROM model_calls WHERE request_id='free-request'").fetchall()
        assert len(calls) == 2 and all(tuple(row) == ("completed", None) for row in calls)
        assert conn.execute("SELECT name,status FROM chat_tool_calls WHERE request_id='free-request'").fetchone()[:] == ("search_companies", "completed")
        assert conn.execute("SELECT secret,enabled FROM model_configs WHERE id=?", (old_id,)).fetchone()[:] == (old_secret, 0)


def test_free_inference_does_not_send_even_fresh_authorized_conversation_history(free_service):
    client, provider, guest, owner, _, _, _ = free_service
    grant = client.post("/api/v1/admin/grants", headers=owner, json={"mode": "count", "limit": 5, "note": "free integration history test"}).json()
    authorized = client.post("/api/v1/chat/authorize", headers={"Origin": settings.frontend_origin}, json={"password": grant["password"]})
    assert authorized.status_code == 200
    source_stamp = chat_cache.source_fingerprint()
    with db.transaction() as conn:
        conn.execute("UPDATE chat_visitors SET free_used=1 WHERE id=?", (guest["id"],))
        conn.execute("""INSERT INTO chat_requests(id,visitor_id,conversation_id,message,answer,status,quota_kind,
                        source_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?)""", (
            "old-history", guest["id"], "free-conversation", "prior-user-private-history-marker",
            "prior-assistant-private-history-marker", "completed", "free", source_stamp, db.now()))
    response = submit(client)
    assert response.status_code == 200 and "event: done" in response.text, response.text
    wire = json.dumps(provider.seen, ensure_ascii=False)
    assert "prior-user-private-history-marker" not in wire and "prior-assistant-private-history-marker" not in wire
    assert "owner-private-prompt-must-not-leave-service" not in wire
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT used,reserved FROM chat_grants WHERE id=?", (grant["id"],)).fetchone()[:] == (1, 0)


@pytest.mark.parametrize("question", [
    "请根据手机号13800138000查询深圳企业。",
    "请根据邮箱private@example.test查询深圳企业。",
    "请核实深圳企业，api_key=synthetic-private-key。",
    "请核实深圳企业，密码：synthetic-password。",
    "请查询深圳企业，test-key-not-real。",
])
def test_sensitive_question_is_rejected_before_any_model_or_rag_call_and_no_charge(free_service, question):
    client, provider, guest, _, _, _, _ = free_service
    assert chat_views.resolve_task(question, {}) is None
    response = submit(client, question=question)
    assert response.status_code == 200 and "event: error" in response.text
    state = stored(client)
    assert state["status"] == "failed" and "移除个人或敏感信息" in state["error"]
    assert not provider.seen
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (0, 0)
        assert conn.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chat_answer_cache").fetchone()[0] == 0


def test_invalid_json_releases_reservation_and_never_caches_partial_success(free_service):
    client, provider, guest, _, _, _, _ = free_service
    provider.outputs = ['private-provider-output {"tool":"execute_sql","arguments":{}}']
    response = submit(client)
    assert response.status_code == 200 and "event: error" in response.text
    assert "private-provider-output" not in response.text
    state = stored(client)
    assert state["status"] == "failed" and "结构无效" in state["error"]
    assert len(provider.seen) == 1 and not state["blocks"]
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (0, 0)
        assert conn.execute("SELECT status FROM model_calls").fetchone()[0] == "failed"
        assert conn.execute("SELECT count(*) FROM chat_tool_calls").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chat_answer_cache").fetchone()[0] == 0
