"""Real SQLite contracts for the public tool boundary, permissions and aggregates."""
import json
import sqlite3

import pytest

from lyph import chat_tools
from lyph.config import settings


@pytest.fixture
def data_tools(tmp_path, monkeypatch):
    site_db = tmp_path / "site.sqlite"
    enterprise_db = tmp_path / "published.sqlite"
    monkeypatch.setattr(settings, "database_path", site_db)
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(enterprise_db))
    stamp = "2026-10-25T11:50:29+08:00"
    with sqlite3.connect(site_db) as conn:
        conn.executescript("""
            CREATE TABLE site_settings(id INTEGER PRIMARY KEY,payload_json TEXT,updated_at TEXT);
            CREATE TABLE contents(id TEXT PRIMARY KEY,draft_json TEXT,published_json TEXT,updated_at TEXT);
        """)
        site = {"profile": {"name": "lyph", "bio": "公开介绍", "secret": "hidden-profile-key"},
                "projects": [{"id": "one", "title": "真实项目", "description": "可查项目", "api_key": "hidden-project-key",
                              "url": "javascript:alert(1)", "image": "https://example.com/project.png"}],
                "stack": [{"name": "FastAPI", "description": "提供接口"}], "interests": [],
                "secret": "hidden-site-key", "contact": {"owner_secret": "hidden-contact-key"}}
        conn.execute("INSERT INTO site_settings VALUES(1,?,?)", (json.dumps(site), stamp))
        for identifier, visibility, rag, published, draft_override in [
            ("allowed", "public", True, True, {}),
            ("private", "private", True, True, {}),
            ("not-rag", "public", False, True, {}),
            ("draft", "public", True, False, {}),
            ("revoked", "public", True, True, {"rag_enabled": False}),
            ("hidden-now", "public", True, True, {"visibility": "private"}),
        ]:
            payload = {"title": "共同关键词 " + identifier, "summary": "摘要", "markdown": "已发布正文 " + identifier,
                       "type": "blog", "date": "2026-10-25", "category": "技术", "visibility": visibility, "rag_enabled": rag}
            draft = dict(payload, markdown="未发布的秘密草稿", **draft_override)
            conn.execute("INSERT INTO contents VALUES(?,?,?,?)", (identifier, json.dumps(draft), json.dumps(payload) if published else None, stamp))
    with sqlite3.connect(enterprise_db) as conn:
        conn.executescript("""
            CREATE TABLE company(id INTEGER PRIMARY KEY,name TEXT,name_en TEXT,status TEXT,province TEXT,city TEXT,industry TEXT,
                                 website TEXT,description TEXT,search_text TEXT,active_jobs INTEGER,latest_round TEXT,company_type TEXT,
                                 primary_source TEXT,primary_source_url TEXT,updated_at TEXT,secret TEXT);
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        """)
        conn.execute("INSERT INTO meta VALUES('built_at',?)", (stamp,))
        for identifier, province, city, industry, jobs in [
            (1, "北京", "北京", "软件", 3), (2, "北京", "北京", "硬件", 2),
            (3, "上海", "上海", "软件", 4), (4, None, None, "软件", 0), (5, "", "", "其他", 0),
        ]:
            conn.execute("INSERT INTO company VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (identifier, f"企业{identifier}", None, "存续", province, city, industry, "https://example.com", "公开企业说明",
                          f"企业{identifier} {industry}", jobs, "A轮", "vc_portfolio", "公开来源", "https://example.com/source", stamp,
                          "do-not-expose-unknown-column"))
    return site_db, enterprise_db, stamp


def test_profile_uses_public_field_allowlist_and_safe_links(data_tools):
    result = chat_tools.execute_tool("get_public_profile", {})
    assert "error" not in result
    assert result["data"]["profile"]["name"] == "lyph"
    encoded = json.dumps(result)
    assert "hidden-" not in encoded and "javascript:" not in encoded
    project_card = result["blocks"][0]["items"][0]
    assert project_card["url"] == "/project?id=one"
    assert project_card["image"] == "https://example.com/project.png"
    assert result["updated_at"] == data_tools[2]
    assert all(block["source"] and block["updated_at"] for block in result["blocks"])


def test_content_search_and_detail_never_expose_private_draft_or_revoked(data_tools):
    result = chat_tools.execute_tool("search_public_content", {"query": "共同关键词"})
    assert result["data"]["total"] == 1
    assert [row["id"] for row in result["data"]["items"]] == ["allowed"]
    detail = chat_tools.execute_tool("get_public_content", {"content_id": "allowed"})
    assert detail["data"]["markdown"] == "已发布正文 allowed"
    assert "未发布的秘密草稿" not in json.dumps(detail, ensure_ascii=False)
    for identifier in ["private", "not-rag", "draft", "revoked", "hidden-now", "missing"]:
        denied = chat_tools.execute_tool("get_public_content", {"content_id": identifier})
        assert denied["error"]["code"] == "not_found"
        assert denied["data"] is None and denied["blocks"] == []


def test_content_revocation_is_observed_on_next_tool_call(data_tools):
    assert chat_tools.execute_tool("get_public_content", {"content_id": "allowed"})["data"]
    with sqlite3.connect(data_tools[0]) as conn:
        conn.execute("UPDATE contents SET draft_json=json_set(draft_json,'$.rag_enabled',0) WHERE id='allowed'")
    assert chat_tools.execute_tool("get_public_content", {"content_id": "allowed"})["error"]["code"] == "not_found"


def test_company_detail_is_exact_readonly_and_uses_public_allowlist(data_tools):
    result = chat_tools.execute_tool("get_company", {"company_id": "1"})
    assert result["data"]["item"]["id"] == 1
    assert result["data"]["item"]["name"] == "企业1"
    assert "do-not-expose" not in json.dumps(result)
    assert result["updated_at"] == data_tools[2]
    assert all(block["_query"] == {"name": "get_company", "arguments": {"company_id": "1"}} for block in result["blocks"])
    assert chat_tools.execute_tool("get_company", {"company_id": "999"})["error"]["code"] == "not_found"
    assert chat_tools.execute_tool("get_company", {"company_id": "1 OR 1=1"})["error"]["code"] == "not_found"
    assert chat_tools.execute_tool("get_company", {"company_id": 1})["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("tool,arguments", [
    ("search_companies", {"sql": "DROP TABLE company"}),
    ("search_companies", {"url": "http://127.0.0.1/admin"}),
    ("search_companies", {"database_path": "../private.sqlite"}),
    ("aggregate_companies", {"group_by": "province); DROP TABLE company;--"}),
    ("aggregate_companies", {"metric": "secret"}),
    ("search_companies", {"limit": 21}),
    ("search_companies", {"limit": True}),
    ("search_companies", {"has_jobs": "true"}),
    ("search_companies", {"query": "a" * 201}),
    ("search_public_content", {"type": "private"}),
])
def test_parameter_schema_rejects_model_controlled_identifiers_and_overlimits(data_tools, tool, arguments):
    result = chat_tools.execute_tool(tool, arguments)
    assert result["error"]["code"] == "invalid_arguments"


def test_search_treats_sql_and_wildcards_as_literal_text(data_tools):
    for query in ["' OR 1=1; DROP TABLE company;--", "%", "_", "https://example.com"]:
        result = chat_tools.execute_tool("search_companies", {"query": query})
        assert "error" not in result and result["data"]["total"] == 0
    for query in ["' OR 1=1;--", "%"]:
        assert chat_tools.execute_tool("search_public_content", {"query": query})["data"]["total"] == 0
    result = chat_tools.execute_tool("search_companies", {"province": "北京市", "industry": "软件", "has_jobs": True})
    assert result["data"]["total"] == 1
    assert result["data"]["items"][0]["name"] == "企业1"
    assert "secret" not in json.dumps(result)
    with sqlite3.connect(data_tools[1]) as conn:
        assert conn.execute("SELECT count(*) FROM company").fetchone()[0] == 5


def test_aggregate_numbers_and_chart_rows_come_from_real_sqlite(data_tools):
    result = chat_tools.execute_tool("aggregate_companies", {"group_by": "province"})
    assert "error" not in result
    assert {row["label"]: row["value"] for row in result["data"]["rows"]} == {"北京": 2, "上海": 1, "未知": 2}
    assert result["data"]["total"] == 5 and result["data"]["other_total"] == 0
    chart = next(block for block in result["blocks"] if block["type"] == "chart")
    assert chart["rows"] == result["data"]["rows"]
    assert chart["kind"] == "bar" and chart["updated_at"] == data_tools[2]
    limited = chat_tools.execute_tool("aggregate_companies", {"group_by": "province", "limit": 1})
    assert limited["data"]["total"] == 5
    assert limited["data"]["displayed_total"] == 2 and limited["data"]["other_total"] == 3
    jobs = chat_tools.execute_tool("aggregate_companies", {"group_by": "industry", "metric": "active_jobs", "has_jobs": True})
    assert jobs["data"]["total"] == 9
    assert {row["label"]: row["value"] for row in jobs["data"]["rows"]} == {"软件": 7, "硬件": 2}


def test_tools_cannot_write_or_create_missing_databases(data_tools, tmp_path, monkeypatch):
    with chat_tools._readonly(data_tools[1]) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM company")
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(missing))
    result = chat_tools.execute_tool("search_companies", {})
    assert result["error"]["code"] == "unavailable" and not missing.exists()
    assert str(missing) not in json.dumps(result)


def test_wrong_enterprise_schema_is_unavailable_without_exposing_internal_sql(data_tools, tmp_path, monkeypatch):
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(data_tools[0]))
    result = chat_tools.execute_tool("search_companies", {})
    assert result["error"]["code"] == "unavailable"
    assert "site_settings" not in json.dumps(result)


def test_query_deadline_interrupts_expensive_sql(data_tools, monkeypatch):
    monkeypatch.setattr(chat_tools, "QUERY_TIMEOUT_SECONDS", 0.001)
    with chat_tools._readonly(data_tools[1]) as conn:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n").fetchone()


def test_unknown_tool_and_non_object_arguments_are_safe_errors(data_tools):
    assert chat_tools.execute_tool("run_sql", {"sql": "SELECT * FROM site_settings"})["error"]["code"] == "unknown_tool"
    assert chat_tools.execute_tool("search_companies", "not an object")["error"]["code"] == "invalid_arguments"
    definitions = {tool["function"]["name"]: tool["function"]["parameters"] for tool in chat_tools.TOOL_DEFINITIONS}
    assert set(definitions) == {"get_public_profile", "search_public_content", "get_public_content", "search_companies", "aggregate_companies", "get_company"}
    assert all(schema["additionalProperties"] is False for schema in definitions.values())
