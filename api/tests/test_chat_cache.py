from contextlib import closing
import asyncio
import json

import pytest

from lyph import chat_cache, chat_views, db, models_routes
from lyph.config import settings
from test_intelligence import service, owner_headers, save_model, visitor  # noqa: F401


@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "cache-unit.sqlite")
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(tmp_path / "enterprise.sqlite"))
    db.init_schema()
    chat_cache.init_schema()
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE site_settings(id INTEGER,revision INTEGER,payload_json TEXT);
        CREATE TABLE contents(id TEXT,draft_json TEXT,published_json TEXT,published_version TEXT);
        CREATE TABLE knowledge_sources(id TEXT,content_id TEXT,visibility TEXT,rag_enabled INTEGER,revision INTEGER,active_version TEXT,status TEXT);
        INSERT INTO site_settings VALUES(1,1,'{}');
        INSERT INTO contents VALUES('content','{}','{"title":"old"}','v1');
        INSERT INTO knowledge_sources VALUES('source','content','public',1,1,'index-v1','ready');
        """)
    return {"id": "model", "version": 1, "model": "test", "prompt": ""}


def key(config, question="有哪些项目", visitor_id="a", history=None, context=None):
    return chat_cache.build_key(visitor_id, question, history or [], config,
                                task=chat_views.resolve_task(question, context), context=context)


def lookup(info, config):
    return asyncio.run(chat_cache.lookup(info, config))


PAYLOAD = {"answer": "公开项目", "citations": [], "blocks": [{"type": "table", "view_id": "old-view", "_query": {"name": "get_public_profile", "arguments": {"section": "projects"}}}],
           "suggestions": ["技术栈是什么"], "usage": {"total_tokens": 123}}


def test_exact_cache_is_scoped_and_invalidates_on_data_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "cache-test.sqlite")
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(tmp_path / "missing.sqlite"))
    db.init_schema()
    chat_cache.init_schema()
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE site_settings(id INTEGER,revision INTEGER,payload_json TEXT);
        CREATE TABLE contents(id TEXT,draft_json TEXT,published_json TEXT,published_version TEXT);
        CREATE TABLE knowledge_sources(id TEXT,content_id TEXT,visibility TEXT,rag_enabled INTEGER,revision INTEGER,active_version TEXT,status TEXT);
        INSERT INTO site_settings VALUES(1,1,'{}');
        """)
    config = {"id": "model", "version": 1, "model": "test", "prompt": ""}
    fingerprint = chat_cache.fingerprint(config)
    key = chat_cache.cache_key("visitor-a", "有哪些项目？", [], fingerprint)
    payload = {"answer": "项目资料", "citations": [], "blocks": [], "suggestions": []}
    chat_cache.put(key, payload)
    assert chat_cache.get(key)["answer"] == "项目资料"
    assert chat_cache.get(chat_cache.cache_key("visitor-b", "有哪些项目？", [], fingerprint)) is None
    assert chat_cache.get(chat_cache.cache_key("visitor-a", "有哪些项目？", [["之前", "回答"]], fingerprint)) is None
    with closing(db.connect()) as conn:
        conn.execute("UPDATE site_settings SET payload_json=?", ('{"changed":true}',))
    assert chat_cache.fingerprint(config) != fingerprint


def test_cache_hit_still_consumes_authorized_request(service, monkeypatch, tmp_path):
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(tmp_path / "missing.sqlite"))
    with closing(db.connect()) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS site_settings(id INTEGER,revision INTEGER,payload_json TEXT,updated_at TEXT)")
        conn.execute("INSERT INTO site_settings VALUES(1,1,'{}',?)", (db.now(),))
    headers = owner_headers(service)
    save_model(service, headers)
    visitor(service)
    grant = service.post("/api/v1/admin/grants", headers=headers, json={"mode": "count", "limit": 2}).json()
    origin = {"Origin": settings.frontend_origin}
    first = {"request_id": "cache-a", "conversation_id": "conversation-a", "message": "有哪些项目？"}
    assert "event: done" in service.post("/api/v1/chat/requests", json=first, headers=origin).text
    assert service.post("/api/v1/chat/authorize", json={"password": grant["password"]}, headers=origin).status_code == 200
    before = len(service.provider.calls)
    second = first | {"request_id": "cache-b", "conversation_id": "conversation-b"}
    assert "event: done" in service.post("/api/v1/chat/requests", json=second, headers=origin).text
    result = service.get("/api/v1/chat/requests/cache-b").json()
    assert result["cache"]["type"] == "exact"
    assert len(service.provider.calls) == before
    assert service.get("/api/v1/chat/access").json()["remaining"] == 1


def test_public_exact_and_normalized_cache_share_without_usage_or_view_credentials(cache_db):
    first = key(cache_db)
    chat_cache.store(first, PAYLOAD, cache_db)
    same = lookup(key(cache_db, visitor_id="b"), cache_db)
    assert same["type"] == "exact"
    alternate = lookup(key(cache_db, "看看项目", visitor_id="c"), cache_db)
    assert alternate["type"] == "normalized"
    assert alternate["payload"]["suggestions"] == PAYLOAD["suggestions"]
    assert "usage" not in alternate["payload"]
    assert "view_id" not in alternate["payload"]["blocks"][0]
    assert alternate["payload"]["blocks"][0]["_query"]


def test_unknown_personal_and_contextual_questions_are_isolated(cache_db):
    first = key(cache_db, "适合我的企业")
    assert not first["public"]
    chat_cache.store(first, PAYLOAD, cache_db)
    assert lookup(key(cache_db, "适合我的企业", visitor_id="b"), cache_db) is None
    assert lookup(key(cache_db, "适合我的企业", history=[["previous", "reply"]]), cache_db) is None
    context = {"intent": "companies", "arguments": {"province": "广东"}}
    other_context = {"intent": "companies", "arguments": {"province": "广西"}}
    contextual = key(cache_db, "只看深圳", context=context)
    assert contextual["public"] is False
    chat_cache.store(contextual, PAYLOAD, cache_db)
    assert lookup(key(cache_db, "这些企业只看深圳", context=context), cache_db)["type"] == "normalized"
    assert lookup(key(cache_db, "这些企业只看深圳", visitor_id="b", context=context), cache_db) is None
    assert lookup(key(cache_db, "只看深圳", context=other_context), cache_db) is None


@pytest.mark.parametrize("question", ["广西企业", "广东前5家企业", "今年广东企业", "广东不招聘企业", "广东正在招聘企业", "广东企业只显示图片", "适合我的广东企业"])
def test_query_slots_and_unrecognized_constraints_never_reuse(cache_db, question):
    chat_cache.store(key(cache_db, "广东前3家企业"), PAYLOAD, cache_db)
    assert lookup(key(cache_db, question), cache_db) is None


@pytest.mark.parametrize("statement", [
    "UPDATE site_settings SET payload_json='changed'",
    "UPDATE contents SET published_json='new content'",
    "UPDATE contents SET published_json=NULL",
    "UPDATE knowledge_sources SET rag_enabled=0",
    "UPDATE knowledge_sources SET visibility='private'",
    "UPDATE knowledge_sources SET active_version='index-v2'",
    "DELETE FROM knowledge_sources",
])
def test_hit_and_write_recheck_actual_source_permissions_and_versions(cache_db, statement):
    before = key(cache_db)
    chat_cache.store(before, PAYLOAD, cache_db)
    with closing(db.connect()) as conn:
        conn.execute(statement)
    assert lookup(before, cache_db) is None
    assert lookup(key(cache_db), cache_db) is None
    assert chat_cache.store(before, PAYLOAD, cache_db) is None


def test_snapshot_contents_and_model_prompt_changes_invalidate(cache_db, tmp_path):
    path = tmp_path / "enterprise.sqlite"
    path.write_bytes(b"old snapshot")
    before = key(cache_db)
    chat_cache.store(before, PAYLOAD, cache_db)
    path.write_bytes(b"new snapshot")
    assert lookup(before, cache_db) is None
    assert key(cache_db)["fingerprint"] != before["fingerprint"]
    assert key(cache_db | {"version": 2})["fingerprint"] != key(cache_db)["fingerprint"]
    assert key(cache_db | {"prompt": "new prompt"})["fingerprint"] != key(cache_db)["fingerprint"]


def test_embedding_configuration_change_invalidates_answer(cache_db):
    with closing(db.connect()) as conn:
        conn.executescript("""CREATE TABLE model_configs(id TEXT,purpose TEXT,base_url TEXT,model TEXT,enabled INTEGER,timeout REAL,prompt TEXT,version INTEGER);
            INSERT INTO model_configs VALUES('embedding','embedding','https://example.test','embed',1,30,'',1);""")
    info = key(cache_db)
    source = chat_cache.source_fingerprint()
    chat_cache.store(info, PAYLOAD, cache_db)
    with closing(db.connect()) as conn:
        conn.execute("UPDATE model_configs SET version=2")
    assert lookup(info, cache_db) is None
    assert lookup(key(cache_db), cache_db) is None
    assert chat_cache.source_fingerprint() == source
    with closing(db.connect()) as conn:
        conn.execute("UPDATE site_settings SET payload_json='new source'")
    assert chat_cache.source_fingerprint() != source


def test_hit_does_not_extend_ttl_expiry_and_capacity_are_enforced(cache_db):
    info = key(cache_db)
    chat_cache.store(info, PAYLOAD, cache_db)
    first = lookup(info, cache_db)
    assert lookup(info, cache_db)["expires_at"] == first["expires_at"]
    with closing(db.connect()) as conn:
        conn.execute("UPDATE chat_answer_cache SET expires_at='2000-01-01T00:00:00.000+00:00'")
    assert lookup(info, cache_db) is None
    assert chat_cache.cleanup() == 1
    chat_cache.configure(chat_cache.CacheSettings(max_entries=1, max_bytes=1024))
    chat_cache.store(info, PAYLOAD, cache_db)
    chat_cache.store(key(cache_db, "技术栈"), PAYLOAD, cache_db)
    assert lookup(info, cache_db) is None
    assert chat_cache.stats()["entries"] == 1
    assert chat_cache.store(key(cache_db, "个人资料"), {"answer": "a" * 2000}, cache_db) is None


def test_cache_disabled_and_storage_failure_fall_back_to_fresh(cache_db, monkeypatch):
    info = key(cache_db)
    chat_cache.store(info, PAYLOAD, cache_db)
    chat_cache.configure(chat_cache.CacheSettings(enabled=False))
    assert lookup(info, cache_db) is None
    assert chat_cache.store(info, PAYLOAD, cache_db) is None
    def fail():
        raise OSError("cache storage unavailable")
    monkeypatch.setattr(chat_cache, "settings", fail)
    assert lookup(info, cache_db) is None
    assert chat_cache.store(info, PAYLOAD, cache_db) is None


def test_cache_migration_restart_retains_completed_entries(cache_db):
    info = key(cache_db)
    chat_cache.store(info, PAYLOAD, cache_db)
    chat_cache.init_schema()
    assert lookup(info, cache_db)["type"] == "exact"


def test_actual_model_usage_and_cache_estimates_are_distinct(cache_db):
    with closing(db.connect()) as conn:
        conn.executescript("""CREATE TABLE model_calls(status TEXT,usage_json TEXT);
            INSERT INTO model_calls VALUES('completed','{"total_tokens":17}');
            INSERT INTO model_calls VALUES('failed',NULL);
            INSERT INTO model_calls VALUES('completed','{"total_tokens":"unknown"}');""")
    info = key(cache_db)
    chat_cache.store(info, PAYLOAD, cache_db)
    assert lookup(info, cache_db)
    assert lookup(key(cache_db, "看看项目"), cache_db)
    result = chat_cache.stats()
    assert result["actual_model_usage"]["calls"] == 3
    assert result["actual_model_usage"]["known_tokens"] == 17
    assert result["actual_model_usage"]["unknown_usage_calls"] == 2
    assert result["estimates"] == {"estimated_saved_tokens": None, "estimated_avoided_generations": 2}


def test_selected_company_is_always_visitor_scoped(cache_db):
    context = {"intent": "companies", "selected_entity": {"kind": "company", "id": "company-a"}}
    task = {"intent": "company", "standalone": False, "canonical": "company-a",
            "steps": [{"name": "get_company", "arguments": {"company_id": "company-a"}}],
            "slots": {"tool": "get_company", "company_id": "company-a"}}
    first = chat_cache.build_key("a", "这家公司", [], cache_db, task, context)
    assert first["structured"] and not first["public"]
    chat_cache.store(first, PAYLOAD, cache_db)
    assert lookup(first, cache_db)
    other = chat_cache.build_key("b", "这家公司", [], cache_db, task, context)
    assert lookup(other, cache_db) is None
    # Even a mistaken standalone flag cannot promote detail queries to shared scope.
    assert not chat_cache.build_key("a", "这家公司", [], cache_db, task | {"standalone": True}, context)["public"]


def setup_service_cache(service):
    service.app_fixture.include_router(chat_cache.router)
    with closing(db.connect()) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS site_settings(id INTEGER,revision INTEGER,payload_json TEXT)")
        conn.execute("INSERT INTO site_settings VALUES(1,1,'{}')")


def test_admin_cache_requires_owner_csrf_and_hides_question_text(service):
    setup_service_cache(service)
    assert service.get("/api/v1/admin/chat-cache").status_code == 401
    headers = owner_headers(service)
    assert service.delete("/api/v1/admin/chat-cache").status_code == 403
    cfg = {"id": "model", "version": 1, "model": "test", "prompt": ""}
    entry_id = chat_cache.store(key(cfg, "私人的问题"), PAYLOAD, cfg)
    entries = service.get("/api/v1/admin/chat-cache/entries")
    assert "私人的问题" not in entries.text and "公开项目" not in entries.text
    assert entries.json()["items"][0]["entry_id"] == entry_id
    body = chat_cache.DEFAULTS | {"semantic_enabled": True}
    assert service.put("/api/v1/admin/chat-cache/config", json=body, headers=headers).status_code == 422
    assert service.delete(f"/api/v1/admin/chat-cache/entries/{entry_id}", headers=headers).status_code == 200
    assert service.delete("/api/v1/admin/chat-cache", headers=headers).json()["deleted"] == 0


def test_semantic_calibration_real_sqlite_vec_and_embedding_usage_are_isolated(service):
    setup_service_cache(service)
    headers = owner_headers(service)
    save_model(service, headers, "embedding")
    cfg = {"id": "model", "version": 1, "model": "test", "prompt": ""}
    result = service.post("/api/v1/admin/chat-cache/calibrate", headers=headers)
    assert result.status_code == 200 and result.json()["passed"], result.text
    assert result.json()["cases"] >= 10
    body = chat_cache.DEFAULTS | {"semantic_enabled": True}
    assert service.put("/api/v1/admin/chat-cache/config", json=body, headers=headers).status_code == 200
    initial = key(cfg)
    assert lookup(initial, cfg) is None
    assert initial.get("vector")
    chat_cache.store(initial, PAYLOAD, cfg)
    before = len(service.provider.calls)
    assert lookup(key(cfg), cfg)["type"] == "exact"
    assert lookup(key(cfg, "看看项目"), cfg)["type"] == "normalized"
    assert len(service.provider.calls) == before
    # Exercise vector search after canonical misses, with identical server slots.
    semantic = initial | {"key": "semantic-query", "exact_key": "semantic-query", "normalized_key": None, "question": "公开作品"}
    assert lookup(semantic, cfg)["type"] == "semantic"
    assert len(service.provider.calls) == before + 1
    assert lookup(key(cfg, "技术栈"), cfg) is None  # Same vector, distinct slots.
    with closing(db.connect()) as conn:
        calls = conn.execute("SELECT purpose,status,usage_json FROM model_calls").fetchall()
    assert calls and all(row["purpose"] == "embedding" and row["status"] == "completed" for row in calls)
    assert all(json.loads(row["usage_json"])["total_tokens"] == 3 for row in calls)
    assert "usage" not in lookup(key(cfg), cfg)["payload"]
    with closing(db.connect()) as conn:
        conn.execute("UPDATE model_configs SET version=version+1 WHERE purpose='embedding'")
    before = len(service.provider.calls)
    assert lookup(semantic, cfg) is None
    assert len(service.provider.calls) == before


def test_semantic_ties_reject_even_with_high_similarity(service):
    setup_service_cache(service)
    headers = owner_headers(service)
    save_model(service, headers, "embedding")
    assert service.post("/api/v1/admin/chat-cache/calibrate", headers=headers).json()["passed"]
    service.put("/api/v1/admin/chat-cache/config", json=chat_cache.DEFAULTS | {"semantic_enabled": True}, headers=headers)
    cfg = {"id": "model", "version": 1, "model": "test", "prompt": ""}
    first = key(cfg)
    assert lookup(first, cfg) is None
    chat_cache.store(first, PAYLOAD, cfg)
    second = first | {"key": "second", "exact_key": "second", "normalized_key": "second"}
    chat_cache.store(second, PAYLOAD, cfg)
    query = first | {"key": "third", "exact_key": "third", "normalized_key": None}
    assert lookup(query, cfg) is None
