"""Owner-only UI workspaces. Unverified deployment capacity keeps publication disabled."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
import httpx
from pydantic import BaseModel, Field

from . import db, security, tasks
from .auth import require_owner
from .config import settings

router = APIRouter(prefix="/api/v1/admin/ui", tags=["UI revisions"], dependencies=[Depends(require_owner)])
public_router = APIRouter(prefix="/api/v1/ui-preview", tags=["isolated previews"])
MAX_FILES, MAX_BYTES = 16, 300 * 1024
TERMINAL = {"published", "cancelled", "failed", "rolled_back"}


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ui_sessions(id TEXT PRIMARY KEY, instruction TEXT NOT NULL,
          status TEXT NOT NULL, base_commit TEXT, candidate_commit TEXT, changes_json TEXT,
          error TEXT, check_json TEXT, preview_container TEXT, preview_token TEXT,
          published_version TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ui_lock(singleton INTEGER PRIMARY KEY CHECK(singleton=1),session_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ui_releases(id TEXT PRIMARY KEY,session_id TEXT,previous_id TEXT,
          status TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
    tasks.register("ui.generate", _generate_task)
    for kind, handler, failed_status in [("ui.check", _check_task, "check_failed"), ("ui.preview", _preview_task, "check_failed"), ("ui.publish", _publish_task, "build_failed")]:
        async def guarded(payload, handler=handler, failed_status=failed_status):
            try:
                return await handler(payload)
            except Exception as exc:
                session_id = payload["session_id"]
                if _row(session_id)["status"] not in TERMINAL:
                    try:
                        await _stop_preview(session_id)
                    except Exception:
                        _update(session_id, "recovery_blocked", error="无法确认候选进程停止，恢复Docker连接后再取消")
                    else:
                        _update(session_id, failed_status, error=type(exc).__name__, preview_container=None, preview_token=None)
                raise
        tasks.register(kind, guarded)


def _ui_root() -> Path:
    path = Path(os.getenv("LYPH_UI_DIR", str(settings.data_dir / "ui"))).resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _workspace(session_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise HTTPException(404, "UI session not found")
    return _ui_root() / "workspaces" / session_id


def permitted_path(value: str) -> str:
    path = PurePosixPath(value)
    if "\\" in value or path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        raise ValueError("Forbidden path")
    if path.suffix not in {".tsx", ".css", ".md"}:
        raise ValueError("Forbidden file type")
    blocked = {"admin", "api", "auth", "collector", "settings", "instrumentation", "middleware", "lib", "node_modules"}
    if blocked.intersection(path.parts):
        raise ValueError("Protected application file")
    allowed = value in {"app/page.tsx", "app/globals.css", "components/public-shell.tsx"} or value.startswith("components/public/")
    if not allowed:
        raise ValueError("Only approved public UI files can be modified")
    return path.as_posix()


def _validate_changes(changes: list[dict]) -> list[dict]:
    if not changes or len(changes) > MAX_FILES:
        raise ValueError("Candidate requires 1–16 files")
    result, seen, total = [], set(), 0
    for change in changes:
        if set(change) != {"path", "content"} or not isinstance(change["content"], str):
            raise ValueError("Each candidate file requires path and complete text content")
        path = permitted_path(change["path"])
        if path in seen:
            raise ValueError("Duplicate candidate path")
        total += len(change["content"].encode())
        if total > MAX_BYTES:
            raise ValueError("Candidate exceeds 300 KiB")
        # Public UI changes have no need to introduce host filesystem/process/secret access.
        if re.search(r"(?:node:|child_process|\bprocess\b|\bglobalThis\b|\bglobal\b|\brequire\s*\(|\bimport\s*\(|\beval\s*\(|\bFunction\s*\(|dangerouslySetInnerHTML|__proto__|\bconstructor\b)", change["content"]):
            raise ValueError("Candidate introduces protected runtime capability")
        allowed_imports = {"react", "next/link", "next/image", "next/navigation", "@/lib/api",
                           "@/components/home-experience", "@/components/chat", "@/components/tracking", "./tracking"}
        for dependency in re.findall(r"(?:\bfrom\s*|\bimport\s*)[\"']([^\"']+)[\"']", change["content"]):
            if dependency not in allowed_imports and not dependency.startswith("@/components/public/"):
                raise ValueError("Candidate imports a protected module")
        if path == "components/public-shell.tsx" and "PageEvent" not in change["content"]:
            raise ValueError("Candidate removes stable page event integration")
        seen.add(path)
        result.append({"path": path, "content": change["content"]})
    return result


def _row(session_id: str) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM ui_sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "UI session not found")
    return dict(row)


def _public(row: dict) -> dict:
    result = dict(row)
    result["files"] = json.loads(result.pop("changes_json") or "[]")
    result["checks"] = json.loads(result.pop("check_json") or "null")
    result.pop("preview_container", None)
    token = result.pop("preview_token", None)
    result["preview_url"] = f"/api/v1/ui-preview/{row['id']}/{token}" if token and row["status"] == "preview_ready" else None
    return result


def _update(session_id: str, status: str, **fields) -> None:
    columns = {"error", "base_commit", "candidate_commit", "changes_json", "check_json", "preview_container", "preview_token", "published_version"}
    if not set(fields).issubset(columns):
        raise ValueError("Unknown revision field")
    with db.transaction() as conn:
        placeholders = ",".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE ui_sessions SET status=?,updated_at=?{',' if placeholders else ''}{placeholders} WHERE id=? AND status!='cancelled'",
                     (status, db.now(), *fields.values(), session_id))
        if status in TERMINAL:
            conn.execute("DELETE FROM ui_lock WHERE session_id=?", (session_id,))


def _capacity_verified() -> None:
    report_path = os.getenv("LYPH_UI_CAPACITY_REPORT", "")
    try:
        report = json.loads(Path(report_path).read_text("utf-8"))
        valid = (report.get("passed") is True and report.get("duration_seconds", 0) >= 86400
                 and report.get("memory_limit_bytes") == 2 * 1024 ** 3
                 and report.get("target_hardware_verified") is True
                 and report.get("scenario_evidence_complete") is True)
    except (OSError, ValueError, TypeError):
        valid = False
    if not valid:
        raise HTTPException(503, "尚未完成2G整栈24小时容量验收，真实预览与发布未启用")


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=30,
                            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    if result.returncode:
        raise RuntimeError("Candidate Git operation failed")
    return result.stdout.strip()


def _verify_candidate(session_id: str) -> None:
    row, source = _row(session_id), _workspace(session_id)
    if _git(source, "rev-parse", "HEAD") != row["candidate_commit"]:
        raise RuntimeError("Candidate commit changed after review")
    if _git(source, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("Candidate source changed during validation/build")
    changed = _git(source, "diff", "--name-only", row["base_commit"], row["candidate_commit"]).splitlines()
    for name in changed:
        permitted_path(name)


def _claim(session_id: str, allowed: set[str], pending: str) -> None:
    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM ui_sessions WHERE id=?", (session_id,)).fetchone()
        if not row or row["status"] not in allowed:
            raise HTTPException(409, "当前候选状态不允许此操作")
        conn.execute("UPDATE ui_sessions SET status=?,updated_at=? WHERE id=?", (pending, db.now(), session_id))


def _copy_source(session_id: str) -> tuple[Path, str]:
    source = settings.source_dir.resolve()
    if not (source / "package-lock.json").is_file():
        raise RuntimeError("Formal frontend source and lockfile are required")
    destination = _workspace(session_id)
    if destination.exists():
        raise RuntimeError("Candidate workspace already exists")
    def ignored(folder, names):
        return [name for name in names if name in {"node_modules", ".next", ".git", "tests", "test-results", "playwright-report"} or name.startswith((".env", ".playwright")) or name.endswith((".tsbuildinfo", ".log")) or (Path(folder) / name).is_symlink()]
    shutil.copytree(source, destination, ignore=ignored)
    (destination / ".gitignore").write_text("node_modules/\n.next/\n*.log\n*.tsbuildinfo\n", "utf-8")
    _git(destination, "init")
    _git(destination, "add", ".")
    _git(destination, "-c", "user.name=lyph owner", "-c", "user.email=owner@lyph.local", "commit", "-m", "Candidate baseline")
    return destination, _git(destination, "rev-parse", "HEAD")


def _docker_client():
    import docker
    return docker.from_env(timeout=30)


def _runner(session_id: str, command: list[str], *, environment: dict | None = None, detach: bool = False):
    client = _docker_client()
    # A volume subpath exposes this candidate only; existing releases are never writable here.
    # Next's standalone trace must see dependencies inside its candidate root. A symlink to
    # /app/node_modules builds successfully but exports an unusable server without `next`.
    # CLI and application must load the same React/Next instances after dependency copying.
    command = [argument.replace("/app/node_modules/", "/candidate/node_modules/", 1)
               if argument.startswith("/app/node_modules/") else argument for argument in command]
    entry = ["/bin/sh", "-c",
        'set -e; if test -L node_modules; then rm node_modules; fi; '
        'if test ! -d node_modules; then mkdir -p .next; rm -rf .next/lyph-dependencies; '
        'cp -a /app/node_modules .next/lyph-dependencies; mv .next/lyph-dependencies node_modules; fi; '
        'exec "$@"', "lyph-ui", *command]
    return client.containers.run(os.getenv("LYPH_UI_RUNNER_IMAGE", "lyph-web-build:0.1.0"), entry,
        detach=True, name="lyph-ui-" + session_id[:16] + "-" + db.uid()[:8],
        working_dir="/candidate",
        mounts=[{"Type": "volume", "Source": os.getenv("LYPH_UI_VOLUME", "lyph_ui"), "Target": "/candidate",
                 "VolumeOptions": {"Subpath": "workspaces/" + session_id}}],
        environment={"NEXT_TELEMETRY_DISABLED": "1", "API_BASE_URL": "http://127.0.0.1:9", **(environment or {})},
        network=os.getenv("LYPH_UI_NETWORK", "lyph_preview") if detach else None,
        network_mode=None if detach else "none", read_only=True,
        tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"}, mem_limit="768m", nano_cpus=1_000_000_000,
        cap_drop=["ALL"], security_opt=["no-new-privileges:true"], pids_limit=128)


async def _wait_container(container, timeout: int) -> tuple[int, str]:
    try:
        result = await asyncio.wait_for(asyncio.to_thread(container.wait), timeout)
        # Bound public diagnostic output. Candidate model source never contains server secrets.
        logs = await asyncio.to_thread(container.logs, tail=200)
        return result["StatusCode"], logs.decode(errors="replace")[-16000:]
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.to_thread(container.kill)
        raise
    finally:
        await asyncio.to_thread(container.remove, force=True)


async def _generate_task(payload: dict) -> dict:
    from .models_routes import config_for, client_for, safe_error
    session_id = payload["session_id"]
    row = _row(session_id)
    if row["status"] == "cancelled":
        return {"cancelled": True}
    _update(session_id, "generating")
    client = None
    try:
        workspace, base_commit = await asyncio.to_thread(_copy_source, session_id)
        _update(session_id, "generating", base_commit=base_commit)
        sources = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(workspace).as_posix()
            try:
                permitted_path(relative)
            except ValueError:
                continue
            content = path.read_text("utf-8")
            if len(content.encode()) <= 80000:
                sources.append({"path": relative, "content": content})
        config = config_for("code")
        client = client_for(config)
        response = await client.chat.completions.create(model=config["model"], messages=[
            {"role": "system", "content": "Only edit the listed public UI. Preserve menu discovery, content IDs, collection and event semantics. Return one JSON object {files:[{path,content}]} containing complete replacement files. Do not edit dependencies, admin, API, auth or environment. Do not use host filesystem, process, eval or raw HTML."},
            {"role": "user", "content": json.dumps({"instruction": row["instruction"], "files": sources}, ensure_ascii=False)}], max_tokens=8192)
        text = response.choices[0].message.content or ""
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        changes = _validate_changes(json.loads(text)["files"])
        for item in changes:
            target = workspace / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or workspace not in target.resolve().parents:
                raise ValueError("Candidate path escapes workspace")
            target.write_text(item["content"], "utf-8")
        _git(workspace, "add", ".")
        _git(workspace, "-c", "user.name=lyph owner", "-c", "user.email=owner@lyph.local", "commit", "-m", "Generated public UI candidate")
        if _row(session_id)["status"] == "cancelled":
            return {"cancelled": True}
        _update(session_id, "candidate_ready", candidate_commit=_git(workspace, "rev-parse", "HEAD"), changes_json=json.dumps(changes, ensure_ascii=False))
        db.audit("ui.generate", session_id, {"files": len(changes)})
        return {"session_id": session_id, "files": len(changes)}
    except Exception as exc:
        _update(session_id, "failed", error=safe_error(exc))
        raise
    finally:
        if client:
            await client.close()


async def _check_task(payload: dict) -> dict:
    session_id = payload["session_id"]
    if _row(session_id)["status"] == "cancelled":
        return {"cancelled": True}
    _capacity_verified()
    _verify_candidate(session_id)
    _update(session_id, "checking")
    checks = []
    for command in (["node", "/app/node_modules/typescript/bin/tsc", "--noEmit"], ["npm", "run", "lint"]):
        container = await asyncio.to_thread(_runner, session_id, command)
        _update(session_id, "checking", preview_container=container.id)
        code, logs = await _wait_container(container, 180)
        _update(session_id, "checking", preview_container=None)
        checks.append({"command": command, "exit_code": code, "output": logs})
        if code:
            _update(session_id, "check_failed", check_json=json.dumps(checks), error="完整候选源码检查未通过")
            return {"passed": False, "checks": checks}
    _update(session_id, "checked", check_json=json.dumps(checks), error=None)
    return {"passed": True, "checks": checks}


async def _stop_preview(session_id: str) -> None:
    row = _row(session_id)
    if row["preview_container"]:
        client = _docker_client()
        try:
            container = await asyncio.to_thread(client.containers.get, row["preview_container"])
            await asyncio.to_thread(container.remove, force=True)
        except Exception:
            # Verify absence before considering cancellation/recovery safe.
            try:
                client.containers.get(row["preview_container"])
            except Exception as exc:
                if type(exc).__name__ != "NotFound":
                    raise RuntimeError("Cannot confirm preview process has stopped") from None


async def _preview_task(payload: dict) -> dict:
    _capacity_verified()
    session_id = payload["session_id"]
    if _row(session_id)["status"] == "cancelled":
        return {"cancelled": True}
    _verify_candidate(session_id)
    token = security.token()
    prefix = f"/api/v1/ui-preview/{session_id}/{token}"
    container = await asyncio.to_thread(_runner, session_id, ["node", "/app/node_modules/next/dist/bin/next", "dev", "--hostname", "0.0.0.0", "--port", "3000"], environment={"LYPH_PREVIEW_BASE_PATH": prefix}, detach=True)
    _update(session_id, "preview_starting", preview_container=container.id, preview_token=token)
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            for _ in range(60):
                try:
                    response = await client.get(f"http://{container.name}:3000{prefix}")
                    if response.status_code < 500:
                        _update(session_id, "preview_ready")
                        return {"preview_url": prefix}
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError("Preview startup timed out")
    except BaseException:
        await _stop_preview(session_id)
        _update(session_id, "check_failed", error="预览启动失败，生产版本未改变")
        raise


async def _publish_task(payload: dict) -> dict:
    _capacity_verified()
    session_id = payload["session_id"]
    row = _row(session_id)
    if row["status"] == "cancelled":
        return {"cancelled": True}
    if not row["candidate_commit"]:
        raise RuntimeError("Missing checked candidate")
    _verify_candidate(session_id)
    await _stop_preview(session_id)
    _update(session_id, "building")
    container = await asyncio.to_thread(_runner, session_id, ["node", "/app/node_modules/next/dist/bin/next", "build"], environment={"API_BASE_URL": "http://api:8000"})
    _update(session_id, "building", preview_container=container.id)
    code, logs = await _wait_container(container, 600)
    _update(session_id, "building", preview_container=None, preview_token=None)
    if code:
        _update(session_id, "build_failed", error="构建失败，线上版本未改变", check_json=json.dumps({"build_output": logs}))
        return {"published": False}
    _verify_candidate(session_id)
    source = _workspace(session_id)
    standalone = source / ".next" / "standalone"
    if not (standalone / "server.js").is_file():
        raise RuntimeError("Build did not produce a standalone server")
    releases = _ui_root() / "releases"
    releases.mkdir(exist_ok=True)
    release_id = db.uid()
    release = releases / release_id
    shutil.copytree(standalone, release)
    shutil.copytree(source / ".next" / "static", release / ".next" / "static", dirs_exist_ok=True)
    if (source / "public").exists():
        shutil.copytree(source / "public", release / "public", dirs_exist_ok=True)
    pointer = releases / "active.json"
    previous = json.loads(pointer.read_text("utf-8"))["id"] if pointer.exists() else "baseline"
    temporary = releases / (release_id + ".json.tmp")
    temporary.write_text(json.dumps({"id": release_id}), "utf-8")
    temporary.replace(pointer)
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            for _ in range(30):
                response = await client.get(os.getenv("LYPH_RELEASE_HEALTH_URL", "http://web:3000/_lyph/health"))
                if response.status_code == 200 and response.json().get("active_version") == release_id:
                    with db.transaction() as conn:
                        conn.execute("INSERT INTO ui_releases VALUES(?,?,?,?,?)", (release_id, session_id, previous, "active", db.now()))
                        conn.execute("UPDATE ui_releases SET status='superseded' WHERE id=?", (previous,))
                    _update(session_id, "published", published_version=release_id)
                    db.audit("ui.publish", session_id, {"release": release_id})
                    return {"published": True, "release_id": release_id}
                await asyncio.sleep(1)
        raise RuntimeError("Release health check failed")
    except BaseException:
        temporary.write_text(json.dumps({"id": previous}), "utf-8")
        temporary.replace(pointer)
        _update(session_id, "build_failed", error="发布健康检查失败，已恢复旧版本")
        raise


class Instruction(BaseModel):
    instruction: str = Field(min_length=1, max_length=8000)


@router.get("/sessions")
def list_sessions():
    with closing(db.connect()) as conn:
        return {"items": [_public(dict(row)) for row in conn.execute("SELECT * FROM ui_sessions ORDER BY created_at DESC LIMIT 100")], "capacity_verified": _capacity_status()}


def _capacity_status() -> bool:
    try:
        _capacity_verified()
        return True
    except HTTPException:
        return False


@router.post("/sessions", status_code=202)
def create_session(payload: Instruction):
    from .models_routes import config_for
    config_for("code")
    session_id, stamp = db.uid(), db.now()
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM ui_lock").fetchone():
            raise HTTPException(409, "已有UI修改会话，请先完成或取消")
        conn.execute("INSERT INTO ui_sessions(id,instruction,status,created_at,updated_at) VALUES(?,?,?,?,?)", (session_id, payload.instruction, "queued", stamp, stamp))
        conn.execute("INSERT INTO ui_lock VALUES(1,?)", (session_id,))
    tasks.enqueue("ui.generate", {"session_id": session_id}, "ui.generate:" + session_id)
    return _public(_row(session_id))


@router.get("/sessions/{session_id}")
@router.get("/sessions/{session_id}/candidate")
def candidate(session_id: str):
    result = _public(_row(session_id))
    path = _workspace(session_id)
    if path.exists() and result["candidate_commit"]:
        result["diff"] = _git(path, "diff", result["base_commit"], result["candidate_commit"])
    return result


@router.post("/sessions/{session_id}/check", status_code=202)
def check(session_id: str):
    _capacity_verified()
    _claim(session_id, {"candidate_ready", "check_failed", "checked", "build_failed"}, "check_pending")
    return tasks.enqueue("ui.check", {"session_id": session_id}, "ui.check:" + session_id + ":" + db.uid())


@router.post("/sessions/{session_id}/preview", status_code=202)
def preview(session_id: str):
    _capacity_verified()
    _claim(session_id, {"checked"}, "preview_pending")
    return tasks.enqueue("ui.preview", {"session_id": session_id}, "ui.preview:" + session_id + ":" + db.uid())


@router.post("/sessions/{session_id}/publish", status_code=202)
def publish(session_id: str):
    _capacity_verified()
    _claim(session_id, {"preview_ready"}, "publish_pending")
    return tasks.enqueue("ui.publish", {"session_id": session_id}, "ui.publish:" + session_id + ":" + db.uid())


@router.post("/sessions/{session_id}/cancel")
async def cancel(session_id: str):
    row = _row(session_id)
    if row["status"] in {"generating", "checking", "preview_starting", "building", "published", "check_pending", "preview_pending", "publish_pending", "rolling_back", "rollback_failed"}:
        raise HTTPException(409, "任务执行中或已发布会话不能取消，请等待任务完成")
    await _stop_preview(session_id)
    _update(session_id, "cancelled")
    db.audit("ui.cancel", session_id)
    return {"cancelled": True}


@router.post("/sessions/{session_id}/rollback")
async def rollback(session_id: str):
    _capacity_verified()
    row = _row(session_id)
    with db.transaction() as conn:
        lock = conn.execute("SELECT session_id FROM ui_lock").fetchone()
        if lock and not (lock["session_id"] == session_id and row["status"] == "rollback_failed"):
            raise HTTPException(409, "已有UI修改任务，请先完成或取消")
        release = conn.execute("SELECT * FROM ui_releases WHERE id=?", (row["published_version"],)).fetchone()
        if not release or release["status"] != "active":
            raise HTTPException(409, "只能回滚当前活动版本")
        conn.execute("INSERT OR IGNORE INTO ui_lock VALUES(1,?)", (session_id,))
    _update(session_id, "rolling_back")
    releases = _ui_root() / "releases"
    temporary = releases / "rollback.json.tmp"
    temporary.write_text(json.dumps({"id": release["previous_id"]}), "utf-8")
    temporary.replace(releases / "active.json")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            for _ in range(30):
                try:
                    response = await client.get(os.getenv("LYPH_RELEASE_HEALTH_URL", "http://web:3000/_lyph/health"))
                    if response.status_code == 200 and response.json().get("active_version") == release["previous_id"]:
                        with db.transaction() as conn:
                            conn.execute("UPDATE ui_releases SET status='rolled_back' WHERE id=?", (release["id"],))
                            conn.execute("UPDATE ui_releases SET status='active' WHERE id=?", (release["previous_id"],))
                        _update(session_id, "rolled_back")
                        db.audit("ui.rollback", session_id, {"version": release["previous_id"]})
                        return {"rolled_back": True, "active_version": release["previous_id"]}
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise HTTPException(503, "回滚健康检查未通过，已请求恢复原活动版本")
    except BaseException:
        temporary.write_text(json.dumps({"id": release["id"]}), "utf-8")
        temporary.replace(releases / "active.json")
        _update(session_id, "rollback_failed", error="回滚健康检查失败，已请求恢复原活动版本；请重试回滚或重启恢复")
        raise


async def recover() -> None:
    """Never replay a half-built UI revision against an unknown live version."""
    with closing(db.connect()) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM ui_sessions WHERE status NOT IN ('published','cancelled','failed','rolled_back')")]
        active = conn.execute("SELECT id FROM ui_releases WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
    if not rows:
        return
    releases = _ui_root() / "releases"
    if releases.exists():
        temporary = releases / "recovery.json.tmp"
        temporary.write_text(json.dumps({"id": active["id"] if active else "baseline"}), "utf-8")
        temporary.replace(releases / "active.json")
    for row in rows:
        try:
            await _stop_preview(row["id"])
        except Exception:
            _update(row["id"], "recovery_blocked", error="无法确认旧候选容器已停止，请恢复Docker连接后取消会话")
        else:
            _update(row["id"], "failed", error="服务重启中断候选流程，活动版本已恢复，请重新生成候选", preview_container=None, preview_token=None)
        with db.transaction() as conn:
            conn.execute("UPDATE tasks SET status='failed',error='Interrupted UI revision requires review',lease_token=NULL,lease_until=NULL WHERE kind LIKE 'ui.%' AND status IN ('running','queued') AND json_extract(payload_json,'$.session_id')=?", (row["id"],))


@public_router.api_route("/{session_id}/{token}", methods=["GET", "HEAD"])
@public_router.api_route("/{session_id}/{token}/{asset_path:path}", methods=["GET", "HEAD"])
async def preview_asset(session_id: str, token: str, request: Request, asset_path: str = ""):
    row = _row(session_id)
    if row["status"] not in {"preview_starting", "preview_ready"} or not row["preview_token"] or not __import__("hmac").compare_digest(row["preview_token"], token):
        raise HTTPException(404, "Preview unavailable")
    if datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"]) > timedelta(minutes=30):
        raise HTTPException(410, "Preview expired; cancel the session")
    container = await asyncio.to_thread(_docker_client().containers.get, row["preview_container"])
    prefix = f"/api/v1/ui-preview/{session_id}/{token}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        upstream = await client.request(request.method, f"http://{container.name}:3000{prefix}/{asset_path}")
    origin = settings.frontend_origin
    csp = f"sandbox allow-scripts; default-src 'none'; script-src {origin} 'unsafe-inline' 'unsafe-eval'; style-src {origin} 'unsafe-inline'; img-src {origin} data: blob:; font-src {origin}; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors {origin}"
    return Response(upstream.content, status_code=upstream.status_code, headers={"Content-Type": upstream.headers.get("content-type", "application/octet-stream"), "Content-Security-Policy": csp, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
