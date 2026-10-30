"""Real SQLite view ownership, conditional routing and no-model action contracts."""
import json
import sqlite3

from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from lyph import chat_tools, chat_views
from test_chat_tools import data_tools  # noqa: F401


@pytest.fixture
def views(data_tools):
    chat_views.init_schema()
    return data_tools


def _create(name="search_companies", args=None, visitor="visitor", conversation="conversation"):
    result = chat_tools.execute_tool(name, args or {})
    assert not result.get("error")
    blocks = chat_views.prepare_blocks(result["blocks"], visitor, conversation, "request")
    return blocks, result


def _table(blocks):
    return next(block for block in blocks if block["type"] == "table")


def _action(blocks, action, value=None, visitor="visitor"):
    return chat_views.perform_action(blocks[0]["view_id"], visitor, chat_views.ActionBody(action=action, revision=blocks[0]["revision"], value=value))


def test_rule_aliases_and_all_sensitive_conditions_are_preserved():
    assert chat_views.resolve_task("项目列表")["canonical"] == chat_views.resolve_task("有哪些项目")["canonical"]
    guangdong = chat_views.resolve_task("找广东AI企业")
    guangxi = chat_views.resolve_task("找广西AI企业")
    assert guangdong["canonical"] != guangxi["canonical"]
    assert guangdong["standalone"] is True
    assert guangdong["steps"][0]["arguments"]["province"] == "广东"
    assert chat_views.resolve_task("找前3家企业")["canonical"] != chat_views.resolve_task("找前5家企业")["canonical"]
    for text in ["只看深圳", "找不在广东的企业", "去年广东企业", "2026年企业", "给我推荐企业", "适合的企业", "企业至少5家", "找广东广西企业", "找价格最低的企业", "找前21家企业", "排除深圳企业", "找广东企业前三"]:
        assert chat_views.resolve_task(text) is None, text


def test_compound_task_has_only_fully_recognized_steps():
    task = chat_views.resolve_task("介绍一下站主并展示项目")
    assert task["intent"] == "composite" and len(task["steps"]) == 2
    assert task["standalone"]
    assert chat_views.resolve_task("介绍一下站主并查去年收入") is None


def test_context_is_server_owned_and_continuous(views):
    args = {"province": "北京", "query": "软件"}
    result = chat_tools.execute_tool("search_companies", args)
    chat_views.record_tool_context("visitor", "conversation", "search_companies", args, result)
    context = chat_views.get_context("visitor", "conversation")
    assert not chat_views.get_context("other", "conversation")
    shenzhen = chat_views.resolve_task("只看深圳", context)
    next_args = shenzhen["steps"][0]["arguments"]
    assert not shenzhen["standalone"] and next_args["city"] == "深圳"
    assert next_args["province"] == "" and next_args["query"] == "软件"
    compare = chat_views.resolve_task("再比较岗位", {"intent": "companies", "arguments": next_args})
    assert compare["steps"][0]["arguments"]["city"] == "深圳"
    assert compare["steps"][0]["arguments"]["sort"] == "jobs_desc"
    assert compare["steps"][0]["name"] == "search_companies"


def test_cached_blocks_receive_new_owned_ids_and_one_control_bar(views):
    result = chat_tools.execute_tool("search_companies", {})
    first = chat_views.prepare_blocks(result["blocks"], "a", "one", "r1")
    second = chat_views.prepare_blocks(result["blocks"], "b", "two", "r2")
    assert first[0]["view_id"] != second[0]["view_id"]
    assert len({block["view_id"] for block in first}) == 1
    assert sum("controls" in block for block in first) == 1
    assert not any("_query" in block for block in first)
    assert _table(first)["pagination"]["total"] == 5
    with pytest.raises(HTTPException) as exc:
        _action(first, "refresh", visitor="b")
    assert exc.value.status_code == 404


def test_filter_page_sort_use_whole_database_and_stale_revision_rejected(views):
    blocks, _ = _create(args={"limit": 1})
    assert _table(blocks)["rows"][0]["name"] == "企业1"
    sorted_result = _action(blocks, "sort", "jobs_desc")
    sorted_blocks = sorted_result["blocks"]
    assert _table(sorted_blocks)["rows"][0]["name"] == "企业3"
    chart = next(block for block in sorted_blocks if block["type"] == "chart")
    assert chart["rows"][0]["value"] == _table(sorted_blocks)["rows"][0]["active_jobs"]
    with pytest.raises(HTTPException) as exc:
        _action(blocks, "page", 2)
    assert exc.value.status_code == 409
    page2 = _action(sorted_blocks, "page", 2)["blocks"]
    assert _table(page2)["rows"][0]["name"] == "企业1"
    filtered = _action(page2, "filter", {"province": "上海"})["blocks"]
    assert _table(filtered)["pagination"]["total"] == 1
    assert _table(filtered)["pagination"]["page"] == 1
    assert chat_views.get_context("visitor", "conversation")["arguments"]["province"] == "上海"


def test_latest_view_restores_current_revision_query_and_permissions(views):
    blocks, _ = _create(args={"limit": 1})
    filtered = _action(blocks, "filter", {"province": "上海"})
    latest = chat_views.get_view(blocks[0]["view_id"], "visitor")
    assert latest["revision"] == filtered["revision"] == 2
    assert _table(latest["blocks"])["rows"][0]["name"] == "企业3"
    assert latest["blocks"][0]["query_state"]["province"] == "上海"
    with pytest.raises(HTTPException) as exc:
        chat_views.get_view(blocks[0]["view_id"], "other")
    assert exc.value.status_code == 404
    content_blocks, _ = _create("get_public_content", {"content_id": "allowed"})
    with sqlite3.connect(views[0]) as conn:
        conn.execute("UPDATE contents SET draft_json=json_set(draft_json,'$.rag_enabled',0) WHERE id='allowed'")
    with pytest.raises(HTTPException) as exc:
        chat_views.get_view(content_blocks[0]["view_id"], "visitor")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("action,value", [("filter", {"sql": "DROP TABLE company"}), ("filter", {"has_jobs": "yes"}), ("sort", "name; DROP TABLE company"), ("page", 0), ("limit", 21), ("limit", True)])
def test_actions_validate_registered_fields_and_types(views, action, value):
    blocks, _ = _create()
    with pytest.raises(HTTPException) as exc:
        _action(blocks, action, value)
    assert exc.value.status_code == 400


def test_export_same_filtered_dataset_sorted_beyond_current_page(views):
    blocks, _ = _create(args={"province": "北京", "limit": 1, "sort": "jobs_desc"})
    download = _action(blocks, "export")["download"]
    assert download["total"] == 2 and not download["truncated"]
    assert [row["name"] for row in download["rows"]] == ["企业1", "企业2"]
    assert download["columns"] == _table(blocks)["columns"]
    assert {row["province"] for row in download["rows"]} == {"北京"}
    assert _table(blocks)["rows"][0]["name"] == download["rows"][0]["name"]


def test_aggregate_chart_table_export_have_identical_rows(views):
    blocks, _ = _create("aggregate_companies", {"group_by": "industry", "metric": "active_jobs"})
    chart = next(block for block in blocks if block["type"] == "chart")
    download = _action(blocks, "export")["download"]
    assert chart["rows"] == _table(blocks)["rows"] == download["rows"]


def test_expired_view_and_csv_formula_boundary(views):
    with sqlite3.connect(views[1]) as conn:
        conn.execute("UPDATE company SET name=' =HYPERLINK(\"https://example.com\")' WHERE id=1")
    blocks, _ = _create()
    download = _action(blocks, "export")["download"]
    assert any(row["name"].startswith("' =HYPERLINK") for row in download["rows"])
    with sqlite3.connect(views[0]) as conn:
        conn.execute("UPDATE chat_views SET expires_at=0")
    with pytest.raises(HTTPException) as exc:
        _action(blocks, "refresh")
    assert exc.value.status_code == 404


def test_public_non_rag_direct_read_cannot_be_used_by_model_and_revokes_immediately(views):
    direct = chat_views._read_entity("content", "not-rag")
    assert direct["blocks"][0]["markdown"] == "已发布正文 not-rag"
    assert "未发布的秘密草稿" not in json.dumps(direct, ensure_ascii=False)
    assert chat_tools.execute_tool("get_public_content", {"content_id": "not-rag"})["error"]
    for block in direct["blocks"]:
        block["_query"] = {"name": "__read", "arguments": {"kind": "content", "id": "not-rag"}}
    blocks = chat_views.prepare_blocks(direct["blocks"], "visitor", "conversation", "r")
    with sqlite3.connect(views[0]) as conn:
        conn.execute("UPDATE contents SET draft_json=json_set(draft_json,'$.visibility','private') WHERE id='not-rag'")
    with pytest.raises(HTTPException) as exc:
        _action(blocks, "refresh")
    assert exc.value.status_code == 404


def test_tool_content_view_observes_rag_revocation(views):
    blocks, _ = _create("get_public_content", {"content_id": "allowed"})
    with sqlite3.connect(views[0]) as conn:
        conn.execute("UPDATE contents SET draft_json=json_set(draft_json,'$.rag_enabled',0) WHERE id='allowed'")
    with pytest.raises(HTTPException) as exc:
        _action(blocks, "refresh")
    assert exc.value.status_code == 404


def test_read_project_uses_published_public_allowlist(views):
    blocks, _ = _create("get_public_profile", {"section": "projects"})
    read = _action(blocks, "read", {"kind": "project", "id": "one"})
    assert read["blocks"][0]["title"] == "真实项目"
    assert "hidden-" not in json.dumps(read)
    assert chat_views.get_context("visitor", "conversation")["selected_entity"] == {"kind": "project", "id": "one"}


def test_refresh_observes_new_snapshot_without_inventing_freshness(views):
    blocks, _ = _create()
    with sqlite3.connect(views[1]) as conn:
        conn.execute("UPDATE meta SET value='2026-10-28T00:00:00Z' WHERE key='built_at'")
        conn.execute("UPDATE company SET active_jobs=99 WHERE id=1")
    fresh = _action(blocks, "refresh")["blocks"]
    assert fresh[0]["updated_at"] == "2026-10-28T00:00:00Z"
    assert _table(fresh)["rows"][0]["active_jobs"] == 99
    unchanged = _action(fresh, "refresh")["blocks"]
    assert unchanged[0]["updated_at"] == fresh[0]["updated_at"]


def test_selected_company_detail_rule_keeps_current_filters_and_source(views):
    args = {"province": "北京", "limit": 1}
    blocks, result = _create(args=args)
    chat_views.record_tool_context("visitor", "conversation", "search_companies", args, result)
    detail = _action(blocks, "select_entity", {"kind": "company", "id": "1"})
    assert detail["blocks"][0]["title"] == "企业1"
    context = chat_views.get_context("visitor", "conversation")
    assert context["arguments"]["province"] == "北京"
    assert context["selected_entity"] == {"kind": "company", "id": "1"}
    assert context["selected_data_time"] == views[2]
    for message in ["这家公司", "查看这家公司", "这家公司详情"]:
        task = chat_views.resolve_task(message, context)
        assert task["steps"][0] == {"name": "get_company", "arguments": {"company_id": "1"}}
        assert not task["standalone"]
    assert chat_views.resolve_task("这家公司") is None
    assert chat_views.resolve_task("这家公司岗位有哪些", context) is None
    assert chat_views.resolve_task("岗位有哪些", context) is None
    tool_result = chat_tools.execute_tool("get_company", {"company_id": "2"})
    chat_views.record_tool_context("visitor", "conversation", "get_company", {"company_id": "2"}, tool_result)
    context = chat_views.get_context("visitor", "conversation")
    assert context["selected_entity"]["id"] == "2" and context["arguments"]["province"] == "北京"


def test_rule_answer_numbers_come_from_actual_result(views):
    task = chat_views.resolve_task("查看北京企业")
    result = chat_tools.execute_tool(task["steps"][0]["name"], task["steps"][0]["arguments"])
    answer = chat_views.rule_answer(task, [result])
    assert "2 家企业" in answer["answer"]
    assert views[2] in answer["answer"]
    assert len(answer["suggestions"]) == 3


def test_http_actions_enforce_origin_ownership_and_request_schema(views, monkeypatch):
    from lyph import chat
    from lyph.config import settings
    blocks, _ = _create()
    monkeypatch.setattr(chat, "visitor_for", lambda request: {"id": request.cookies.get("identity", "other")})
    monkeypatch.setattr(chat, "rate_limit", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(chat_views.router)
    with TestClient(app) as client:
        path = "/api/v1/chat/views/" + blocks[0]["view_id"] + "/actions"
        body = {"action": "refresh", "revision": 1}
        assert client.post(path, json=body).status_code == 403
        headers = {"origin": settings.frontend_origin, "cookie": "identity=other"}
        assert client.post(path, json=body, headers=headers).status_code == 404
        headers["cookie"] = "identity=visitor"
        assert client.post(path, json={"action": "refresh"}, headers=headers).status_code == 422
        assert client.post(path, json={"action": "sql", "revision": 1}, headers=headers).status_code == 422
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200 and response.json()["revision"] == 2
        restored = client.get("/api/v1/chat/views/" + blocks[0]["view_id"], headers=headers)
        assert restored.status_code == 200 and restored.json()["revision"] == 2
        assert client.get("/api/v1/chat/views/" + blocks[0]["view_id"], headers={"cookie": "identity=other"}).status_code == 404
        direct = client.get("/api/v1/chat/entities?kind=content&id=not-rag", headers=headers)
        assert direct.status_code == 200 and direct.json()["blocks"][0]["markdown"] == "已发布正文 not-rag"
        assert client.get("/api/v1/chat/entities?kind=company&id=1&date=2026-10-25", headers=headers).status_code == 400
        assert client.get("/api/v1/chat/entities?kind=content&date=2026-10-25", headers=headers).status_code == 404
        with sqlite3.connect(views[0]) as conn:
            conn.execute("UPDATE contents SET published_json=json_set(published_json,'$.type','automatic') WHERE id='not-rag'")
        daily = client.get("/api/v1/chat/entities?kind=content&date=2026-10-25", headers=headers)
        assert daily.status_code == 200 and daily.json()["blocks"][0]["entity"]["id"] == "not-rag"
