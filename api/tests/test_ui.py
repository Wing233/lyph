"""Review boundaries and lifecycle; no production model or capacity bypass."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from lyph import auth, db, security, tasks, ui_routes
from lyph.config import settings


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "database_path", tmp_path / "ui.sqlite3")
    monkeypatch.setattr(settings, "secret_key", "ui-fixture-key-" * 4)
    monkeypatch.setattr(settings, "owner_password_hash", security.hash_password("ui-fixture-password"))
    monkeypatch.setattr(settings, "secure_cookies", False)
    monkeypatch.setattr(settings, "frontend_origin", "http://localhost:3000")
    monkeypatch.setenv("LYPH_UI_DIR", str(tmp_path / "ui"))
    monkeypatch.delenv("LYPH_UI_CAPACITY_REPORT", raising=False)
    db.init_schema()
    auth.init_schema()
    tasks.init_schema()
    ui_routes.init_schema()
    app = FastAPI()
    for router in [auth.router, ui_routes.router, ui_routes.public_router]:
        app.include_router(router)
    with TestClient(app) as client:
        yield client
    tasks._handlers.clear()


def session(status="candidate_ready"):
    identifier = db.uid()
    with db.transaction() as conn:
        conn.execute("INSERT INTO ui_sessions(id,instruction,status,created_at,updated_at) VALUES(?,?,?,?,?)", (identifier, "调整间距", status, db.now(), db.now()))
        conn.execute("INSERT INTO ui_lock VALUES(1,?)", (identifier,))
    return identifier


def owner(client):
    result = client.post("/api/v1/auth/login", json={"password": "ui-fixture-password"}, headers={"Origin": settings.frontend_origin})
    assert result.status_code == 200
    return {"Origin": settings.frontend_origin, "X-CSRF-Token": result.json()["csrf_token"]}


def test_owner_csrf_and_unverified_capacity_gate(ui):
    assert ui.get("/api/v1/admin/ui/sessions").status_code == 401
    headers = owner(ui)
    identifier = session()
    assert ui.post(f"/api/v1/admin/ui/sessions/{identifier}/check").status_code == 403
    response = ui.post(f"/api/v1/admin/ui/sessions/{identifier}/check", headers=headers)
    assert response.status_code == 503 and "24小时" in response.json()["detail"]
    assert ui_routes._row(identifier)["status"] == "candidate_ready"


@pytest.mark.parametrize("path", ["../app/page.tsx", "/app/page.tsx", "app\\page.tsx", "app/admin/page.tsx", "app/layout.tsx", "package.json", "lib/api.ts", "components/chat.tsx", "components/public/.secret.tsx"])
def test_path_restrictions(path):
    with pytest.raises(ValueError):
        ui_routes.permitted_path(path)


@pytest.mark.parametrize("source", ["import fs from 'fs'; export default () => null", "import {x} from '@/lib/secrets';", "eval('x')", "globalThis['process']", "require('fs')", "import('fs')", "x.constructor('x')", "process.env.KEY", "<div dangerouslySetInnerHTML={{__html: x}}/>"])
def test_protected_runtime_rejected(source):
    with pytest.raises(ValueError):
        ui_routes._validate_changes([{"path": "app/page.tsx", "content": source}])


def test_complete_files_size_and_duplicate():
    with pytest.raises(ValueError):
        ui_routes._validate_changes([{"path": "app/page.tsx", "diff": "@@"}])
    change = {"path": "app/globals.css", "content": "body { color: black; }"}
    with pytest.raises(ValueError):
        ui_routes._validate_changes([change, change])
    with pytest.raises(ValueError):
        ui_routes._validate_changes([{**change, "content": "x" * (ui_routes.MAX_BYTES + 1)}])
    assert ui_routes._validate_changes([change]) == [change]


def test_atomic_claim_and_cancel_never_resurrect(ui):
    identifier = session()
    def claim(_):
        try:
            ui_routes._claim(identifier, {"candidate_ready"}, "check_pending")
            return True
        except HTTPException as exc:
            assert exc.status_code == 409
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(claim, range(2))) == 1
    with db.transaction() as conn:
        conn.execute("UPDATE ui_sessions SET status='cancelled' WHERE id=?", (identifier,))
    ui_routes._update(identifier, "checked")
    assert ui_routes._row(identifier)["status"] == "cancelled"


def test_runner_has_only_candidate_subpath(ui, monkeypatch):
    captured = {}
    class Containers:
        def run(self, image, command, **kwargs):
            captured.update(kwargs)
            return object()
    class Client:
        containers = Containers()
    monkeypatch.setattr(ui_routes, "_docker_client", lambda: Client())
    identifier = session()
    ui_routes._runner(identifier, ["npm", "run", "lint"])
    assert captured["mounts"][0]["VolumeOptions"]["Subpath"] == "workspaces/" + identifier
    assert len(captured["mounts"]) == 1 and captured["network_mode"] == "none"
    assert captured["read_only"] and captured["cap_drop"] == ["ALL"]
    assert "LYPH_SECRET_KEY" not in captured["environment"]


def test_source_copy_omits_credentials_and_browser_traces(ui, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "package-lock.json").write_text("{}", "utf-8")
    (source / "page.tsx").write_text("export default () => null", "utf-8")
    for folder in [".playwright-cli", "tests", "node_modules"]:
        (source / folder).mkdir()
        (source / folder / "private-fixture.txt").write_text("test-only-secret", "utf-8")
    for filename in [".env.production", "debug.log"]:
        (source / filename).write_text("test-only-secret", "utf-8")
    monkeypatch.setattr(settings, "source_dir", source)
    workspace, commit = ui_routes._copy_source(db.uid())
    assert (workspace / "page.tsx").is_file() and commit
    assert not any((workspace / name).exists() for name in [".playwright-cli", "tests", "node_modules", ".env.production", "debug.log"])


@pytest.mark.asyncio
async def test_restart_recovery_releases_no_unconfirmed_process(ui, monkeypatch):
    identifier = session("building")
    async def cannot_stop(_):
        raise RuntimeError("Docker unavailable")
    monkeypatch.setattr(ui_routes, "_stop_preview", cannot_stop)
    await ui_routes.recover()
    assert ui_routes._row(identifier)["status"] == "recovery_blocked"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT session_id FROM ui_lock").fetchone()[0] == identifier
    async def stopped(_):
        return None
    monkeypatch.setattr(ui_routes, "_stop_preview", stopped)
    await ui_routes.recover()
    assert ui_routes._row(identifier)["status"] == "failed"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT 1 FROM ui_lock").fetchone() is None


@pytest.mark.asyncio
async def test_failed_worker_returns_reviewable_candidate_state(ui, monkeypatch):
    identifier = session("check_pending")
    monkeypatch.setattr(ui_routes, "_capacity_verified", lambda: None)
    monkeypatch.setattr(ui_routes, "_verify_candidate", lambda _: (_ for _ in ()).throw(RuntimeError("bad candidate")))
    with pytest.raises(RuntimeError):
        await tasks._handlers["ui.check"]({"session_id": identifier})
    assert ui_routes._row(identifier)["status"] == "check_failed"
    headers = owner(ui)
    assert ui.post(f"/api/v1/admin/ui/sessions/{identifier}/cancel", headers=headers).status_code == 200
