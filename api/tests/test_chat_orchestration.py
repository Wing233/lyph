"""Exercise HTTP orchestration with real SQLite/cache/quotas and controlled network waits."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json

import httpx
import pytest

from lyph import chat, chat_agent, chat_cache, content, db, knowledge, models_routes, settings_routes
from lyph.config import settings
from test_intelligence import service, owner_headers, save_model, visitor  # noqa: F401


QUESTION = "请结合公开内容解释长期系统设计的取舍"


@pytest.fixture
def conversation_service(service, monkeypatch, tmp_path):
    settings_routes.init_schema()
    monkeypatch.setenv("LYPH_ENTERPRISE_DATABASE_PATH", str(tmp_path / "no-enterprise.sqlite"))
    chat._flights.clear()
    headers = owner_headers(service)
    save_model(service, headers)
    identity = visitor(service)
    origin = {"Origin": settings.frontend_origin}
    # Use the public API to spend the free allowance before testing count reservations.
    warmup = {"request_id": "warmup", "conversation_id": "warmup", "message": "个人资料"}
    assert "event: done" in service.post("/api/v1/chat/requests", json=warmup, headers=origin).text
    grant = service.post("/api/v1/admin/grants", json={"mode": "count", "limit": 12}, headers=headers).json()
    assert service.post("/api/v1/chat/authorize", json={"password": grant["password"]}, headers=origin).status_code == 200
    yield service, headers, grant, identity
    chat._flights.clear()


def quota(grant):
    with closing(db.connect()) as conn:
        return tuple(conn.execute("SELECT used,reserved FROM chat_grants WHERE id=?", (grant["id"],)).fetchone())


def state(client, request_id):
    response = client.get("/api/v1/chat/requests/" + request_id)
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, request_id, message=QUESTION, **extra):
    payload = {"request_id": request_id, "conversation_id": "conversation-" + request_id,
               "message": message} | extra
    response = client.post("/api/v1/chat/requests", json=payload, headers={"Origin": settings.frontend_origin})
    assert response.status_code == 200, response.text
    assert "event: done" in response.text, response.text
    return state(client, request_id)


class ControlledProvider:
    """Only the provider response boundary is simulated; all orchestration stays real."""
    def __init__(self, fail=False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.fail = fail

    async def retrieve(self, query, **kwargs):
        return []

    async def run(self, config, messages, request_id, attempt_id, check_cancelled):
        self.calls.append(request_id)
        self.started.set()
        await self.release.wait()
        await check_cancelled()
        if self.fail:
            raise RuntimeError("controlled provider failure")
        answer = "已核实公开资料中的设计取舍。"
        yield "delta", {"text": answer}
        yield "result", {"answer": answer, "blocks": [], "suggestions": ["有哪些项目"],
                         "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}}


async def await_event_or_response(event, task):
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait({waiter, task}, timeout=8, return_when=asyncio.FIRST_COMPLETED)
        assert done, "请求未到达预期的网络/缓存边界"
        if task in done:
            response = task.result()
            pytest.fail(f"请求提前结束: {response.status_code} {response.text}")
        await waiter
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


async def concurrent_requests(service, monkeypatch, *, fail=False, cancel=None):
    provider = ControlledProvider(fail=fail)
    monkeypatch.setattr(chat_agent, "run", provider.run)
    monkeypatch.setattr(knowledge, "retrieve", provider.retrieve)
    second_lookup_finished = asyncio.Event()
    original_lookup = chat_cache.lookup

    async def observe_lookup(info, config, request_id=None, attempt_id=None):
        # Observe completion of the real lookup; do not replace any matching logic.
        result = await original_lookup(info, config, request_id, attempt_id)
        if request_id == "second":
            second_lookup_finished.set()
        return result

    monkeypatch.setattr(chat_cache, "lookup", observe_lookup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app_fixture),
                                base_url="http://testserver", cookies=dict(service.cookies.items()),
                                headers={"Origin": settings.frontend_origin}) as client:
        def payload(request_id):
            return {"request_id": request_id, "conversation_id": "conversation-" + request_id, "message": QUESTION}
        first = asyncio.create_task(client.post("/api/v1/chat/requests", json=payload("first")))
        requests = [first]
        try:
            await await_event_or_response(provider.started, first)
            second = asyncio.create_task(client.post("/api/v1/chat/requests", json=payload("second")))
            requests.append(second)
            await await_event_or_response(second_lookup_finished, second)
            if cancel:
                response = await client.post(f"/api/v1/chat/requests/{cancel}/cancel")
                assert response.status_code == 200 and response.json()["cancel_requested"]
            provider.release.set()
            responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=8)
            persisted = []
            for ident, response in zip(("first", "second"), responses):
                assert response.status_code == 200, response.text
                stored = await client.get("/api/v1/chat/requests/" + ident)
                assert stored.status_code == 200, stored.text
                persisted.append(stored.json())
            return provider, responses, persisted
        finally:
            provider.release.set()
            await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), timeout=8)


def test_same_key_concurrent_requests_generate_once_and_settle_each_quota(conversation_service, monkeypatch):
    client, _, grant, _ = conversation_service
    provider, responses, results = asyncio.run(concurrent_requests(client, monkeypatch))
    assert len(provider.calls) == 1
    assert all("event: done" in response.text for response in responses)
    assert [result["status"] for result in results] == ["completed", "completed"]
    assert results[0]["answer"] == results[1]["answer"]
    assert results[0]["usage"]["total_tokens"] == 16
    assert results[1]["usage"]["total_tokens"] == 0
    assert results[1]["cache"]["type"] == "coalesced"
    assert quota(grant) == (2, 0)
    assert not chat._flights
    # An ordinary retransmission returns the completed result without spending again.
    replay = submit(client, "second")
    assert replay["answer"] == results[1]["answer"]
    assert quota(grant) == (2, 0) and len(provider.calls) == 1


def test_failed_leader_fails_waiters_without_spending_or_hanging(conversation_service, monkeypatch):
    client, _, grant, _ = conversation_service
    provider, responses, results = asyncio.run(concurrent_requests(client, monkeypatch, fail=True))
    assert len(provider.calls) == 1
    assert all("event: error" in response.text for response in responses)
    assert [result["status"] for result in results] == ["failed", "failed"]
    assert quota(grant) == (0, 0) and not chat._flights
    provider.fail = False
    retried = submit(client, "retry-after-failure")
    assert retried["status"] == "completed" and len(provider.calls) == 2
    assert quota(grant) == (1, 0)


@pytest.mark.parametrize("cancel,statuses,used", [
    ("first", ["cancelled", "failed"], 0),
    ("second", ["completed", "cancelled"], 1),
])
def test_cancel_releases_reservations_and_does_not_strand_shared_requests(conversation_service, monkeypatch, cancel, statuses, used):
    client, _, grant, _ = conversation_service
    provider, _, results = asyncio.run(concurrent_requests(client, monkeypatch, cancel=cancel))
    assert len(provider.calls) == 1
    assert [result["status"] for result in results] == statuses
    assert quota(grant) == (used, 0) and not chat._flights


def test_restart_releases_every_pending_shared_reservation_once(conversation_service):
    _, _, grant, identity = conversation_service
    cfg = models_routes.config_for("chat")
    # Arrange the durable state that remains after the process loses its in-memory flights.
    for number in (1, 2):
        payload = chat.ChatInput(request_id=f"restart-{number}", conversation_id=f"restart-{number}", message=QUESTION)
        row, attempt_id = chat.reserve(payload, identity["id"], cfg)
        with db.transaction() as conn:
            conn.execute("INSERT INTO model_calls(id,request_id,attempt_id,purpose,model_id,model_version,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (db.uid(), row["id"], attempt_id, "chat", cfg["id"], cfg["version"], "running", db.now()))
            conn.execute("INSERT INTO chat_tool_calls VALUES(?,?,?,?,?,?,?,?)",
                         (db.uid(), row["id"], attempt_id, "get_public_profile", "running", db.now(), None, None))
    assert quota(grant) == (0, 2)
    chat.recover_interrupted()
    chat.recover_interrupted()
    assert quota(grant) == (0, 0)
    with closing(db.connect()) as conn:
        for table in ("chat_requests", "chat_attempts", "model_calls", "chat_tool_calls"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE status='running'").fetchone()[0] == 0
        assert [row[0] for row in conn.execute("SELECT status FROM chat_requests WHERE id LIKE 'restart-%' ORDER BY id")] == ["interrupted", "interrupted"]


def seed_public_content():
    item = content.create_draft({"title": "仅本次测试可见的公开文章", "summary": "公开图表来源摘要", "markdown": "真实发布正文", "visibility": "public", "rag_enabled": True})
    return content.publish_content(item["id"], item["revision"])


def test_cache_hit_mints_fresh_view_capabilities_for_current_request(conversation_service):
    client, _, grant, identity = conversation_service
    seed_public_content()
    first = submit(client, "view-first", "有哪些文章")
    second = submit(client, "view-second", "有哪些文章")
    assert first["cache"]["type"] == "miss" and second["cache"]["type"] == "exact"
    assert first["blocks"] and second["blocks"]
    old_ids = {block["view_id"] for block in first["blocks"]}
    new_ids = {block["view_id"] for block in second["blocks"]}
    assert old_ids.isdisjoint(new_ids)
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT visitor_id,request_id FROM chat_views WHERE request_id='view-second'").fetchall()
    assert rows and all(row["visitor_id"] == identity["id"] and row["request_id"] == "view-second" for row in rows)
    assert first["blocks"][0]["items"] == second["blocks"][0]["items"]
    assert quota(grant) == (2, 0)


@pytest.mark.parametrize("change", ["rag-revocation", "visibility-revocation", "withdraw", "republish"])
def test_source_permission_publication_changes_never_reuse_old_text_or_views(conversation_service, change):
    client, _, _, _ = conversation_service
    item = seed_public_content()
    title = item["title"]
    first = submit(client, "source-first", "有哪些文章")
    assert title in json.dumps(first["blocks"], ensure_ascii=False)
    assert submit(client, "source-cached", "有哪些文章")["cache"]["type"] == "exact"
    if change == "withdraw":
        content.withdraw_content(item["id"], item["revision"])
    else:
        draft = {key: item[key] for key in content.Draft.model_fields}
        if change == "rag-revocation":
            draft["rag_enabled"] = False
        elif change == "visibility-revocation":
            draft["visibility"] = "private"
        else:
            draft.update(title="重新发布后的新文章", summary="新的来源摘要", markdown="新版正文")
        updated = content.update_draft(item["id"], draft, item["revision"])
        if change == "republish":
            content.publish_content(item["id"], updated["revision"])
    current = submit(client, "source-after-change", "有哪些文章")
    assert current["cache"]["type"] == "miss"
    assert title not in json.dumps({key: current[key] for key in ("answer", "blocks", "citations")}, ensure_ascii=False)
    if change == "republish":
        assert "重新发布后的新文章" in json.dumps(current["blocks"], ensure_ascii=False)


def test_refresh_bypasses_answer_cache_without_claiming_new_source_date(conversation_service):
    client, _, _, _ = conversation_service
    seed_public_content()
    first = submit(client, "refresh-first", "有哪些文章")
    assert submit(client, "refresh-cached", "有哪些文章")["cache"]["type"] == "exact"
    refreshed = submit(client, "refresh-again", "有哪些文章", cache_mode="refresh")
    assert refreshed["cache"]["type"] == "miss"
    assert refreshed["blocks"][0]["updated_at"] == first["blocks"][0]["updated_at"]
    assert refreshed["cache"]["data_updated_at"] == first["cache"]["data_updated_at"]
