"""Contract tests use an explicit SDK HTTP fixture, never a production fake provider."""
import asyncio
import base64
import io
import json
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from lyph import auth, chat, content, daily, db, devices, knowledge, models_routes, operations, security, storage, tasks
from lyph.config import settings


class ObjectFixture:
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


class ProviderFixture:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.cutoff = False
    def handle(self, request):
        body = json.loads(request.content)
        self.calls.append((str(request.url), body))
        if self.fail:
            return httpx.Response(500, json={"error": {"message": "provider-secret-do-not-log", "type": "server_error"}})
        if request.url.path.endswith("/embeddings"):
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx.Response(200, json={"object": "list", "data": [{"object": "embedding", "index": index, "embedding": [1.0, 0.5, 0.1]} for index, _ in enumerate(inputs)], "model": body["model"], "usage": {"prompt_tokens": 3, "total_tokens": 3}})
        if body.get("stream"):
            chunks = [{"id": "provider-stream", "object": "chat.completion.chunk", "created": 1, "model": body["model"], "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]} for text in ["真实", " SDK 流"]]
            if not self.cutoff:
                chunks.append({"id": "provider-stream", "object": "chat.completion.chunk", "created": 1, "model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            data = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=data.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "provider-result", "object": "chat.completion", "created": 1, "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": "已确认的日报文字"}, "finish_reason": "stop"}]})


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "database_path", tmp_path / "lyph.sqlite3")
    monkeypatch.setattr(settings, "secret_key", "intelligence-test-key-" * 3)
    monkeypatch.setattr(settings, "owner_password_hash", security.hash_password("owner-test-password"))
    monkeypatch.setattr(settings, "secure_cookies", False)
    monkeypatch.setattr(settings, "frontend_origin", "http://localhost:3000")
    monkeypatch.setenv("LYPH_CHAT_IP_LIMIT", "1000")
    content.set_publication_callback(None)
    tasks._handlers.clear()
    tasks._lock = None
    db.init_schema()
    modules = [auth, storage, tasks, content, models_routes, knowledge, chat, devices, daily, operations]
    for module in modules:
        module.init_schema()
    app = FastAPI()
    for module in modules:
        app.include_router(module.router)
        if hasattr(module, "public_router"):
            app.include_router(module.public_router)
    provider = ProviderFixture()
    models_routes.set_test_http_transport(httpx.MockTransport(provider.handle))
    storage.set_test_adapter(ObjectFixture())
    with TestClient(app) as client:
        client.provider = provider
        client.app_fixture = app
        yield client
    models_routes.set_test_http_transport(None)
    storage.set_test_adapter(None)
    content.set_publication_callback(None)
    tasks._handlers.clear()
    tasks._lock = None


def owner_headers(client):
    result = client.post("/api/v1/auth/login", json={"password": "owner-test-password"}, headers={"Origin": settings.frontend_origin})
    assert result.status_code == 200
    return {"Origin": settings.frontend_origin, "X-CSRF-Token": result.json()["csrf_token"]}


def save_model(client, headers, purpose="chat"):
    result = client.post("/api/v1/admin/models", headers=headers, json={"purpose": purpose, "base_url": "https://provider.example.test/v1", "model": purpose + "-test", "api_key": "test-secret-do-not-expose"})
    assert result.status_code == 201, result.text
    return result.json()


def visitor(client):
    assert client.get("/api/v1/chat/access").status_code == 200
    raw = client.cookies.get(chat.COOKIE)
    with closing(db.connect()) as conn:
        return dict(conn.execute("SELECT * FROM chat_visitors WHERE token_hash=?", (security.digest(raw),)).fetchone())


def test_all_owner_routes_deny_unauthed(service):
    for path in ["models", "knowledge", "grants", "devices", "daily", "analytics", "conversations", "requests", "overview", "audit"]:
        assert service.get("/api/v1/admin/" + path).status_code == 401


def test_model_secrets_versions_and_real_capability_requests(service):
    headers = owner_headers(service)
    for purpose in ["chat", "embedding", "vision", "code"]:
        model = save_model(service, headers, purpose)
        result = service.post(f"/api/v1/admin/models/{model['id']}/test", headers=headers)
        assert result.json()["status"] == "passed", result.text
        assert "test-secret" not in service.get("/api/v1/admin/models").text
        assert "test-secret" not in service.get(f"/api/v1/admin/models/{model['id']}/versions").text
    assert any(call[1].get("input") for call in service.provider.calls)
    assert any(isinstance(call[1].get("messages", [{}])[0].get("content"), list) for call in service.provider.calls)
    for _, payload in service.provider.calls:
        if "messages" in payload:
            assert payload["max_tokens"] == 1024
            assert "thinking" not in payload and "reasoning_effort" not in payload
            image = payload["messages"][0]["content"]
            if isinstance(image, list):
                assert_valid_capability_png(base64.b64decode(image[1]["image_url"]["url"].split(",", 1)[1]))
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT secret FROM model_configs").fetchall()
        assert all(b"test-secret" not in row[0] for row in rows)


@pytest.mark.parametrize("url", ["http://api.openai.com/v1", "https://127.0.0.1/v1", "https://169.254.169.254/v1", "https://[::1]/v1", "https://metadata.google.internal/v1"])
def test_production_provider_rejects_internal_targets(service, url):
    models_routes.set_test_http_transport(None)
    with pytest.raises(HTTPException) as error:
        models_routes.validate_endpoint(url)
    assert error.value.status_code == 422


def test_true_sse_success_replay_usage_unknown_and_identity(service):
    headers = owner_headers(service)
    model = save_model(service, headers)
    guest = visitor(service)
    payload = {"request_id": "request-one", "conversation_id": "conversation-one", "message": "你好"}
    result = service.post("/api/v1/chat/requests", json=payload, headers={"Origin": settings.frontend_origin})
    assert result.status_code == 200 and "event: delta" in result.text and "event: done" in result.text
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 0
    stored = service.get("/api/v1/chat/requests/request-one").json()
    assert stored["status"] == "completed" and stored["answer"] == "真实 SDK 流" and stored["usage"] is None
    calls = len(service.provider.calls)
    with db.transaction() as conn:
        conn.execute("UPDATE model_configs SET enabled=0 WHERE id=?", (model["id"],))
    replay = service.post("/api/v1/chat/requests", json=payload, headers={"Origin": settings.frontend_origin})
    assert "event: done" in replay.text and len(service.provider.calls) == calls
    with TestClient(service.app_fixture) as other:
        visitor(other)
        assert other.get("/api/v1/chat/requests/request-one").status_code == 404
        assert other.post("/api/v1/chat/requests", json=payload, headers={"Origin": settings.frontend_origin}).status_code == 404
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT free_used,free_reserved FROM chat_visitors WHERE id=?", (guest["id"],)).fetchone()[:] == (1, 0)


@pytest.mark.parametrize("mode", ["provider-error", "cutoff"])
def test_failure_releases_quota_and_does_not_leak_error(service, mode):
    headers = owner_headers(service)
    save_model(service, headers)
    visitor(service)
    service.provider.fail = mode == "provider-error"
    service.provider.cutoff = mode == "cutoff"
    payload = {"request_id": "failure-one", "conversation_id": "conversation", "message": "你好"}
    result = service.post("/api/v1/chat/requests", json=payload, headers={"Origin": settings.frontend_origin})
    assert "event: error" in result.text and "provider-secret-do-not-log" not in result.text
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1
    service.provider.fail = service.provider.cutoff = False
    retry = service.post("/api/v1/chat/requests", json=payload | {"retry": True}, headers={"Origin": settings.frontend_origin})
    assert "event: done" in retry.text
    assert service.get("/api/v1/chat/requests/failure-one").json()["attempts"] == 2


@pytest.mark.parametrize("termination", ["cancelled", "disconnected"])
def test_active_stream_termination_releases_reservation_and_closes_client(service, termination):
    from types import SimpleNamespace
    headers = owner_headers(service)
    save_model(service, headers)
    visitor(service)
    raw = service.cookies.get(chat.COOKIE)
    class RequestFixture:
        headers = {"origin": settings.frontend_origin}
        cookies = {chat.COOKIE: raw}
        client = SimpleNamespace(host="termination-test")
        disconnected = False
        async def is_disconnected(self):
            return self.disconnected
    class ClosingTransport(httpx.MockTransport):
        closed = False
        async def aclose(self):
            self.closed = True
            await super().aclose()
    transport = ClosingTransport(service.provider.handle)
    models_routes.set_test_http_transport(transport)
    async def run():
        request = RequestFixture()
        response = await chat.submit(chat.ChatInput(request_id="active-stream", conversation_id="active", message="question"), request)
        iterator = response.body_iterator
        assert "event: start" in await anext(iterator)
        assert "event: citations" in await anext(iterator)
        assert "event: delta" in await anext(iterator)
        if termination == "cancelled":
            result = service.post("/api/v1/chat/requests/active-stream/cancel", headers={"Origin": settings.frontend_origin})
            assert result.json()["cancel_requested"]
            assert "event: error" in await anext(iterator)
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
        else:
            request.disconnected = True
            with pytest.raises(asyncio.CancelledError):
                await anext(iterator)
    asyncio.run(run())
    assert service.get("/api/v1/chat/requests/active-stream").json()["status"] == termination
    assert service.get("/api/v1/chat/access").json()["free_remaining"] == 1
    assert transport.closed


def test_last_shared_quota_is_atomic_and_cancel_restart_release(service):
    headers = owner_headers(service)
    save_model(service, headers)
    grant = service.post("/api/v1/admin/grants", json={"mode": "count", "limit": 1}, headers=headers).json()
    first = visitor(service)
    assert service.post("/api/v1/chat/authorize", json={"password": grant["password"]}, headers={"Origin": settings.frontend_origin}).status_code == 200
    with TestClient(service.app_fixture) as other:
        second = visitor(other)
        assert other.post("/api/v1/chat/authorize", json={"password": grant["password"]}, headers={"Origin": settings.frontend_origin}).status_code == 200
    with db.transaction() as conn:
        conn.execute("UPDATE chat_visitors SET free_used=1")
    config = models_routes.config_for("chat")
    def attempt(pair):
        ident, request_id = pair
        try:
            row, _ = chat.reserve(chat.ChatInput(request_id=request_id, conversation_id="shared", message="question"), ident, config)
            return row
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, [(first["id"], "first"), (second["id"], "second")]))
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1 and 403 in results
    row = winners[0]
    with db.transaction() as conn:
        conn.execute("UPDATE chat_requests SET cancel_requested=1 WHERE id=?", (row["id"],))
    assert chat.settle(row["id"], "completed", "answer", [], None, None, 1, 1) == "cancelled"
    with closing(db.connect()) as conn:
        quota = conn.execute("SELECT used,reserved FROM chat_grants WHERE id=?", (grant["id"],)).fetchone()
        assert quota[:] == (0, 0)
    recovered, _ = chat.reserve(chat.ChatInput(request_id="interrupted", conversation_id="shared", message="next"), first["id"], config)
    chat.recover_interrupted()
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT status FROM chat_requests WHERE id='interrupted'").fetchone()[0] == "interrupted"
        assert conn.execute("SELECT reserved FROM chat_grants WHERE id=?", (grant["id"],)).fetchone()[0] == 0


def test_time_grant_expiration_and_inflight_revocation(service):
    headers = owner_headers(service)
    save_model(service, headers)
    grant = service.post("/api/v1/admin/grants", json={"mode": "time", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}, headers=headers).json()
    ident = visitor(service)
    assert service.post("/api/v1/chat/authorize", json={"password": grant["password"]}, headers={"Origin": settings.frontend_origin}).status_code == 200
    with db.transaction() as conn:
        conn.execute("UPDATE chat_visitors SET free_used=1")
    row, _ = chat.reserve(chat.ChatInput(request_id="inflight", conversation_id="time", message="q"), ident["id"], models_routes.config_for("chat"))
    assert service.delete(f"/api/v1/admin/grants/{grant['id']}", headers=headers).status_code == 200
    assert chat.settle(row["id"], "completed", "answer", [], None, None, 1, 1) == "completed"
    assert service.post("/api/v1/chat/authorize", json={"password": grant["password"]}, headers={"Origin": settings.frontend_origin}).status_code == 403
    invalid = service.post("/api/v1/admin/grants", json={"mode": "time", "expires_at": "2020-01-01T00:00:00Z"}, headers=headers)
    assert invalid.status_code == 422


def test_accidental_grant_secret_paste_is_redacted_from_message_and_feedback(service):
    headers = owner_headers(service)
    save_model(service, headers)
    grant = service.post("/api/v1/admin/grants", json={"mode": "count", "limit": 1}, headers=headers).json()
    visitor(service)
    result = service.post("/api/v1/chat/requests", json={"request_id": "redaction", "conversation_id": "redaction", "message": "我的授权是 " + grant["password"]}, headers={"Origin": settings.frontend_origin})
    assert "event: done" in result.text and grant["password"] not in result.text
    service.post("/api/v1/chat/requests/redaction/feedback", json={"rating": "positive", "note": grant["password"]}, headers={"Origin": settings.frontend_origin})
    detail = service.get("/api/v1/admin/requests/redaction")
    assert grant["password"] not in detail.text and "临时授权已隐藏" in detail.text
    assert grant["password"] not in json.dumps(service.provider.calls, ensure_ascii=False)


def test_native_vector_private_revoke_and_old_index_survives_failure(service):
    headers = owner_headers(service)
    model = save_model(service, headers, "embedding")
    result = service.post("/api/v1/admin/knowledge", files={"file": ("private.txt", "私密事实\n\n学习记录".encode(), "text/plain")}, headers=headers)
    assert result.status_code == 201, result.text
    source_id = result.json()["id"]
    asyncio.run(tasks.run_pending())
    ready = service.get(f"/api/v1/admin/knowledge/{source_id}").json()
    assert ready["status"] == "ready", ready
    assert asyncio.run(knowledge.retrieve("学习")) == []
    assert len(asyncio.run(knowledge.retrieve("学习", owner=True))) == 1
    assert service.put(f"/api/v1/admin/knowledge/{source_id}/permissions", json={"visibility": "public", "rag_enabled": True}, headers=headers).status_code == 200
    assert len(asyncio.run(knowledge.retrieve("学习"))) == 1
    old_version = ready["active_version"]
    changed = service.put(f"/api/v1/admin/models/{model['id']}", json={"purpose": "embedding", "base_url": model["base_url"], "model": "new-model-test"}, headers=headers)
    assert changed.status_code == 200
    service.provider.fail = True
    service.post(f"/api/v1/admin/knowledge/{source_id}/rebuild", headers=headers)
    asyncio.run(tasks.run_pending())
    failed = service.get(f"/api/v1/admin/knowledge/{source_id}").json()
    assert failed["status"] == "failed" and failed["active_version"] == old_version
    service.provider.fail = False
    assert len(asyncio.run(knowledge.retrieve("学习"))) == 1
    assert service.provider.calls[-1][1]["model"] == "embedding-test"
    service.put(f"/api/v1/admin/knowledge/{source_id}/permissions", json={"rag_enabled": False}, headers=headers)
    assert asyncio.run(knowledge.retrieve("学习")) == []
    object_id = ready["object_id"]
    assert service.delete(f"/api/v1/admin/files/{object_id}", headers=headers).status_code == 409
    assert service.delete(f"/api/v1/admin/knowledge/{source_id}", headers=headers).status_code == 200
    assert service.delete(f"/api/v1/admin/files/{object_id}", headers=headers).status_code == 200


def test_content_rag_uses_published_snapshot_and_immediate_draft_permissions(service):
    headers = owner_headers(service)
    save_model(service, headers, "embedding")
    content.set_publication_callback(knowledge.sync_content)
    draft = content.create_draft({"title": "内容", "markdown": "已发布事实", "visibility": "public", "rag_enabled": True})
    published = content.publish_content(draft["id"], draft["revision"])
    asyncio.run(tasks.run_pending())
    assert asyncio.run(knowledge.retrieve("事实"))[0]["text"].endswith("已发布事实")
    body = {key: published[key] for key in content.Draft.model_fields}
    body.update(markdown="还未发布的秘密", rag_enabled=False)
    content.update_draft(draft["id"], body, published["revision"])
    assert asyncio.run(knowledge.retrieve("事实")) == []


def test_devices_durable_dedup_isolation_and_screenshot_retention(service):
    headers = owner_headers(service)
    device = service.post("/api/v1/admin/devices", json={"name": "Windows", "platform": "windows"}, headers=headers).json()
    token_headers = {"Authorization": "Bearer " + device["token"]}
    assert service.get("/api/v1/device/config", headers=token_headers).json()["capture_interval_seconds"] == 300
    with TestClient(service.app_fixture) as isolated:
        assert isolated.get("/api/v1/admin/contents", headers=token_headers).status_code == 401
    image = b"\x89PNG\r\n\x1a\n" + b"fixture-image"
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    def submit(capture_id, kind):
        return service.post("/api/v1/device/captures", data={"capture_id": capture_id, "captured_at": old, "source_kind": kind}, files={"file": ("image.png", image, "image/png")}, headers=token_headers)
    first = submit("screen-one", "screenshot")
    repeated = submit("screen-one", "screenshot")
    second = submit("screen-two", "screenshot")
    photo = submit("photo-one", "photo")
    assert first.status_code == 201 and not first.json()["duplicate"]
    assert repeated.json()["duplicate"]
    assert first.json()["object_id"] == second.json()["object_id"] == photo.json()["object_id"]
    cleanup = devices.cleanup_captures(7)
    assert cleanup["expired_screenshots"] == 2 and cleanup["deleted_capture_objects"] == 0
    assert storage.metadata(photo.json()["object_id"])
    assert service.delete(f"/api/v1/admin/devices/{device['id']}", headers=headers).status_code == 200
    assert service.get("/api/v1/device/config", headers=token_headers).status_code == 403


def test_daily_snapshot_manual_cron_idempotent_draft_and_failure_keep_old(service):
    headers = owner_headers(service)
    save_model(service, headers)
    device = service.post("/api/v1/admin/devices", json={"name": "iPhone", "platform": "ios"}, headers=headers).json()
    capture = service.post("/api/v1/device/captures", data={"capture_id": "note", "captured_at": "2026-10-04T12:00:00+08:00", "source_kind": "note", "text": "今天读了三页书"}, headers={"Authorization": "Bearer " + device["token"]})
    assert capture.status_code == 201
    run = daily.enqueue_daily("2026-10-04")
    assert daily.enqueue_daily("2026-10-04")["id"] == run["id"]
    asyncio.run(tasks.run_pending())
    complete = daily.run_record(run["id"])
    assert complete["status"] == "ready"
    draft = content.get_content(complete["content_id"])
    assert draft["visibility"] == "private" and not draft["published"] and not draft["rag_enabled"]
    # Replaying the handler after a terminal acknowledgement never creates another content item.
    asyncio.run(daily.generate_daily({"run_id": run["id"]}))
    newer = daily.enqueue_daily("2026-10-04", regenerate=True)
    service.provider.fail = True
    asyncio.run(tasks.run_pending())
    assert daily.run_record(newer["id"])["status"] == "failed"
    assert content.get_content(complete["content_id"])["markdown"] == draft["markdown"]
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM contents").fetchone()[0] == 1


def test_expired_screenshot_input_is_explicitly_rejected(service):
    headers = owner_headers(service)
    save_model(service, headers)
    device = service.post("/api/v1/admin/devices", json={"name": "desktop", "platform": "windows"}, headers=headers).json()
    old = datetime.now(timezone.utc) - timedelta(days=8)
    stamp = old.isoformat()
    result = service.post("/api/v1/device/captures", data={"capture_id": "expired-screen", "captured_at": stamp, "source_kind": "screenshot"}, files={"file": ("screen.png", b"\x89PNG\r\n\x1a\nfixture", "image/png")}, headers={"Authorization": "Bearer " + device["token"]})
    assert result.status_code == 201
    day = result.json()["captured_day"]
    devices.cleanup_captures(7)
    with pytest.raises(HTTPException) as error:
        daily.enqueue_daily(day)
    assert error.value.status_code == 422 and "过期" in error.value.detail


def test_events_dedup_sensitive_query_strip_owner_filter_and_unknown_ctr(service):
    visitor(service)
    payload = {"occurrence_id": "page-one", "event_name": "page_view", "path": "/blog?password=do-not-record", "source": "https://private.example?api_key=secret", "device": "mobile"}
    headers = {"Origin": settings.frontend_origin}
    assert service.post("/api/v1/public/events", json=payload, headers=headers).json()["accepted"]
    assert service.post("/api/v1/public/events", json=payload, headers=headers).json()["duplicate"]
    assert service.post("/api/v1/public/events", json=payload | {"occurrence_id": "excluded", "path": "/admin"}, headers=headers).json()["accepted"] is False
    admin = owner_headers(service)
    assert service.post("/api/v1/public/events", json=payload | {"occurrence_id": "owner"}, headers=headers).json()["reason"] == "owner"
    result = service.get("/api/v1/admin/analytics").json()
    assert result["pv"] == 1 and result["uv"] == 1 and result["ctr"] is None
    assert result["events"][0]["path"] == "/blog" and result["events"][0]["source"] == "other"
    assert "do-not-record" not in json.dumps(result)


def test_unconfigured_model_and_storage_are_real_errors(service):
    headers = owner_headers(service)
    visitor(service)
    assert service.post("/api/v1/chat/requests", json={"request_id": "unconfigured", "conversation_id": "c", "message": "q"}, headers={"Origin": settings.frontend_origin}).status_code == 503
    save_model(service, headers, "embedding")
    storage.set_test_adapter(None)
    assert service.post("/api/v1/admin/knowledge", files={"file": ("file.txt", b"real data", "text/plain")}, headers=headers).status_code == 503


def test_parser_utf8_docx_expansion_and_scanned_pdf_errors():
    assert knowledge.extract_text("中文文本".encode(), "source.md") == "中文文本"
    with pytest.raises(ValueError, match="UTF-8"):
        knowledge.extract_text(b"\xff\xfe", "bad.txt")
    from pypdf import PdfWriter
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.write(output)
    with pytest.raises(ValueError, match="OCR"):
        knowledge.extract_text(output.getvalue(), "scan.pdf")


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("172.30.91.2", "203.0.113.50", "203.0.113.50"),
    ("198.51.100.7", "203.0.113.50", "198.51.100.7"),
    ("172.30.91.3", "203.0.113.50", "172.30.91.3"),
])
def test_proxy_trust_is_exact_and_direct_forged_headers_are_ignored(peer, forwarded, expected):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    observed = {}
    async def endpoint(scope, receive, send):
        observed["host"] = scope["client"][0]
    async def receive():
        return {"type": "http.request", "body": b""}
    async def send(message):
        pass
    middleware = ProxyHeadersMiddleware(endpoint, trusted_hosts="172.30.91.2")
    asyncio.run(middleware({"type": "http", "client": (peer, 1234), "headers": [(b"x-forwarded-for", forwarded.encode()), (b"x-forwarded-proto", b"https")], "scheme": "http"}, receive, send))
    assert observed["host"] == expected


def assert_valid_capability_png(raw):
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    position, chunks, pixels = 8, [], []
    while position < len(raw):
        size = struct.unpack(">I", raw[position:position + 4])[0]
        kind = raw[position + 4:position + 8]
        payload = raw[position + 8:position + 8 + size]
        expected = struct.unpack(">I", raw[position + 8 + size:position + 12 + size])[0]
        assert zlib.crc32(kind + payload) & 0xffffffff == expected
        chunks.append(kind)
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            assert width == height == 256
            assert (depth, color, compression, filtering, interlace) == (8, 2, 0, 0, 0)
        if kind == b"IDAT":
            pixels.append(payload)
        position += 12 + size
    assert position == len(raw) and chunks == [b"IHDR", b"IDAT", b"IEND"]
    # Fully decompress and decode the RGB rows, rather than accepting the PNG signature alone.
    assert zlib.decompress(b"".join(pixels)) == (b"\x00" + b"\xff\x00\x00" * 256) * 256


def test_capability_vision_fixture_is_crc_valid_and_decodes_without_image_dependencies():
    assert_valid_capability_png(models_routes.capability_vision_png())


def completion_fixture(content_value, finish_reason, usage=None):
    return {"id": "capability-fixture", "object": "chat.completion", "created": 1,
            "model": "code-test", "choices": [{"index": 0, "message": {
                "role": "assistant", "content": content_value, "reasoning_content": "private-reasoning-never-return"},
                "finish_reason": finish_reason}], "usage": usage}


def test_capability_code_probe_has_budget_for_reasoning_and_preserves_config(service):
    headers = owner_headers(service)
    model = save_model(service, headers, "code")
    calls = []
    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        # Reproduce a provider that spends the 32-token allowance before it can emit final text.
        if body["max_tokens"] <= 32:
            return httpx.Response(200, json=completion_fixture(None, "length"))
        return httpx.Response(200, json=completion_fixture("const ok = true;", "stop", {
            "prompt_tokens": 9, "completion_tokens": 35, "total_tokens": 44,
            "completion_tokens_details": {"reasoning_tokens": 30}}))
    models_routes.set_test_http_transport(httpx.MockTransport(provider))
    result = service.post(f"/api/v1/admin/models/{model['id']}/test", headers=headers).json()
    assert result["status"] == "passed" and result["finish_reason"] == "stop"
    assert result["token_budget"] == 1024 and result["usage"]["reasoning_tokens"] == 30
    assert len(calls) == 1 and calls[0]["max_tokens"] == 1024
    assert "JavaScript" in calls[0]["messages"][0]["content"]
    assert "private-reasoning" not in json.dumps(result)
    assert "const ok" not in json.dumps(result)
    current = service.get("/api/v1/admin/models").json()["items"][0]
    assert (current["model"], current["base_url"], current["version"]) == (model["model"], model["base_url"], model["version"])


@pytest.mark.parametrize("reason,text,expected", [
    ("length", None, "output_budget_exhausted"),
    ("length", "private-partial-final-text", "output_budget_exhausted"),
    ("stop", None, "no_final_text"), ("stop", " \n ", "no_final_text"),
])
def test_capability_truncation_and_missing_final_text_are_distinct_safe_failures(service, reason, text, expected):
    headers = owner_headers(service)
    model = save_model(service, headers, "code")
    models_routes.set_test_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, json=completion_fixture(text, reason, {"prompt_tokens": 6, "completion_tokens": 1024, "total_tokens": 1030}))))
    result = service.post(f"/api/v1/admin/models/{model['id']}/test", headers=headers).json()
    assert result["status"] == "failed" and result["error_code"] == expected
    assert result["finish_reason"] == reason and result["usage"]["completion_tokens"] == 1024
    assert "private-" not in json.dumps(result) and "test-secret" not in json.dumps(result)
    assert service.get("/api/v1/admin/models").json()["items"][0]["test_status"] == "failed"


def test_capability_not_found_is_actionable_without_provider_body_or_key(service):
    headers = owner_headers(service)
    model = save_model(service, headers, "vision")
    models_routes.set_test_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        404, json={"error": {"message": "private-provider-error-with-test-secret", "type": "not_found_error"}})))
    response = service.post(f"/api/v1/admin/models/{model['id']}/test", headers=headers)
    result = response.json()
    assert result["status"] == "failed" and result["error_code"] == "model_not_found"
    assert result["http_status"] == 404 and "模型 ID" in result["error"]
    assert "private-provider" not in response.text and "test-secret" not in response.text
