"""Backup tests use an explicit private-object SDK adapter and isolated SQLite."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tarfile

import pytest
from cryptography.exceptions import InvalidTag

from lyph import chat, content, db, devices, maintenance, security, storage
from lyph.config import settings
from test_intelligence import service, owner_headers, visitor


@pytest.fixture
def backup_service(service, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.tsx").write_text("export default function Page(){return 'saved source'}", encoding="utf-8")
    (source / ".env").write_text("SENSITIVE_TEST_CONFIG=must-not-back-up", encoding="utf-8")
    monkeypatch.setattr(settings, "source_dir", source)
    monkeypatch.setenv("LYPH_UI_DIR", str(tmp_path / "ui"))
    maintenance.init_schema()
    service.app_fixture.include_router(maintenance.router)
    monkeypatch.setenv("LYPH_RESTORE_OFFLINE", "1")
    return service


def test_backups_require_owner_and_restore_offline(backup_service, monkeypatch):
    assert backup_service.get("/api/v1/admin/backups").status_code == 401
    assert backup_service.post("/api/v1/admin/backups", headers={"Origin": settings.frontend_origin}).status_code == 401
    monkeypatch.delenv("LYPH_RESTORE_OFFLINE")
    with pytest.raises(RuntimeError, match="Stop the API"):
        maintenance.restore_backup(Path("nonexistent.lyph.enc"))


def test_backup_restores_wal_content_referenced_objects_source_and_invalidates_authorization(backup_service):
    headers = owner_headers(backup_service)
    guest = visitor(backup_service)
    grant = backup_service.post("/api/v1/admin/grants", json={"mode": "count", "limit": 5}, headers=headers).json()
    assert backup_service.post("/api/v1/chat/authorize", json={"password": grant["password"]}, headers={"Origin": settings.frontend_origin}).status_code == 200
    device = backup_service.post("/api/v1/admin/devices", json={"name": "test device", "platform": "windows"}, headers=headers).json()
    data = b"private backup object"
    media = storage.put(data, "attachment.txt", "text/plain")
    original = content.create_draft({"title": "WAL snapshot", "markdown": "published body", "visibility": "public", "media_ids": [media["id"]]})
    original = content.publish_content(original["id"], original["revision"])
    result = maintenance.backup()
    encrypted = settings.data_dir / "backups" / result["filename"]
    assert encrypted.is_file() and result["object_count"] == 1
    assert b"private backup object" not in encrypted.read_bytes()
    # Simulate application/object loss after the consistent SQLite backup.
    storage._adapter.objects.clear()
    with db.transaction() as conn:
        conn.execute("DELETE FROM content_versions")
        conn.execute("DELETE FROM contents")
        conn.execute("DELETE FROM file_references")
        conn.execute("DELETE FROM files")
    recovered = maintenance.restore_backup(encrypted)
    assert recovered["restored"] and recovered["authorizations_invalidated"]
    assert content.get_content(original["id"])["published_snapshot"]["markdown"] == "published body"
    assert storage.read(media["id"]) == data
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM owner_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT revoked,reserved FROM chat_grants WHERE id=?", (grant["id"],)).fetchone()[:] == (1, 0)
        assert conn.execute("SELECT free_used,free_reserved,grant_id FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (1, 0, None)
        assert conn.execute("SELECT revoked FROM devices WHERE id=?", (device["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM file_references WHERE file_id=?", (media["id"],)).fetchone()[0] >= 2
    assert backup_service.get("/api/v1/auth/session").json()["authenticated"] is False
    assert backup_service.get("/api/v1/device/config", headers={"Authorization": "Bearer " + device["token"]}).status_code == 403
    copies = list((settings.data_dir / "recovered-source").glob("*/page.tsx"))
    assert copies and "saved source" in copies[0].read_text("utf-8")
    assert not list((settings.data_dir / "recovered-source").rglob(".env"))


def test_backup_ciphertext_tamper_rejected_before_database_mutation(backup_service):
    draft = content.create_draft({"title": "before", "markdown": "original"})
    result = maintenance.backup()
    encrypted = settings.data_dir / "backups" / result["filename"]
    tampered = settings.data_dir / "tampered.lyph.enc"
    modified = bytearray(encrypted.read_bytes())
    modified[-1] ^= 1
    tampered.write_bytes(modified)
    body = {key: draft[key] for key in content.Draft.model_fields}
    content.update_draft(draft["id"], body | {"title": "after"}, draft["revision"])
    with pytest.raises(InvalidTag):
        maintenance.restore_backup(tampered)
    assert content.get_content(draft["id"])["title"] == "after"


def test_backup_keeps_exactly_seven_completed_archives(backup_service):
    created = [maintenance.backup() for _ in range(9)]
    files = list((settings.data_dir / "backups").glob("*.lyph.enc"))
    with closing(db.connect()) as conn:
        saved = [dict(row) for row in conn.execute("SELECT * FROM backups")]
    assert len(files) == len(saved) == 7
    assert {file.name for file in files} == {row["filename"] for row in saved}
    assert not (settings.data_dir / "backups" / created[0]["filename"]).exists()
    assert not (settings.data_dir / "backups" / created[1]["filename"]).exists()
    assert not list((settings.data_dir / "backups").glob("*.partial"))


def test_backup_storage_failure_preserves_previous_backup(backup_service, monkeypatch):
    previous = maintenance.backup()
    media = storage.put(b"test object", "file.txt")
    def fail(*args, **kwargs):
        raise ConnectionError("test outage")
    monkeypatch.setattr(storage._adapter, "get_object", fail)
    with pytest.raises(ConnectionError):
        maintenance.backup()
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM backups").fetchone()[0] == 1
    assert (settings.data_dir / "backups" / previous["filename"]).exists()
    assert not list((settings.data_dir / "backups").glob("*.partial"))


def test_encrypted_archive_traversal_entry_rejected(backup_service):
    result = maintenance.backup()
    encrypted = settings.data_dir / "backups" / result["filename"]
    plain = settings.data_dir / "test-original.tar.gz"
    maintenance._decrypt(encrypted, plain)
    altered = settings.data_dir / "test-altered.tar.gz"
    with tarfile.open(plain, "r:gz") as incoming, tarfile.open(altered, "w:gz") as outgoing:
        for member in incoming:
            outgoing.addfile(member, incoming.extractfile(member))
        item = tarfile.TarInfo("../escaped.txt")
        item.size = 3
        outgoing.addfile(item, io.BytesIO(b"bad"))
    malicious = settings.data_dir / "test-malicious.lyph.enc"
    maintenance._encrypt(altered, malicious)
    with pytest.raises(ValueError, match="Unsafe archive"):
        maintenance.restore_backup(malicious)
    assert not (settings.data_dir / "escaped.txt").exists()


def test_cleanup_removes_only_expired_bodies_events_audit_and_screenshots(backup_service):
    ident = visitor(backup_service)
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with db.transaction() as conn:
        conn.execute("INSERT INTO chat_requests(id,visitor_id,conversation_id,message,answer,status,quota_kind,created_at) VALUES (?,?,?,?,?,?,?,?)", ("old", ident["id"], "c", "old question", "old answer", "completed", "free", old))
        conn.execute("INSERT INTO chat_requests(id,visitor_id,conversation_id,message,status,quota_kind,created_at) VALUES (?,?,?,?,?,?,?)", ("inflight", ident["id"], "c", "preserve in flight", "running", "free", old))
        conn.execute("INSERT INTO visit_events VALUES (?,?,?,?,?,?,?,?,?)", ("old-event", ident["id"], "s", "page_view", "/", None, "unknown", "direct", old))
        conn.execute("INSERT INTO audit_log(id,action,details_json,created_at) VALUES (?,?,?,?)", ("old-audit", "test", "{}", (datetime.now(timezone.utc) - timedelta(days=181)).isoformat()))
    result = maintenance.cleanup()
    assert result["chat_bodies"] == 1 and result["visit_events"] == 1 and result["audit"] == 1
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT message,answer FROM chat_requests WHERE id='old'").fetchone()[:] == (None, None)
        assert conn.execute("SELECT message FROM chat_requests WHERE id='inflight'").fetchone()[0] == "preserve in flight"


def test_backup_restores_ui_release_pointer_and_invalidates_candidates(backup_service):
    from lyph import tasks, ui_routes
    ui_routes.init_schema()
    release_id, published_session, candidate_session = db.uid(), db.uid(), db.uid()
    root = ui_routes._ui_root()
    release = root / "releases" / release_id
    release.mkdir(parents=True)
    (release / "server.js").write_text("saved release server", encoding="utf-8")
    (release / "page.html").write_text("saved release page", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("INSERT INTO ui_sessions(id,instruction,status,published_version,created_at,updated_at) VALUES (?,?,?,?,?,?)", (published_session, "published fixture", "published", release_id, db.now(), db.now()))
        conn.execute("INSERT INTO ui_sessions(id,instruction,status,preview_token,preview_container,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (candidate_session, "candidate fixture", "preview_ready", "test-preview-token", "test-container", db.now(), db.now()))
        conn.execute("INSERT INTO ui_releases VALUES (?,?,?,?,?)", (release_id, published_session, "baseline", "active", db.now()))
        conn.execute("INSERT INTO ui_lock VALUES (?,?)", (1, candidate_session))
    queued = tasks.enqueue("ui.preview", {"session_id": candidate_session}, "test-ui-pending")
    result = maintenance.backup()
    encrypted = settings.data_dir / "backups" / result["filename"]
    (release / "server.js").write_text("damaged release server", encoding="utf-8")
    (root / "releases" / "active.json").write_text(json.dumps({"id": "wrong"}), encoding="utf-8")
    maintenance.restore_backup(encrypted)
    assert (release / "server.js").read_text("utf-8") == "saved release server"
    assert json.loads((root / "releases" / "active.json").read_text("utf-8"))["id"] == release_id
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT status,preview_token,preview_container FROM ui_sessions WHERE id=?", (candidate_session,)).fetchone()[:] == ("failed", None, None)
        assert conn.execute("SELECT status FROM ui_sessions WHERE id=?", (published_session,)).fetchone()[0] == "published"
        assert conn.execute("SELECT count(*) FROM ui_lock").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (queued["id"],)).fetchone()[0] == "failed"


def test_backup_refuses_missing_published_ui_files(backup_service):
    from lyph import ui_routes
    ui_routes.init_schema()
    with db.transaction() as conn:
        conn.execute("INSERT INTO ui_releases VALUES (?,?,?,?,?)", (db.uid(), None, "baseline", "active", db.now()))
    with pytest.raises(RuntimeError, match="Published UI files unavailable"):
        maintenance.backup()


def test_restore_clears_view_state_and_revokes_restored_mcp_credentials(backup_service):
    from lyph import agent_protocol, chat_views
    chat_views.init_schema()
    agent_protocol.init_schema()
    guest = visitor(backup_service)
    query = {"name": "search_companies", "arguments": {}}
    chat_views.prepare_blocks([{"type": "table", "columns": [], "rows": [], "_query": query}], guest["id"], "saved", "request")
    chat_views.record_tool_context(guest["id"], "saved", "search_companies", {}, {"source": "公开快照", "updated_at": db.now()})
    credential = agent_protocol.issue(agent_protocol.CredentialInput(label="restore fixture"))
    with db.transaction() as conn:
        conn.execute("INSERT INTO mcp_calls VALUES(?,?,?,?,?)", ("recent-call", credential["id"], "search_companies", "completed", db.now()))
    result = maintenance.backup()
    recovered = maintenance.restore_backup(settings.data_dir / "backups" / result["filename"])
    assert recovered["authorizations_invalidated"]
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM chat_views").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chat_view_contexts").fetchone()[0] == 0
        assert conn.execute("SELECT revoked FROM mcp_credentials WHERE id=?", (credential["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM mcp_calls WHERE id='recent-call'").fetchone()[0] == 1


def test_cleanup_expires_views_contexts_and_mcp_logs_at_separate_boundaries(backup_service):
    from lyph import agent_protocol, chat_views
    chat_views.init_schema()
    agent_protocol.init_schema()
    guest = visitor(backup_service)
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    now = db.now()
    query = json.dumps({"name": "search_companies", "arguments": {}})
    with db.transaction() as conn:
        conn.execute("INSERT INTO chat_requests(id,visitor_id,conversation_id,message,status,quota_kind,created_at) VALUES(?,?,?,?,?,?,?)", ("context-running", guest["id"], "running", "pending", "running", "free", old))
        for identifier, expiry in [("expired-view", 0), ("active-view", datetime.now(timezone.utc).timestamp() + 3600)]:
            conn.execute("INSERT INTO chat_views VALUES(?,?,?,?,?,?,?,?)", (identifier, guest["id"], "context", "request", 1, query, 0, expiry))
        for conversation, stamp in [("old", old), ("running", old), ("recent", now)]:
            conn.execute("INSERT INTO chat_view_contexts VALUES(?,?,?,?)", (guest["id"], conversation, "{}", stamp))
        for identifier, days in [("old-mcp", 181), ("recent-mcp", 179)]:
            conn.execute("INSERT INTO mcp_calls VALUES(?,?,?,?,?)", (identifier, "credential", "search_companies", "completed", (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()))
    result = maintenance.cleanup()
    assert result["chat_views"] == result["chat_view_contexts"] == result["mcp_calls"] == 1
    with closing(db.connect()) as conn:
        assert [row[0] for row in conn.execute("SELECT id FROM chat_views")] == ["active-view"]
        assert {row[0] for row in conn.execute("SELECT conversation_id FROM chat_view_contexts")} == {"running", "recent"}
        assert [row[0] for row in conn.execute("SELECT id FROM mcp_calls")] == ["recent-mcp"]
