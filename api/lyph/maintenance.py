"""Bounded, authenticated backups. Restore is deliberately an offline CLI action."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from . import db, storage, tasks
from .auth import require_owner
from .config import settings

router = APIRouter(prefix="/api/v1/admin/backups", tags=["backups"], dependencies=[Depends(require_owner)])
MAGIC = b"LYPHBK01"
CHUNK = 1024 * 1024


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY, filename TEXT NOT NULL,
          size INTEGER NOT NULL, sha256 TEXT NOT NULL, object_count INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS maintenance_runs(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
          result_json TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
    tasks.register("backup", _backup_task)
    tasks.register("cleanup", _cleanup_task)


def _folder() -> Path:
    folder = settings.data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def _key() -> bytes:
    if len(settings.secret_key) < 32:
        raise RuntimeError("Backup encryption key is not configured")
    return hashlib.sha256((settings.secret_key + ":lyph-backup-v1").encode()).digest()


def _encrypt(source: Path, target: Path) -> None:
    nonce = secrets.token_bytes(12)
    cipher = Cipher(algorithms.AES(_key()), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(MAGIC)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        os.chmod(target, 0o600)
        outgoing.write(MAGIC + nonce)
        while data := incoming.read(CHUNK):
            outgoing.write(cipher.update(data))
        outgoing.write(cipher.finalize())
        outgoing.write(cipher.tag)


def _decrypt(source: Path, target: Path) -> None:
    size = source.stat().st_size
    if size < len(MAGIC) + 12 + 16:
        raise ValueError("Invalid backup")
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        os.chmod(target, 0o600)
        if incoming.read(8) != MAGIC:
            raise ValueError("Unsupported backup format")
        nonce = incoming.read(12)
        incoming.seek(-16, 2)
        tag = incoming.read(16)
        incoming.seek(20)
        cipher = Cipher(algorithms.AES(_key()), modes.GCM(nonce, tag)).decryptor()
        cipher.authenticate_additional_data(MAGIC)
        remaining = size - 36
        while remaining:
            data = incoming.read(min(CHUNK, remaining))
            if not data:
                raise ValueError("Truncated backup")
            outgoing.write(cipher.update(data))
            remaining -= len(data)
        outgoing.write(cipher.finalize())


def _hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(CHUNK):
            result.update(data)
    return result.hexdigest()


def _json_member(archive, name: str, value) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2).encode()
    member = tarfile.TarInfo(name)
    member.size, member.mode = len(data), 0o600
    archive.addfile(member, io.BytesIO(data))


def backup() -> dict:
    folder = _folder()
    with tempfile.TemporaryDirectory(prefix="backup-", dir=settings.data_dir) as scratch:
        scratch_path = Path(scratch)
        snapshot, archive_path = scratch_path / "database.sqlite3", scratch_path / "archive.tar.gz"
        with closing(db.connect()) as source, closing(sqlite3.connect(snapshot)) as destination:
            source.backup(destination)
        os.chmod(snapshot, 0o600)
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.row_factory = sqlite3.Row
            objects = [dict(row) for row in conn.execute("SELECT * FROM files WHERE state='active'")]
            versions = [dict(row) for row in conn.execute("SELECT * FROM ui_releases")] if _table_exists(conn, "ui_releases") else []
        ui_root = Path(os.getenv("LYPH_UI_DIR", str(settings.data_dir / "ui"))).resolve()
        ui_files = []
        for version in versions:
            if not re.fullmatch(r"[a-f0-9]{32}", version["id"]):
                raise ValueError("Invalid stored UI version")
            release = ui_root / "releases" / version["id"]
            if not (release / "server.js").is_file():
                raise RuntimeError("Published UI files unavailable; refusing incomplete backup")
            for file in sorted(release.rglob("*")):
                if file.is_symlink():
                    raise ValueError("UI release contains unsupported symbolic links")
                if file.is_file():
                    ui_files.append({"path": file.relative_to(ui_root).as_posix(), "size": file.stat().st_size, "sha256": _hash(file)})
        manifest = {"format": 1, "created_at": db.now(), "database_sha256": _hash(snapshot),
                    "objects": objects, "retention": "7 backups on this VPS only",
                    "ui_versions": versions, "ui_files": ui_files,
                    "requires_original_secret_key": True}
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(snapshot, arcname="database.sqlite3")
            _json_member(archive, "manifest.json", manifest)
            for item in ui_files:
                archive.add(ui_root / item["path"], arcname="ui/" + item["path"])
            client = storage._client() if objects else None
            for item in objects:
                response = client.get_object(settings.minio_bucket, item["object_key"])
                try:
                    member = tarfile.TarInfo("objects/" + item["sha256"])
                    member.size, member.mode = item["size"], 0o600
                    archive.addfile(member, response)
                finally:
                    response.close()
                    response.release_conn()
            # Include reconstructable web source, never .env, installed dependencies or build caches.
            root = settings.source_dir.resolve()
            if root.is_dir():
                for source_folder, directories, filenames in os.walk(root, followlinks=False):
                    directories[:] = sorted(name for name in directories if not name.startswith(".") and name not in {"node_modules", "test-results", "playwright-report", "results"} and not (Path(source_folder) / name).is_symlink())
                    for filename in sorted(filenames):
                        path = Path(source_folder) / filename
                        if filename.startswith(".") or path.is_symlink():
                            continue
                        if path.suffix in {".ts", ".tsx", ".css", ".md", ".json", ".mjs", ".js", ".woff2", ".svg"}:
                            archive.add(path, arcname="source/" + path.relative_to(root).as_posix())
        backup_id = db.uid()
        filename = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{backup_id}.lyph.enc"
        partial = folder / (filename + ".partial")
        try:
            _encrypt(archive_path, partial)
            final = folder / filename
            partial.replace(final)
        finally:
            partial.unlink(missing_ok=True)
        result = {"id": backup_id, "filename": filename, "size": final.stat().st_size,
                  "sha256": _hash(final), "object_count": len(objects), "created_at": db.now()}
        with db.transaction() as conn:
            conn.execute("INSERT INTO backups VALUES(?,?,?,?,?,?)", tuple(result.values()))
            expired = conn.execute("SELECT * FROM backups ORDER BY created_at DESC LIMIT -1 OFFSET 7").fetchall()
            for row in expired:
                (folder / Path(row["filename"]).name).unlink(missing_ok=True)
                conn.execute("DELETE FROM backups WHERE id=?", (row["id"],))
        db.audit("backup.create", backup_id, {"objects": len(objects), "size": result["size"]})
        return result


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def cleanup() -> dict:
    stamp = datetime.now(timezone.utc)
    cut = lambda days: (stamp - timedelta(days=days)).isoformat(timespec="milliseconds")
    result: dict[str, int] = {}
    with db.transaction() as conn:
        if _table_exists(conn, "chat_requests"):
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_requests)")}
            body_columns = [name for name in ("message", "answer", "citations_json", "blocks_json", "suggestions_json") if name in columns]
            assignments = ",".join(name + "=NULL" for name in body_columns)
            has_body = " OR ".join(name + " IS NOT NULL" for name in body_columns)
            result["chat_bodies"] = conn.execute("UPDATE chat_requests SET " + assignments
                                               + " WHERE created_at<? AND status!='running' AND (" + has_body + ")", (cut(30),)).rowcount
        if _table_exists(conn, "chat_tool_calls"):
            result["chat_tools"] = conn.execute("DELETE FROM chat_tool_calls WHERE created_at<?", (cut(30),)).rowcount
        if _table_exists(conn, "chat_answer_cache"):
            result["chat_cache"] = conn.execute("DELETE FROM chat_answer_cache WHERE expires_at<=?", (db.now(),)).rowcount
        if _table_exists(conn, "chat_views"):
            result["chat_views"] = conn.execute("DELETE FROM chat_views WHERE expires_at<=?", (stamp.timestamp(),)).rowcount
        if _table_exists(conn, "chat_view_contexts"):
            active = " AND NOT EXISTS (SELECT 1 FROM chat_requests r WHERE r.visitor_id=chat_view_contexts.visitor_id AND r.conversation_id=chat_view_contexts.conversation_id AND r.status='running')" if _table_exists(conn, "chat_requests") else ""
            result["chat_view_contexts"] = conn.execute("DELETE FROM chat_view_contexts WHERE updated_at<?" + active, (cut(30),)).rowcount
        if _table_exists(conn, "mcp_calls"):
            result["mcp_calls"] = conn.execute("DELETE FROM mcp_calls WHERE created_at<?", (cut(180),)).rowcount
        if _table_exists(conn, "visit_events"):
            result["visit_events"] = conn.execute("DELETE FROM visit_events WHERE created_at<?", (cut(30),)).rowcount
        result["audit"] = conn.execute("DELETE FROM audit_log WHERE created_at<?", (cut(180),)).rowcount
        if _table_exists(conn, "owner_sessions"):
            result["owner_sessions"] = conn.execute("DELETE FROM owner_sessions WHERE expires_at<?", (db.now(),)).rowcount
    # Device module owns capture references/retention; use its canonical implementation.
    try:
        from .devices import cleanup_captures
        result.update(cleanup_captures(7))
    except ImportError:
        pass
    with db.transaction() as conn:
        conn.execute("INSERT INTO maintenance_runs VALUES(?,?,?,?)", (db.uid(), "cleanup", json.dumps(result), db.now()))
        retained = {row[0] for row in conn.execute("SELECT filename FROM backups")}
    # A restored snapshot may not list newer archives. Keep these extra copies at most 7 days.
    orphan_cutoff = (stamp - timedelta(days=7)).timestamp()
    result["extra_backups"] = 0
    for archive in _folder().glob("*.lyph.enc"):
        if archive.name not in retained and archive.stat().st_mtime < orphan_cutoff and not archive.is_symlink():
            archive.unlink()
            result["extra_backups"] += 1
    return result


def restore_backup(path: Path) -> dict:
    """Caller must stop the API before restore. Verify before changing live files."""
    if os.getenv("LYPH_RESTORE_OFFLINE") != "1":
        raise RuntimeError("Stop the API and set LYPH_RESTORE_OFFLINE=1 before restoring")
    path = path.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=settings.data_dir) as scratch:
        root = Path(scratch)
        plain = root / "verified.tar.gz"
        _decrypt(path, plain)  # GCM authentication completes before archive contents are used.
        extracted = root / "verified"
        extracted.mkdir()
        with tarfile.open(plain, "r:gz") as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or "\\" in member.name or not relative.parts or not member.isfile():
                    raise ValueError("Unsafe archive entry")
                if member.name not in {"manifest.json", "database.sqlite3"} and relative.parts[0] not in {"objects", "source", "ui"}:
                    raise ValueError("Unknown archive entry")
                member_limit = 1024 * 1024 * 1024 if member.name == 'database.sqlite3' else 128 * 1024 * 1024
                if member.size > member_limit:
                    raise ValueError("Oversized archive entry")
                destination = extracted.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                with destination.open("xb") as target:
                    shutil.copyfileobj(source, target, CHUNK)
        manifest = json.loads((extracted / "manifest.json").read_text("utf-8"))
        snapshot = extracted / "database.sqlite3"
        if manifest.get("format") != 1 or _hash(snapshot) != manifest["database_sha256"]:
            raise ValueError("Invalid backup manifest/database checksum")
        with closing(sqlite3.connect(snapshot)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Backup database failed integrity check")
            for item in manifest["objects"]:
                if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
                    raise ValueError("Invalid object identifier")
                source = extracted / "objects" / item["sha256"]
                if source.stat().st_size != item["size"] or _hash(source) != item["sha256"]:
                    raise ValueError("Backup object failed integrity check")
        for item in manifest.get("ui_files", []):
            relative = PurePosixPath(item["path"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in item["path"] or len(relative.parts) < 3 or relative.parts[0] != "releases" or not re.fullmatch(r"[a-f0-9]{32}", relative.parts[1]):
                raise ValueError("Invalid UI version manifest path")
            source = extracted / "ui" / item["path"]
            if source.stat().st_size != item["size"] or _hash(source) != item["sha256"]:
                raise ValueError("Backup UI release failed integrity check")
        client = storage._client() if manifest["objects"] else None
        if client and not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)
        for item in manifest["objects"]:
            source = extracted / "objects" / item["sha256"]
            with source.open("rb") as stream:
                client.put_object(settings.minio_bucket, item["object_key"], stream, item["size"], content_type=item["content_type"])
        with closing(sqlite3.connect(snapshot)) as source, closing(db.connect()) as target:
            source.backup(target)
        # Old authorization/quotas must never revive when restoring an older snapshot.
        with db.transaction() as conn:
            if _table_exists(conn, "owner_sessions"):
                conn.execute("DELETE FROM owner_sessions")
            if _table_exists(conn, "chat_grants"):
                conn.execute("UPDATE chat_grants SET revoked=1,reserved=0")
            if _table_exists(conn, "chat_visitors"):
                conn.execute("UPDATE chat_visitors SET free_used=1,free_reserved=0,grant_id=NULL")
            if _table_exists(conn, "devices"):
                conn.execute("UPDATE devices SET revoked=1")
            if _table_exists(conn, "chat_answer_cache"):
                conn.execute("DELETE FROM chat_answer_cache")
            if _table_exists(conn, "chat_views"):
                conn.execute("DELETE FROM chat_views")
            if _table_exists(conn, "chat_view_contexts"):
                conn.execute("DELETE FROM chat_view_contexts")
            if _table_exists(conn, "mcp_credentials"):
                conn.execute("UPDATE mcp_credentials SET revoked=1")
            if _table_exists(conn, "ui_sessions"):
                conn.execute("UPDATE ui_sessions SET status='failed',preview_token=NULL,preview_container=NULL,error='Restore invalidated candidate session' WHERE status NOT IN ('published','rolled_back')")
                conn.execute("DELETE FROM ui_lock")
            if _table_exists(conn, "tasks"):
                conn.execute("UPDATE tasks SET status='failed',lease_token=NULL,lease_until=NULL,error='Restore requires reviewed retry' WHERE status IN ('running','queued')")
        ui_source = extracted / "ui" / "releases"
        if ui_source.exists():
            ui_root = Path(os.getenv("LYPH_UI_DIR", str(settings.data_dir / "ui"))).resolve()
            shutil.copytree(ui_source, ui_root / "releases", dirs_exist_ok=True)
            active = next((row["id"] for row in manifest.get("ui_versions", []) if row["status"] == "active"), "baseline")
            pointer = ui_root / "releases" / "restore.json.tmp"
            pointer.write_text(json.dumps({"id": active}), "utf-8")
            pointer.replace(ui_root / "releases" / "active.json")
        source_dir = extracted / "source"
        if source_dir.exists():
            recovered = settings.data_dir / "recovered-source" / db.uid()
            shutil.copytree(source_dir, recovered)
        cleanup()
        db.audit("backup.restore", details={"objects": len(manifest["objects"]), "authorizations_invalidated": True})
        return {"restored": True, "objects": len(manifest["objects"]), "authorizations_invalidated": True}


async def _backup_task(payload: dict) -> dict:
    return await asyncio.to_thread(backup)


async def _cleanup_task(payload: dict) -> dict:
    return await asyncio.to_thread(cleanup)


def register_jobs(scheduler) -> None:
    def enqueue_backup():
        date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        tasks.enqueue("backup", {}, "backup:" + date)
    def enqueue_cleanup():
        date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        tasks.enqueue("cleanup", {}, "cleanup:" + date)
    scheduler.add_job(enqueue_backup, "cron", hour=3, minute=0, id="daily-backup", replace_existing=True, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(enqueue_cleanup, "cron", hour=3, minute=30, id="daily-cleanup", replace_existing=True, coalesce=True, misfire_grace_time=3600)


@router.get("")
def list_backups() -> dict:
    with closing(db.connect()) as conn:
        return {"items": [dict(row) for row in conn.execute("SELECT * FROM backups ORDER BY created_at DESC")],
                "coverage": "仅同一VPS本地备份；整机或磁盘丢失无法恢复", "retention_count": 7}


@router.post("", status_code=202)
def create_backup() -> dict:
    return tasks.enqueue("backup", {}, "backup:manual:" + db.uid())


@router.get("/{backup_id}/download")
def download_backup(backup_id: str):
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Backup not found")
    file = _folder() / Path(row["filename"]).name
    if not file.is_file():
        raise HTTPException(404, "Backup file unavailable")
    return FileResponse(file, media_type="application/octet-stream", filename=row["filename"], headers={"Cache-Control": "no-store"})
