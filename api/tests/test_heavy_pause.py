"""Real SQLite queue/CLI coordination; no remote services or production data."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from lyph import auth, cli, db, security, tasks
from lyph.config import settings


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "database_path", tmp_path / "lyph.sqlite3")
    monkeypatch.setattr(settings, "secret_key", "isolated-heavy-pause-secret-" * 3)
    monkeypatch.setattr(settings, "owner_password_hash", security.hash_password("heavy-pause-test-password"))
    monkeypatch.setattr(settings, "secure_cookies", False)
    monkeypatch.setattr(settings, "frontend_origin", "http://localhost:3000")
    monkeypatch.setattr(tasks, "_handlers", {})
    monkeypatch.setattr(tasks, "_active_runners", set())
    monkeypatch.setattr(tasks, "_lock", None)
    db.init_schema()
    auth.init_schema()
    tasks.init_schema()
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(tasks.router)
    with TestClient(app) as client:
        yield client


def login(client):
    response = client.post("/api/v1/auth/login", json={"password": "heavy-pause-test-password"},
                           headers={"Origin": settings.frontend_origin})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_pause_blocks_new_claims_resume_executes_and_owner_api_remains_available(queue):
    completed = []

    async def handle(payload):
        completed.append(payload["value"])
        return {"done": True}

    tasks.register("verification.heavy", handle)
    item = tasks.enqueue("verification.heavy", {"value": 7}, "heavy-pause-new")
    paused = tasks.pause_heavy(1800)
    assert paused["paused"] and not paused["invalid"] and paused["running_tasks"] == 0
    # Constructing a new worker lock models a new executor/process reading persisted state.
    tasks._lock = None
    tasks.recover()
    await tasks.run_pending()
    assert tasks.get_task(item["id"])["status"] == "queued"
    assert tasks.get_task(item["id"])["attempts"] == 0 and completed == []
    assert queue.get("/api/v1/admin/tasks").status_code == 401
    login(queue)
    assert queue.get("/api/v1/auth/session").json()["authenticated"]
    listing = queue.get("/api/v1/admin/tasks").json()
    assert listing["items"][0]["id"] == item["id"]
    assert listing["heavy_pause"]["expires_at"] == paused["expires_at"]
    assert listing["heavy_pause"]["paused"]
    assert not tasks.resume_heavy()["paused"]
    assert not tasks.resume_heavy()["paused"]
    await tasks.run_pending()
    assert tasks.get_task(item["id"])["status"] == "succeeded" and completed == [7]


@pytest.mark.asyncio
async def test_expired_marker_allows_claim_after_worker_restart(queue):
    async def handle(payload):
        return {"done": True}

    tasks.register("verification.heavy", handle)
    item = tasks.enqueue("verification.heavy", {}, "expired-pause")
    tasks.pause_heavy(1800)
    marker = settings.data_dir / "heavy-pause.json"
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    marker.write_text(json.dumps({"format": 1, "expires_at": past}), encoding="utf-8")
    tasks._lock = None
    tasks.recover()
    await tasks.run_pending()
    assert tasks.get_task(item["id"])["status"] == "succeeded"
    state = tasks.list_tasks()["heavy_pause"]
    assert not state["paused"] and state["expired"] and not state["invalid"]
    assert marker.exists()  # An expired marker needs no destructive cleanup to resume work.


@pytest.mark.asyncio
async def test_pause_reports_running_work_without_cancelling_it(queue):
    started, finish = asyncio.Event(), asyncio.Event()
    completed = []

    async def handle(payload):
        if payload["value"] == 1:
            started.set()
            await finish.wait()
        # This write also verifies the pause operation left no database transaction open.
        db.audit("verification.finished", details=payload)
        completed.append(payload["value"])
        return {"done": True}

    tasks.register("verification.heavy", handle)
    first = tasks.enqueue("verification.heavy", {"value": 1}, "active-first")
    runner = asyncio.create_task(tasks.run_pending())
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        pause = tasks.pause_heavy(1800)
        assert pause["running_tasks"] == 1 and not runner.done()
        second = tasks.enqueue("verification.heavy", {"value": 2}, "active-second")
        finish.set()
        await asyncio.wait_for(runner, timeout=2)
        assert completed == [1] and tasks.get_task(first["id"])["status"] == "succeeded"
        await tasks.run_pending()
        assert tasks.get_task(second["id"])["status"] == "queued"
        assert tasks.list_tasks()["heavy_pause"]["running_tasks"] == 0
        tasks.resume_heavy()
        await tasks.run_pending()
        assert completed == [1, 2]
    finally:
        if not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.parametrize("contents", ["{not-json: secret-test-value}",
    '{"format":1,"expires_at":"2030-01-01T00:00:00"}',
    '{"format":true,"expires_at":"2030-01-01T00:00:00Z"}', "x" * 4097])
@pytest.mark.asyncio
async def test_invalid_marker_fails_closed_until_explicit_resume(queue, contents):
    async def handle(payload):
        return {}

    tasks.register("verification.heavy", handle)
    item = tasks.enqueue("verification.heavy", {}, "invalid-pause")
    tasks.pause_heavy(1800)
    (settings.data_dir / "heavy-pause.json").write_text(contents, encoding="utf-8")
    state = tasks.list_tasks()["heavy_pause"]
    assert state["paused"] and state["invalid"] and state["expires_at"] is None
    assert "resume-heavy" in state["error"] and "secret-test-value" not in json.dumps(state)
    with pytest.raises(ValueError, match="resume-heavy"):
        tasks.pause_heavy(1800)
    await tasks.run_pending()
    assert tasks.get_task(item["id"])["status"] == "queued"
    assert tasks.get_task(item["id"])["attempts"] == 0
    assert not tasks.resume_heavy()["paused"]
    await tasks.run_pending()
    assert tasks.get_task(item["id"])["status"] == "succeeded"


def test_cli_pause_and_resume_never_initialize_or_recover_app(queue, monkeypatch, capsys):
    def unwanted_init(**kwargs):
        pytest.fail("Online pause/resume must not initialize or recover application state")

    monkeypatch.setitem(sys.modules, "lyph.main", SimpleNamespace(init_all=unwanted_init))
    monkeypatch.setattr(sys, "argv", ["lyph", "pause-heavy", "--seconds", "1800"])
    cli.main()
    paused = json.loads(capsys.readouterr().out)
    assert paused["paused"] and paused["running_tasks"] == 0
    monkeypatch.setattr(sys, "argv", ["lyph", "resume-heavy"])
    cli.main()
    assert not json.loads(capsys.readouterr().out)["paused"]
    cli.main()
    assert not json.loads(capsys.readouterr().out)["paused"]
    monkeypatch.setattr(sys, "argv", ["lyph", "pause-heavy", "--seconds", "0"])
    with pytest.raises(SystemExit, match="正整数"):
        cli.main()
    assert not (settings.data_dir / "heavy-pause.json").exists()


def test_pause_is_serialized_with_task_claim_transaction(queue, monkeypatch):
    # A second SQLite writer cannot claim a task while CLI publication is in progress.
    original = tasks._write_pause_marker
    checked = []

    def check_other_writer(expires_at):
        from contextlib import closing
        import sqlite3
        with closing(db.connect()) as other:
            other.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            checked.append(True)
        original(expires_at)

    monkeypatch.setattr(tasks, "_write_pause_marker", check_other_writer)
    assert tasks.pause_heavy(1800)["paused"] and checked == [True]


@pytest.mark.asyncio
async def test_claim_waiting_for_pause_transaction_sees_the_new_marker(queue, monkeypatch):
    async def handle(payload):
        return {}

    tasks.register("verification.heavy", handle)
    item = tasks.enqueue("verification.heavy", {}, "claim-pause-race")
    writing, release = threading.Event(), threading.Event()
    original = tasks._write_pause_marker
    results, errors = [], []

    def delayed_write(deadline):
        writing.set()
        if not release.wait(timeout=2):
            raise TimeoutError("Test did not release marker publication")
        original(deadline)

    def pause_in_other_process_connection():
        try:
            results.append(tasks.pause_heavy(1800))
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(tasks, "_write_pause_marker", delayed_write)
    writer = threading.Thread(target=pause_in_other_process_connection)
    writer.start()
    timer = threading.Timer(0.1, release.set)
    try:
        assert writing.wait(timeout=2)
        timer.start()
        # Claim waits for BEGIN IMMEDIATE; it must read the marker after that wait,
        # rather than using a stale unpaused read made before acquiring the transaction.
        await tasks.run_pending()
    finally:
        release.set()
        writer.join(timeout=2)
        timer.cancel()
    assert not writer.is_alive() and errors == []
    assert results[0]["paused"] and results[0]["running_tasks"] == 0
    assert tasks.get_task(item["id"])["status"] == "queued"
    assert tasks.get_task(item["id"])["attempts"] == 0


@pytest.mark.asyncio
async def test_cli_marker_is_shared_with_a_separate_python_process(queue):
    async def handle(payload):
        return {}

    tasks.register("verification.heavy", handle)
    item = tasks.enqueue("verification.heavy", {}, "subprocess-pause")
    environment = {**os.environ, "LYPH_DATA_DIR": str(settings.data_dir),
                   "LYPH_DATABASE_PATH": str(settings.database_path)}
    command = [sys.executable, "-m", "lyph.cli"]
    paused = subprocess.run(command + ["pause-heavy", "--seconds", "1800"],
                            env=environment, capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(paused.stdout)["paused"]
    await tasks.run_pending()
    assert tasks.get_task(item["id"])["status"] == "queued"
    resumed = subprocess.run(command + ["resume-heavy"], env=environment,
                             capture_output=True, text=True, check=True, timeout=10)
    assert not json.loads(resumed.stdout)["paused"]
    await tasks.run_pending()
    assert tasks.get_task(item["id"])["status"] == "succeeded"


@pytest.mark.skipif(os.name != "posix", reason="Windows does not expose Unix UID/mode enforcement")
def test_marker_is_private_and_invalid_permissions_fail_closed(queue):
    tasks.pause_heavy(1800)
    marker = settings.data_dir / "heavy-pause.json"
    info = marker.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid()
    assert not list(settings.data_dir.glob("heavy-pause.json.tmp-*"))
    marker.chmod(0o644)
    assert tasks.list_tasks()["heavy_pause"]["invalid"]
    assert not tasks.resume_heavy()["paused"]
