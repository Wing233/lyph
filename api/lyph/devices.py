"""Independent device credentials and durable, idempotent capture ingestion."""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from . import db, security, storage
from .auth import require_owner
from .config import settings

router = APIRouter(tags=["devices"])
owner = [Depends(require_owner)]


def init_schema():
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
          revoked INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
          interval_seconds INTEGER NOT NULL DEFAULT 300, pending_count INTEGER NOT NULL DEFAULT 0,
          last_seen TEXT, last_capture_at TEXT, last_error TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS captures (
          id TEXT PRIMARY KEY, device_id TEXT NOT NULL, captured_at TEXT NOT NULL, captured_day TEXT NOT NULL,
          source_kind TEXT NOT NULL, text TEXT NOT NULL, object_id TEXT, sha256 TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS captures_day ON captures(captured_day,captured_at);
        CREATE TABLE IF NOT EXISTS capture_object_deletions (
          object_id TEXT PRIMARY KEY, created_at TEXT NOT NULL
        );
        """)


def public_device(row):
    result = dict(row)
    result.pop("token_hash", None)
    return result


def require_device(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "需要独立设备凭证")
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM devices WHERE token_hash=?", (security.digest(auth[7:]),)).fetchone()
    if not row or row["revoked"]:
        raise HTTPException(403, "设备凭证无效或已吊销")
    return dict(row)


def device_config(device):
    return {"device_id": device["id"], "paused": bool(device["paused"]),
            "capture_interval_seconds": device["interval_seconds"], "cache_limit_bytes": 1024 * 1024 * 1024,
            "upload_concurrency": 1, "max_upload_bytes": settings.max_upload_bytes}


class DeviceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platform: Literal["windows", "linux", "ios"]


@router.get("/api/v1/admin/devices", dependencies=owner)
def list_devices():
    with closing(db.connect()) as conn:
        return {"items": [public_device(row) for row in conn.execute("SELECT * FROM devices ORDER BY created_at DESC")]}


@router.post("/api/v1/admin/devices", dependencies=owner, status_code=201)
def create_device(payload: DeviceInput):
    device_id, token = db.uid(), security.token()
    with db.transaction() as conn:
        conn.execute("INSERT INTO devices(id,name,platform,token_hash,created_at) VALUES (?,?,?,?,?)", (device_id, payload.name, payload.platform, security.digest(token), db.now()))
        result = public_device(conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())
    db.audit("device.issue", device_id, {"platform": payload.platform})
    return result | {"token": token}


class DeviceSettings(BaseModel):
    paused: bool
    interval_seconds: int = Field(default=300, ge=30, le=86400)


@router.put("/api/v1/admin/devices/{device_id}", dependencies=owner)
def update_device(device_id: str, payload: DeviceSettings):
    with db.transaction() as conn:
        if conn.execute("UPDATE devices SET paused=?,interval_seconds=? WHERE id=? AND revoked=0", (int(payload.paused), payload.interval_seconds, device_id)).rowcount == 0:
            raise HTTPException(404, "有效设备不存在")
    db.audit("device.settings", device_id, payload.model_dump())
    return {"updated": True}


@router.delete("/api/v1/admin/devices/{device_id}", dependencies=owner)
def revoke_device(device_id: str):
    with db.transaction() as conn:
        if conn.execute("UPDATE devices SET revoked=1 WHERE id=?", (device_id,)).rowcount == 0:
            raise HTTPException(404, "设备不存在")
    db.audit("device.revoke", device_id, {})
    return {"revoked": True}


@router.get("/api/v1/device/config")
def config(device: dict = Depends(require_device)):
    return device_config(device)


class Heartbeat(BaseModel):
    pending_count: int = Field(default=0, ge=0, le=1_000_000)
    last_capture_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=160)


@router.post("/api/v1/device/heartbeat")
def heartbeat(payload: Heartbeat, device: dict = Depends(require_device)):
    if payload.last_capture_at is not None and payload.last_capture_at.tzinfo is None:
        raise HTTPException(422, "采集时间需要时区")
    with db.transaction() as conn:
        conn.execute("UPDATE devices SET last_seen=?,pending_count=?,last_capture_at=?,last_error=? WHERE id=? AND revoked=0", (db.now(), payload.pending_count, payload.last_capture_at.astimezone(timezone.utc).isoformat() if payload.last_capture_at else None, payload.last_error, device["id"]))
    return device_config(device)


def image_type(data: bytes):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise HTTPException(422, "图片必须是 PNG、JPEG 或 WebP")


@router.post("/api/v1/device/captures", status_code=201)
async def upload_capture(capture_id: str = Form(...), captured_at: str = Form(...),
                         source_kind: Literal["screenshot", "note", "photo"] = Form(...),
                         text: str = Form(""), file: UploadFile | None = File(None),
                         device: dict = Depends(require_device)):
    if not capture_id or len(capture_id) > 100 or len(text) > 16000:
        raise HTTPException(422, "采集标识或文本长度无效")
    try:
        stamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        stamp = stamp.astimezone(timezone.utc)
    except ValueError:
        raise HTTPException(422, "captured_at 必须是带时区 ISO 时间") from None
    normalized = stamp.isoformat()
    with closing(db.connect()) as conn:
        previous = conn.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
    if previous:
        if previous["device_id"] != device["id"]:
            raise HTTPException(409, "采集标识已使用")
        if previous["captured_at"] != normalized or previous["source_kind"] != source_kind or previous["text"] != text:
            raise HTTPException(409, "capture_id 不可对应不同事件")
        if file:
            duplicate_data = await file.read(settings.max_upload_bytes + 1)
            if len(duplicate_data) > settings.max_upload_bytes:
                raise HTTPException(413, "图片超过 20MiB")
            if previous["sha256"] != hashlib.sha256(duplicate_data).hexdigest():
                raise HTTPException(409, "capture_id 不可对应不同图片")
        return dict(previous) | {"duplicate": True}
    if source_kind in ("screenshot", "photo") and file is None or source_kind == "note" and not text.strip():
        raise HTTPException(422, "图片事件需要文件，速记需要文本")
    media = None
    if file:
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, "图片超过 20MiB")
        media = await asyncio.to_thread(storage.put, data, file.filename or "capture.png", image_type(data))
    with db.transaction() as conn:
        current = conn.execute("SELECT revoked FROM devices WHERE id=?", (device["id"],)).fetchone()
        if current["revoked"]:
            raise HTTPException(403, "设备已吊销")
        previous = conn.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
        if previous:
            if previous["device_id"] != device["id"]:
                raise HTTPException(409, "采集标识已使用")
            if previous["captured_at"] != normalized or previous["source_kind"] != source_kind or previous["text"] != text or previous["sha256"] != (media["sha256"] if media else None):
                raise HTTPException(409, "capture_id 不可对应不同事件")
            return dict(previous) | {"duplicate": True}
        conn.execute("INSERT INTO captures VALUES (?,?,?,?,?,?,?,?,?)", (capture_id, device["id"], normalized,
                     stamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(), source_kind, text,
                     media["id"] if media else None, media["sha256"] if media else None, db.now()))
        if media:
            storage.add_reference(conn, media["id"], "capture", capture_id, "original")
        conn.execute("UPDATE devices SET last_seen=?,last_capture_at=? WHERE id=?", (db.now(), normalized, device["id"]))
        result = dict(conn.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone())
    return result | {"duplicate": False}


@router.get("/api/v1/admin/captures", dependencies=owner)
def captures(date: str | None = None):
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM captures WHERE (? IS NULL OR captured_day=?) ORDER BY captured_at DESC LIMIT 500", (date, date)).fetchall()
    return {"items": [dict(row) for row in rows]}


def cleanup_captures(days: int = 7) -> dict:
    """Expire screenshot objects; photos and shared/media references stay protected."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.transaction() as conn:
        expired = conn.execute("SELECT id,object_id FROM captures WHERE source_kind='screenshot' AND object_id IS NOT NULL AND captured_at<?", (cutoff,)).fetchall()
        for row in expired:
            conn.execute("DELETE FROM file_references WHERE owner_type='capture' AND owner_id=?", (row["id"],))
            conn.execute("INSERT OR IGNORE INTO capture_object_deletions VALUES (?,?)", (row["object_id"], db.now()))
            conn.execute("UPDATE captures SET object_id=NULL WHERE id=?", (row["id"],))
        raw_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        # Photo originals stay attached to their event; only unreferenced raw events expire here.
        raw_deleted = conn.execute("DELETE FROM captures WHERE object_id IS NULL AND captured_at<?", (raw_cutoff,)).rowcount
        pending = [row[0] for row in conn.execute("SELECT object_id FROM capture_object_deletions")]
    removed, failed = 0, 0
    for object_id in pending:
        try:
            storage.delete(object_id)
            removed += 1
        except HTTPException as exc:
            if exc.status_code not in (404, 409):
                failed += 1
                continue
            # Still referenced by a photo/content/knowledge source: its owning lifecycle controls it.
        with db.transaction() as conn:
            conn.execute("DELETE FROM capture_object_deletions WHERE object_id=?", (object_id,))
    return {"expired_screenshots": len(expired), "deleted_capture_objects": removed,
            "capture_object_delete_failures": failed, "expired_capture_events": raw_deleted}
