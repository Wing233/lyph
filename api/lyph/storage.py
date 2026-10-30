"""Private MinIO objects. An adapter can only be supplied explicitly by tests."""
from contextlib import closing
import hashlib
import io
from pathlib import PurePath
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from minio import Minio
from urllib3 import PoolManager, Timeout
from . import db
from .auth import require_owner
from .config import settings

router = APIRouter(prefix="/api/v1/admin/files", tags=["private media"], dependencies=[Depends(require_owner)])
public_router = APIRouter(prefix="/api/v1/public/media", tags=["published media"])
_adapter = None


def set_test_adapter(adapter) -> None:
    """Only a test fixture should call this; production always uses the SDK."""
    global _adapter
    _adapter = adapter


def _client():
    if _adapter is not None:
        return _adapter
    if not settings.minio_access_key or not settings.minio_secret_key:
        raise HTTPException(503, "Object storage is not configured")
    return Minio(settings.minio_endpoint, access_key=settings.minio_access_key,
                 secret_key=settings.minio_secret_key, secure=settings.minio_secure,
                 http_client=PoolManager(timeout=Timeout(connect=3, read=20), retries=0))


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, object_key TEXT NOT NULL,
          size INTEGER NOT NULL, filename TEXT NOT NULL, content_type TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS file_references(
          file_id TEXT NOT NULL REFERENCES files(id), owner_type TEXT NOT NULL,
          owner_id TEXT NOT NULL, role TEXT NOT NULL,
          PRIMARY KEY(file_id,owner_type,owner_id,role));
        """)


def _clean_name(filename: str) -> str:
    return PurePath(filename.replace("\\", "/")).name.replace("\r", "").replace("\n", "")[:240] or "attachment"


def put(data: bytes, filename: str, content_type: str = "application/octet-stream") -> dict:
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, "File exceeds 20 MiB")
    sha = hashlib.sha256(data).hexdigest()
    with closing(db.connect()) as conn:
        previous = conn.execute("SELECT * FROM files WHERE sha256=?", (sha,)).fetchone()
    if previous:
        if previous["state"] != "active":
            raise HTTPException(409, "Object deletion in progress; retry later")
        return dict(previous)
    client = _client()
    key = f"objects/{sha[:2]}/{sha}"
    try:
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)
        client.put_object(settings.minio_bucket, key, io.BytesIO(data), len(data), content_type=content_type)
    except Exception as exc:
        raise HTTPException(503, "Object storage unavailable") from exc
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO files(id,sha256,object_key,size,filename,content_type,created_at) VALUES(?,?,?,?,?,?,?)",
                     (db.uid(), sha, key, len(data), _clean_name(filename), content_type, db.now()))
        row = conn.execute("SELECT * FROM files WHERE sha256=?", (sha,)).fetchone()
    return dict(row)


def metadata(file_id: str) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM files WHERE id=? AND state='active'", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "File not found")
    return dict(row)


def read(file_id: str) -> bytes:
    meta = metadata(file_id)
    response = None
    try:
        response = _client().get_object(settings.minio_bucket, meta["object_key"])
        return response.read()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "Object storage unavailable") from exc
    finally:
        if response:
            response.close()
            response.release_conn()


def delete(file_id: str) -> dict:
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=? AND state='active'", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "File not found")
        refs = conn.execute("SELECT owner_type,owner_id,role FROM file_references WHERE file_id=?", (file_id,)).fetchall()
        if refs:
            raise HTTPException(409, {"message": "File is still referenced", "references": [dict(r) for r in refs]})
        conn.execute("UPDATE files SET state='deleting' WHERE id=?", (file_id,))
    try:
        _client().remove_object(settings.minio_bucket, row["object_key"])
    except Exception as exc:
        with db.transaction() as conn:
            conn.execute("UPDATE files SET state='active' WHERE id=?", (file_id,))
        raise HTTPException(503, "Object deletion failed") from exc
    with db.transaction() as conn:
        conn.execute("DELETE FROM files WHERE id=?", (file_id,))
    db.audit("file.delete", file_id)
    return {"deleted": True}


def add_reference(conn, file_id: str, owner_type: str, owner_id: str, role: str) -> None:
    row = conn.execute("SELECT id FROM files WHERE id=? AND state='active'", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(409, "Referenced file is unavailable")
    conn.execute("INSERT OR IGNORE INTO file_references VALUES(?,?,?,?)", (file_id, owner_type, owner_id, role))


@router.get("")
def list_files() -> dict:
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM files ORDER BY created_at DESC LIMIT 200").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["references"] = [dict(r) for r in conn.execute("SELECT owner_type,owner_id,role FROM file_references WHERE file_id=?", (row["id"],))]
            items.append(item)
    return {"items": items, "total_bytes": sum(i["size"] for i in items)}


@router.post("")
async def upload(file: UploadFile) -> dict:
    data = await file.read(settings.max_upload_bytes + 1)
    return put(data, file.filename or "attachment", file.content_type or "application/octet-stream")


@router.get("/{file_id}")
def file_detail(file_id: str) -> dict:
    return metadata(file_id)


def _download(file_id: str, public: bool = False) -> Response:
    meta = metadata(file_id)
    inline_types = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}
    media_type = meta["content_type"] if meta["content_type"] in inline_types else "application/octet-stream"
    return Response(read(file_id), media_type=media_type, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline" if media_type in inline_types else "attachment",
    })


@router.get("/{file_id}/download")
def download(file_id: str) -> Response:
    return _download(file_id)


@router.delete("/{file_id}")
def delete_file(file_id: str) -> dict:
    return delete(file_id)


@public_router.get("/{file_id}")
def public_media(file_id: str) -> Response:
    with closing(db.connect()) as conn:
        allowed = conn.execute("""SELECT 1 FROM file_references r JOIN contents c ON r.owner_id=c.id
          WHERE r.file_id=? AND r.owner_type='content' AND r.role='published'
          AND c.published_json IS NOT NULL AND json_extract(c.published_json,'$.visibility')='public'
          AND json_extract(c.draft_json,'$.visibility')='public' LIMIT 1""", (file_id,)).fetchone()
    if not allowed:
        raise HTTPException(404, "Published media not found")
    return _download(file_id, True)
