from contextlib import closing
from datetime import datetime, timedelta, timezone
import asyncio
import io
import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from lyph import auth, content, db, navigation, security, settings_routes, storage, tasks
from lyph.config import settings


class ObjectFixture:
    """Explicit SDK fixture, never selected by production configuration."""
    def __init__(self):
        self.objects = {}
    def bucket_exists(self, bucket):
        return True
    def put_object(self, bucket, key, stream, size, content_type):
        self.objects[key] = stream.read()
    def get_object(self, bucket, key):
        result = io.BytesIO(self.objects[key])
        result.release_conn = lambda: None
        return result
    def remove_object(self, bucket, key):
        self.objects.pop(key, None)


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "database_path", tmp_path / "lyph.sqlite3")
    monkeypatch.setattr(settings, "secret_key", "test-secret-key-" * 4)
    monkeypatch.setattr(settings, "owner_password_hash", security.hash_password("correct-password-test"))
    monkeypatch.setattr(settings, "secure_cookies", False)
    monkeypatch.setattr(settings, "frontend_origin", "http://localhost:3000")
    content.set_publication_callback(None)
    db.init_schema()
    for module in [auth, storage, tasks, content, settings_routes, navigation]:
        module.init_schema()
    app = FastAPI()
    for module in [auth, storage, tasks, content, settings_routes, navigation]:
        app.include_router(module.router)
        if hasattr(module, "public_router"):
            app.include_router(module.public_router)
    storage.set_test_adapter(ObjectFixture())
    with TestClient(app) as client:
        yield client
    storage.set_test_adapter(None)
    tasks._handlers.clear()


def login(client):
    response = client.post("/api/v1/auth/login", json={"password": "correct-password-test"}, headers={"Origin": settings.frontend_origin})
    assert response.status_code == 200
    return {"Origin": settings.frontend_origin, "X-CSRF-Token": response.json()["csrf_token"]}


def test_identity_origin_csrf_expiry(core):
    assert core.get("/api/v1/admin/contents").status_code == 401
    assert core.post("/api/v1/auth/login", json={"password": "correct-password-test"}).status_code == 403
    headers = login(core)
    assert core.get("/api/v1/auth/session").json()["authenticated"]
    assert core.post("/api/v1/admin/contents", json={}).status_code == 403
    assert core.post("/api/v1/admin/contents", json={}, headers=headers).status_code == 200
    with db.transaction() as conn:
        conn.execute("UPDATE owner_sessions SET last_seen_at=?", ((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(),))
    assert core.get("/api/v1/auth/session").json()["authenticated"] is False
    assert core.get("/api/v1/admin/contents").status_code == 401


def test_login_rate_limit_is_persisted(core):
    for _ in range(10):
        assert core.post("/api/v1/auth/login", json={"password": "wrong"}, headers={"Origin": settings.frontend_origin}).status_code == 401
    assert core.post("/api/v1/auth/login", json={"password": "correct-password-test"}, headers={"Origin": settings.frontend_origin}).status_code == 429


def test_private_default_snapshot_revision_and_immediate_revoke(core):
    headers = login(core)
    item = core.post("/api/v1/admin/contents", json={"title": "First", "markdown": "published body"}, headers=headers).json()
    assert item["visibility"] == "private" and item["rag_enabled"] is False
    assert core.get("/api/v1/public/contents").json()["total"] == 0
    content_id = item["id"]
    update = {k: item[k] for k in content.Draft.model_fields}
    update.update(visibility="public", revision=item["revision"])
    saved = core.put(f"/api/v1/admin/contents/{content_id}", json=update, headers=headers).json()
    published = core.post(f"/api/v1/admin/contents/{content_id}/publish", json={"revision": saved["revision"]}, headers=headers).json()
    update.update(markdown="unpublished edit", revision=published["revision"])
    edited = core.put(f"/api/v1/admin/contents/{content_id}", json=update, headers=headers).json()
    assert core.get(f"/api/v1/public/contents/{content_id}").json()["markdown"] == "published body"
    assert core.put(f"/api/v1/admin/contents/{content_id}", json=update, headers=headers).status_code == 409
    update.update(visibility="private", revision=edited["revision"])
    assert core.put(f"/api/v1/admin/contents/{content_id}", json=update, headers=headers).status_code == 200
    assert core.get(f"/api/v1/public/contents/{content_id}").status_code == 404


def test_versions_restore_only_draft_same_date_legacy_link(core):
    headers = login(core)
    first = content.create_draft({"title": "One", "markdown": "one", "date": "2026-10-04", "visibility": "public"})
    first = content.publish_content(first["id"], first["revision"])
    second = content.create_draft({"title": "Two", "markdown": "two", "date": "2026-10-04", "visibility": "public", "type": "automatic"})
    content.publish_content(second["id"], second["revision"])
    assert core.get("/api/v1/public/contents?date=2026-10-04").json()["total"] == 2
    assert core.get("/api/v1/public/contents/2026-10-04").json()["id"] in {first["id"], second["id"]}
    versions = core.get(f"/api/v1/admin/contents/{first['id']}/versions").json()["items"]
    updated = content.update_draft(first["id"], {"title": "Changed", "markdown": "changed", "visibility": "public"}, first["revision"])
    restored = core.post(f"/api/v1/admin/contents/{first['id']}/restore", json={"revision": updated["revision"], "version_id": versions[0]["id"]}, headers=headers).json()
    assert restored["markdown"] == "one"
    assert core.get(f"/api/v1/public/contents/{first['id']}").json()["markdown"] == "one"
    assert core.get("/api/v1/public/contents?type=journal").json()["total"] == 1


def test_pagination_summary_and_search(core):
    for i in range(50):
        item = content.create_draft({"title": f"Item {i}", "markdown": "needle", "visibility": "public"})
        content.publish_content(item["id"], item["revision"])
    page = core.get("/api/v1/public/contents?q=needle&page=1&batch=4").json()
    assert len(page["items"]) == 12 and page["total"] == 50
    assert page["has_more"] is False and page["has_next_page"] is True
    assert all("markdown" not in item for item in page["items"])
    assert len(core.get("/api/v1/public/contents?page=2").json()["items"]) == 2


def test_media_reference_permission_deletion_and_dedup(core):
    media = storage.put(b"png", "sample.png", "image/png")
    assert storage.put(b"png", "second.png", "image/png")["id"] == media["id"]
    item = content.create_draft({"title": "Photo", "markdown": f"![](/api/v1/public/media/{media['id']})", "visibility": "public"})
    assert core.get(f"/api/v1/public/media/{media['id']}").status_code == 404
    item = content.publish_content(item["id"], item["revision"])
    assert core.get(f"/api/v1/public/media/{media['id']}").content == b"png"
    with pytest.raises(HTTPException) as protected:
        storage.delete(media["id"])
    assert protected.value.status_code == 409
    content.withdraw_content(item["id"], item["revision"])
    assert core.get(f"/api/v1/public/media/{media['id']}").status_code == 404
    orphan = storage.put(b"unused", "unused.txt", "text/plain")
    assert storage.delete(orphan["id"])["deleted"]


def test_settings_revision_public_linkage(core):
    headers = login(core)
    current = core.get("/api/v1/admin/settings").json()
    assert current["site"]["projects"] == []
    assert [item["id"] for item in current["site"]["navigation"]] == [
        "about", "projects", "stack", "interests", "journal", "chat", "collection"
    ]
    current["site"]["profile"]["name"] = "Owner"
    assert core.put("/api/v1/admin/settings", json={"revision": current["revision"], "site": current["site"]}, headers=headers).status_code == 200
    assert core.get("/api/v1/public/site").json()["profile"]["name"] == "Owner"
    assert core.put("/api/v1/admin/settings", json={"revision": current["revision"], "site": current["site"]}, headers=headers).status_code == 409


def test_navigation_database_driven_public_filter(core):
    public = core.get("/api/v1/public/navigation").json()["items"]
    assert [item["id"] for item in public] == [
        "about", "projects", "stack", "interests", "journal", "chat", "collection"
    ]
    assert public[4]["event_name"] == "nav.journal.open"

    # 直接写库即可新增菜单，不需要改前端代码；省略的字段使用表默认值。
    with closing(db.connect()) as conn:
        conn.execute(
            "INSERT INTO navigation_items(id,label,href) VALUES(?,?,?)",
            ("blog", "博客", "/blog"),
        )
        conn.execute(
            "INSERT INTO navigation_items(id,label,href,visible,show_as_bot) VALUES(?,?,?,0,1)",
            ("draft-menu", "未公开", "/draft"),
        )
        conn.execute(
            "INSERT INTO navigation_items(id,label,href) VALUES(?,?,?)",
            ("unsafe", "危险链接", "javascript:alert(1)"),
        )
    items = core.get("/api/v1/public/navigation").json()["items"]
    assert any(item["id"] == "blog" and item["href"] == "/blog" for item in items)
    assert all(item["id"] != "draft-menu" for item in items)
    assert all(item["id"] != "unsafe" for item in items)

    headers = login(core)
    assert core.post(
        "/api/v1/admin/navigation",
        json={"id": "external", "label": "外部", "href": "https://example.com", "sort_order": 5},
        headers=headers,
    ).status_code == 200
    assert core.post(
        "/api/v1/admin/navigation",
        json={"label": "危险", "href": "javascript:alert(1)"},
        headers=headers,
    ).status_code == 422
    assert core.put(
        "/api/v1/admin/navigation/blog",
        json={"label": "博客", "href": "/blog", "show_as_bot": False, "bot_size": 1.4},
        headers=headers,
    ).status_code == 200
    assert core.delete("/api/v1/admin/navigation/blog", headers=headers).status_code == 200
    assert core.delete("/api/v1/admin/navigation/blog", headers=headers).status_code == 404

    settings = core.get("/api/v1/admin/settings").json()
    settings["site"]["navigation"] = [{"id": "replaced", "label": "替换", "href": "/replaced"}]
    assert core.put(
        "/api/v1/admin/settings",
        json={"revision": settings["revision"], "site": settings["site"]},
        headers=headers,
    ).status_code == 200
    replaced = core.get("/api/v1/public/navigation").json()["items"]
    assert [item["id"] for item in replaced] == ["replaced"]


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_navigation_edit_invalidates_legacy_settings_snapshot(core, operation):
    headers = login(core)
    stale = core.get("/api/v1/admin/settings").json()
    if operation == "create":
        response = core.post("/api/v1/admin/navigation", json={
            "id": "new-menu", "label": "新菜单", "href": "/new-menu",
        }, headers=headers)
    elif operation == "update":
        response = core.put("/api/v1/admin/navigation/about", json={
            "label": "个人介绍", "href": "/profile",
        }, headers=headers)
    else:
        response = core.delete("/api/v1/admin/navigation/about", headers=headers)
    assert response.status_code == 200
    current = core.get("/api/v1/admin/settings").json()
    assert current["revision"] == stale["revision"] + 1

    # An older compatible client sends its complete JSON, including old menus.
    assert core.put("/api/v1/admin/settings", json={
        "revision": stale["revision"], "site": stale["site"],
    }, headers=headers).status_code == 409
    assert core.get("/api/v1/admin/settings").json() == current
    items = {item["id"]: item for item in current["site"]["navigation"]}
    if operation == "create":
        assert items["new-menu"]["href"] == "/new-menu"
    elif operation == "update":
        assert items["about"]["label"] == "个人介绍" and items["about"]["href"] == "/profile"
    else:
        assert "about" not in items


def test_failed_navigation_mutation_keeps_settings_revision(core):
    headers = login(core)
    current = core.get("/api/v1/admin/settings").json()
    assert core.post("/api/v1/admin/navigation", json={
        "id": "about", "label": "重复", "href": "/duplicate",
    }, headers=headers).status_code == 409
    assert core.put("/api/v1/admin/navigation/missing", json={
        "label": "缺失", "href": "/missing",
    }, headers=headers).status_code == 404
    assert core.delete("/api/v1/admin/navigation/missing", headers=headers).status_code == 404
    assert core.get("/api/v1/admin/settings").json() == current


def test_navigation_migrates_legacy_once_and_keeps_intentionally_empty(core):
    headers = login(core)
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM site_settings WHERE id=1").fetchone()
        site = json.loads(row["payload_json"])
        site["profile"]["name"] = "原有站主"
        site["navigation"] = [{"id": "custom", "label": "原有菜单", "href": "/custom", "bot_size": 1.2}]
        conn.execute("UPDATE site_settings SET payload_json=? WHERE id=1", (json.dumps(site),))
        conn.execute("DROP TABLE navigation_items")
        conn.execute("DELETE FROM schema_migrations WHERE version='navigation-001'")
    navigation.init_schema()
    migrated = core.get("/api/v1/admin/settings").json()
    assert migrated["revision"] == row["revision"]
    assert migrated["site"]["profile"]["name"] == "原有站主"
    assert [(item["id"], item["label"], item["href"]) for item in migrated["site"]["navigation"]] == [
        ("custom", "原有菜单", "/custom")
    ]
    assert migrated["site"]["navigation"][0]["bot_size"] == 1.2
    navigation.init_schema()
    assert core.get("/api/v1/admin/settings").json() == migrated

    assert core.delete("/api/v1/admin/navigation/custom", headers=headers).status_code == 200
    emptied = core.get("/api/v1/admin/settings").json()
    navigation.init_schema()
    assert core.get("/api/v1/admin/settings").json() == emptied
    assert core.get("/api/v1/public/navigation").json()["items"] == []


def test_navigation_keeps_existing_rows_when_migration_marker_missing(core):
    with db.transaction() as conn:
        conn.execute("DELETE FROM navigation_items")
        conn.execute("INSERT INTO navigation_items(id,label,href) VALUES('manual','直接写库','/manual')")
        conn.execute("DELETE FROM schema_migrations WHERE version='navigation-001'")
    navigation.init_schema()
    assert [item["id"] for item in core.get("/api/v1/public/navigation").json()["items"]] == ["manual"]
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version='navigation-001'").fetchone()


@pytest.mark.asyncio
async def test_task_idempotency_failure_retry_crash(core):
    async def handler(payload):
        return {"value": payload["value"]}
    tasks.register("test", handler)
    first = tasks.enqueue("test", {"value": 1}, "same-key")
    assert tasks.enqueue("test", {"value": 1}, "same-key")["id"] == first["id"]
    with pytest.raises(HTTPException):
        tasks.enqueue("test", {"value": 2}, "same-key")
    await tasks.run_pending()
    assert tasks.get_task(first["id"])["result"] == {"value": 1}
    async def broken(payload):
        raise RuntimeError("secret that must not leak")
    tasks.register("broken", broken)
    failed = tasks.enqueue("broken", {}, "broken-key")
    await tasks.run_pending()
    assert tasks.get_task(failed["id"])["error"] == "RuntimeError"
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET status='running',attempts=1 WHERE id=?", (failed["id"],))
    tasks.recover()
    assert tasks.get_task(failed["id"])["status"] == "queued"


def test_short_transaction_rollback_and_secrets(core):
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO audit_log(id,action,details_json,created_at) VALUES('rolled','x','{}',?)", (db.now(),))
            raise RuntimeError("rollback")
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log WHERE id='rolled'").fetchone()[0] == 0
    value = security.encrypt("provider-secret")
    assert "provider-secret" not in value
    assert security.decrypt(value) == "provider-secret"
    assert security.verify_password(security.hash_password("a-password"), "a-password")


def test_generated_draft_origin_is_durable_unique(core):
    first = content.create_draft({"type": "automatic", "title": "Daily", "markdown": "one"}, origin_key="daily:run-1")
    second = content.create_draft({"type": "automatic", "title": "Daily", "markdown": "recovered"}, origin_key="daily:run-1")
    assert first["id"] == second["id"] and second["markdown"] == "one"


@pytest.mark.asyncio
async def test_live_preview_holds_heavy_slot_until_ui_finishes(core):
    async def handler(payload):
        return {"completed": True}
    tasks.register("parse", handler)
    tasks.register("ui.publish", handler)
    parse = tasks.enqueue("parse", {}, "parse-one")
    publish = tasks.enqueue("ui.publish", {}, "ui-one")
    with db.transaction() as conn:
        conn.execute("CREATE TABLE ui_sessions(id TEXT,status TEXT)")
        conn.execute("INSERT INTO ui_sessions VALUES('preview','preview_ready')")
    await tasks.run_pending()
    assert tasks.get_task(parse["id"])["status"] == "queued"
    assert tasks.get_task(publish["id"])["status"] == "succeeded"
    with db.transaction() as conn:
        conn.execute("UPDATE ui_sessions SET status='published'")
    await tasks.run_pending()
    assert tasks.get_task(parse["id"])["status"] == "succeeded"


def test_cli_maintenance_requires_offline_and_never_recovers_online_quota(core, monkeypatch):
    from lyph import cli, main, chat
    calls = []
    monkeypatch.setattr(main, "_modules", lambda: [])
    monkeypatch.setattr(chat, "recover_interrupted", lambda: calls.append("recover"))
    main.init_all(recover_requests=False)
    assert calls == []
    main.init_all()
    assert calls == ["recover"]
    monkeypatch.delenv("LYPH_MAINTENANCE_OFFLINE", raising=False)
    monkeypatch.setattr("sys.argv", ["lyph", "backup"])
    with pytest.raises(SystemExit, match="LYPH_MAINTENANCE_OFFLINE"):
        cli.main()


@pytest.mark.asyncio
async def test_scheduler_tick_does_not_wait_for_long_handler_and_stop_requeues(core, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def handler(payload):
        entered.set()
        await release.wait()
        return {}
    tasks.register("long", handler)
    item = tasks.enqueue("long", {}, "long-tick")
    monkeypatch.setattr(tasks, "_stopping", False)
    monkeypatch.setattr(tasks, "_lock", asyncio.Lock())
    await tasks._tick()
    await asyncio.wait_for(entered.wait(), timeout=1)
    await tasks._tick()
    assert len(tasks._active_runners) == 1
    assert tasks.get_task(item["id"])["attempts"] == 1
    await tasks.stop()
    assert not tasks._active_runners
    assert tasks.get_task(item["id"])["status"] == "queued"
    await tasks._tick()
    assert not tasks._active_runners
