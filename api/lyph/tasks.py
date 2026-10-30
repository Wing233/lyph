"""APScheduler wakes one durable SQLite task queue; it is not the source of truth."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import stat
from typing import Awaitable, Callable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, Depends, HTTPException
from . import db
from .auth import require_owner
from .config import settings

router = APIRouter(prefix="/api/v1/admin/tasks", tags=["tasks"], dependencies=[Depends(require_owner)])
_handlers: dict[str, Callable[[dict], Awaitable[dict]]] = {}
_scheduler: AsyncIOScheduler | None = None
_active_runners: set[asyncio.Task] = set()
_lock: asyncio.Lock | None = None
_stopping = False
_logger = logging.getLogger(__name__)


def _heavy_pause_path():
    # No CLI/API argument can choose a file outside the configured private data directory.
    return settings.data_dir / "heavy-pause.json"


def _pause_status() -> dict:
    status = {"paused": False, "expires_at": None, "expired": False, "invalid": False}
    path = _heavy_pause_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Not a regular marker file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError("Invalid marker file")
            if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Marker permissions must be 0600")
            marker = json.loads(stream.read(4097))
        if not isinstance(marker, dict) or set(marker) != {"format", "expires_at"}:
            raise ValueError("Invalid marker structure")
        if type(marker["format"]) is not int or marker["format"] != 1 or not isinstance(marker["expires_at"], str):
            raise ValueError("Unsupported marker format")
        expires = datetime.fromisoformat(marker["expires_at"])
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise ValueError("Marker deadline must have a timezone")
        expires = expires.astimezone(timezone.utc)
        expired = expires <= datetime.now(timezone.utc)
        return {**status, "paused": not expired, "expires_at": expires.isoformat(), "expired": expired}
    except FileNotFoundError:
        return status
    except (OSError, ValueError, TypeError, OverflowError):
        # Neither raw JSON nor its path is exposed through the owner API.
        return {**status, "paused": True, "invalid": True,
                "error": "重任务暂停标记无效；请运行 lyph resume-heavy 清除后重试"}


def _write_pause_marker(expires_at: str) -> None:
    path = _heavy_pause_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + db.uid())
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"format": 1, "expires_at": expires_at}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pause_heavy(seconds: int = 1800) -> dict:
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("--seconds 必须是正整数")
    try:
        duration = timedelta(seconds=seconds)
    except OverflowError:
        raise ValueError("--seconds 超出可表示的期限") from None
    # Both processes serialize with the same task-claim transaction. A claimed task
    # is visible in running_tasks before an external verifier is allowed to start.
    with db.transaction() as conn:
        old = _pause_status()
        if old["invalid"]:
            raise ValueError(old["error"])
        try:
            deadline = (datetime.now(timezone.utc) + duration).isoformat()
        except OverflowError:
            raise ValueError("--seconds 超出可表示的期限") from None
        _write_pause_marker(deadline)
        running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        status = _pause_status()
    return {**status, "running_tasks": running}


def resume_heavy() -> dict:
    with db.transaction() as conn:
        path = _heavy_pause_path()
        try:
            path.unlink(missing_ok=True)
        except IsADirectoryError:
            # Clear an accidentally created empty directory without deleting its contents.
            path.rmdir()
        running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        status = _pause_status()
    return {**status, "running_tasks": running}


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, result_json TEXT, error TEXT,
          lease_token TEXT, lease_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status,created_at);
        """)


def register(kind: str, handler: Callable[[dict], Awaitable[dict]]) -> None:
    _handlers[kind] = handler


def _decode(row) -> dict:
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
    item.pop("lease_token", None)
    return item


def enqueue(kind: str, payload: dict, idempotency_key: str) -> dict:
    if kind not in _handlers:
        raise ValueError(f"Task handler not registered: {kind}")
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    with db.transaction() as conn:
        old = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if old:
            if old["kind"] != kind or old["payload_json"] != encoded:
                raise HTTPException(409, "Idempotency key already used for another task")
            return _decode(old)
        task_id, ts = db.uid(), db.now()
        conn.execute("INSERT INTO tasks(id,kind,payload_json,idempotency_key,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (task_id, kind, encoded, idempotency_key, "queued", ts, ts))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _decode(row)


def get_task(task_id: str) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Task not found")
    return _decode(row)


def recover() -> None:
    # Exactly one uvicorn worker is required. A process restart invalidates its old leases.
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,"
                     "error='Worker interrupted; recovered on startup',lease_token=NULL,lease_until=NULL,updated_at=? "
                     "WHERE status='running'", (db.now(),))


async def _heartbeat(task_id: str, lease: str) -> None:
    while True:
        await asyncio.sleep(20)
        until = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
        with db.transaction() as conn:
            conn.execute("UPDATE tasks SET lease_until=? WHERE id=? AND lease_token=? AND status='running'", (until, task_id, lease))


async def run_pending() -> None:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    if _lock.locked():
        return
    async with _lock:
        with db.transaction() as conn:
            if _pause_status()["paused"]:
                return
            ui_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ui_sessions'").fetchone()
            preview_active = ui_exists and conn.execute(
                "SELECT 1 FROM ui_sessions WHERE status IN ('preview_starting','preview_ready','publish_pending','building','rolling_back','recovery_blocked') LIMIT 1"
            ).fetchone()
            # A live preview keeps consuming memory after its launch handler returns. The
            # durable UI session holds the heavy slot until publishing or stopping it.
            condition = " AND kind LIKE 'ui.%'" if preview_active else ""
            row = conn.execute("SELECT * FROM tasks WHERE status='queued'" + condition + " ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return
            task_id, lease = row["id"], db.uid()
            until = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
            conn.execute("UPDATE tasks SET status='running',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                         (lease, until, db.now(), task_id))
        current = asyncio.current_task()
        _active_runners.add(current)
        heartbeat = asyncio.create_task(_heartbeat(task_id, lease))
        try:
            handler = _handlers.get(row["kind"])
            if handler is None:
                raise RuntimeError("Missing handler for persisted task")
            result = await handler(json.loads(row["payload_json"]))
            with db.transaction() as conn:
                conn.execute("UPDATE tasks SET status='succeeded',result_json=?,error=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=? AND lease_token=?",
                             (json.dumps(result or {}, ensure_ascii=False), db.now(), task_id, lease))
        except asyncio.CancelledError:
            with db.transaction() as conn:
                conn.execute("UPDATE tasks SET status='queued',error='Worker interrupted',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=? AND lease_token=?", (db.now(), task_id, lease))
            raise
        except Exception as exc:
            # Model responses and storage SDK exceptions can include secrets: store safe class only.
            with db.transaction() as conn:
                conn.execute("UPDATE tasks SET status='failed',error=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=? AND lease_token=?",
                             (type(exc).__name__, db.now(), task_id, lease))
        finally:
            _active_runners.discard(current)
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass


def _runner_finished(runner: asyncio.Task) -> None:
    _active_runners.discard(runner)
    if not runner.cancelled() and (error := runner.exception()) is not None:
        # Errors before a task acquires its lease must remain visible without leaking payloads.
        _logger.error("Task queue runner failed (%s)", type(error).__name__)


async def _tick() -> None:
    # APScheduler only wakes the queue. Long handlers do not occupy its recurring job
    # instance, and the tracked runner remains cancellable during app shutdown.
    if _stopping or _active_runners or (_lock is not None and _lock.locked()):
        return
    runner = asyncio.create_task(run_pending())
    _active_runners.add(runner)
    runner.add_done_callback(_runner_finished)


def start() -> AsyncIOScheduler:
    global _scheduler, _lock, _stopping
    _stopping = False
    _lock = asyncio.Lock()
    recover()
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(_tick, "interval", seconds=1, max_instances=1, coalesce=True)
    _scheduler.start()
    return _scheduler


async def stop() -> None:
    global _scheduler, _stopping
    _stopping = True
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    running = list(_active_runners)
    for runner in running:
        runner.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    await asyncio.sleep(0)


@router.get("")
def list_tasks() -> dict:
    with db.transaction() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100").fetchall()
        pause = _pause_status()
        running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
    return {"items": [_decode(row) for row in rows], "heavy_pause": {**pause, "running_tasks": running}}


@router.get("/{task_id}")
def task_detail(task_id: str) -> dict:
    return get_task(task_id)


@router.post("/{task_id}/retry")
def retry(task_id: str) -> dict:
    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Task not found")
        if row["status"] != "failed":
            raise HTTPException(409, "Only failed tasks can be retried")
        conn.execute("UPDATE tasks SET status='queued',error=NULL,updated_at=? WHERE id=?", (db.now(), task_id))
    db.audit("task.retry", task_id)
    return get_task(task_id)
