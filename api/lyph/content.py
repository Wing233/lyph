from contextlib import closing
from datetime import date as Date, datetime
import json
import re
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from . import db, storage
from .auth import require_owner

router = APIRouter(prefix="/api/v1/admin/contents", tags=["content"], dependencies=[Depends(require_owner)])
public_router = APIRouter(prefix="/api/v1/public", tags=["public content"])
PUBLIC_WHERE = "published_json IS NOT NULL AND json_extract(published_json,'$.visibility')='public' AND json_extract(draft_json,'$.visibility')='public'"


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["blog", "document", "manual", "automatic"] = "blog"
    title: str = Field(default="未命名草稿", max_length=300)
    summary: str = Field(default="", max_length=2000)
    markdown: str = Field(default="", max_length=500000)
    date: str = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    visibility: Literal["public", "private"] = "private"
    rag_enabled: bool = False
    category: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=30)
    media_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        return Date.fromisoformat(value).isoformat()

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values: list[str]) -> list[str]:
        if any(len(value) > 100 for value in values):
            raise ValueError("Tag exceeds 100 characters")
        return list(dict.fromkeys(values))


class UpdateDraft(Draft):
    revision: int = Field(ge=1)


class Revision(BaseModel):
    revision: int = Field(ge=1)


class Restore(Revision):
    version_id: str


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS contents(
          id TEXT PRIMARY KEY, type TEXT NOT NULL, draft_json TEXT NOT NULL,
          published_json TEXT, published_version TEXT, revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, origin_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS content_versions(
          id TEXT PRIMARY KEY, content_id TEXT NOT NULL REFERENCES contents(id), number INTEGER NOT NULL,
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL, reason TEXT NOT NULL,
          UNIQUE(content_id,number));
        CREATE INDEX IF NOT EXISTS idx_content_updated ON contents(updated_at);
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(contents)")}
        if "origin_key" not in columns:
            conn.execute("ALTER TABLE contents ADD COLUMN origin_key TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_content_origin ON contents(origin_key)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(?,?)", ("content-002", db.now()))


def _row(conn, content_id: str):
    row = conn.execute("SELECT * FROM contents WHERE id=?", (content_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Content not found")
    return row


def _check_revision(row, revision: int) -> None:
    if row["revision"] != revision:
        raise HTTPException(409, {"message": "Content changed; reload before saving", "revision": row["revision"]})


def _decode(row, summary_only: bool = False) -> dict:
    item = json.loads(row["draft_json"])
    item.update(id=row["id"], revision=row["revision"], created_at=row["created_at"], updated_at=row["updated_at"],
                published=bool(row["published_json"]), published_version=row["published_version"])
    if summary_only:
        item.pop("markdown", None)
    else:
        item["published_snapshot"] = json.loads(row["published_json"]) if row["published_json"] else None
    return item


def get_content(content_id: str) -> dict:
    with closing(db.connect()) as conn:
        return _decode(_row(conn, content_id))


def _files(payload: dict) -> set[str]:
    # Canonical media URLs are owned by our backend; unknown remote links never gain access.
    urls = re.findall(r"/api/v1/(?:public/media/|admin/files/)([a-f0-9]{32})", payload["markdown"])
    return set(payload.get("media_ids", [])) | set(urls)


def _references(conn, content_id: str, role: str, payload: dict) -> None:
    conn.execute("DELETE FROM file_references WHERE owner_type='content' AND owner_id=? AND role=?", (content_id, role))
    for file_id in _files(payload):
        storage.add_reference(conn, file_id, "content", content_id, role)


def _version(conn, row, payload: dict, reason: str) -> str:
    number = conn.execute("SELECT coalesce(max(number),0)+1 FROM content_versions WHERE content_id=?", (row["id"],)).fetchone()[0]
    version_id = db.uid()
    conn.execute("INSERT INTO content_versions VALUES(?,?,?,?,?,?)",
                 (version_id, row["id"], number, json.dumps(payload, ensure_ascii=False), db.now(), reason))
    _references(conn, row["id"], "version:" + version_id, payload)
    return version_id


def create_draft(payload: dict, origin_key: str | None = None) -> dict:
    clean = Draft.model_validate(payload).model_dump()
    content_id, ts = db.uid(), db.now()
    with db.transaction() as conn:
        if origin_key:
            existing = conn.execute("SELECT * FROM contents WHERE origin_key=?", (origin_key,)).fetchone()
            if existing:
                return _decode(existing)
        conn.execute("INSERT INTO contents(id,type,draft_json,created_at,updated_at,origin_key) VALUES(?,?,?,?,?,?)",
                     (content_id, clean["type"], json.dumps(clean, ensure_ascii=False), ts, ts, origin_key))
        _references(conn, content_id, "draft", clean)
    db.audit("content.create", content_id, {"type": clean["type"]})
    return get_content(content_id)


def update_draft(content_id: str, payload: dict, revision: int) -> dict:
    clean = Draft.model_validate(payload).model_dump()
    with db.transaction() as conn:
        row = _row(conn, content_id)
        _check_revision(row, revision)
        old = json.loads(row["draft_json"])
        if clean == old:
            return _decode(row)
        _version(conn, row, old, "before-save")
        _references(conn, content_id, "draft", clean)
        conn.execute("UPDATE contents SET type=?,draft_json=?,revision=revision+1,updated_at=? WHERE id=?",
                     (clean["type"], json.dumps(clean, ensure_ascii=False), db.now(), content_id))
    db.audit("content.save", content_id, {"visibility": clean["visibility"], "rag_enabled": clean["rag_enabled"]})
    return get_content(content_id)


def _sync_knowledge(content_id: str) -> None:
    # This callback is wired during application startup, avoiding a circular import.
    if _publication_callback is not None:
        _publication_callback(content_id)


_publication_callback = None


def set_publication_callback(callback) -> None:
    global _publication_callback
    _publication_callback = callback


def publish_content(content_id: str, revision: int) -> dict:
    with db.transaction() as conn:
        row = _row(conn, content_id)
        _check_revision(row, revision)
        payload = json.loads(row["draft_json"])
        if not payload["title"].strip() or not payload["markdown"].strip():
            raise HTTPException(422, "A title and body are required to publish")
        version_id = _version(conn, row, payload, "publish")
        _references(conn, content_id, "published", payload)
        conn.execute("UPDATE contents SET published_json=?,published_version=?,revision=revision+1,updated_at=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False), version_id, db.now(), content_id))
    db.audit("content.publish", content_id, {"version_id": version_id, "visibility": payload["visibility"]})
    _sync_knowledge(content_id)
    return get_content(content_id)


def withdraw_content(content_id: str, revision: int) -> dict:
    with db.transaction() as conn:
        row = _row(conn, content_id)
        _check_revision(row, revision)
        conn.execute("UPDATE contents SET published_json=NULL,published_version=NULL,revision=revision+1,updated_at=? WHERE id=?", (db.now(), content_id))
        conn.execute("DELETE FROM file_references WHERE owner_type='content' AND owner_id=? AND role='published'", (content_id,))
    db.audit("content.withdraw", content_id)
    _sync_knowledge(content_id)
    return get_content(content_id)


@router.get("")
def admin_list(type: str | None = None) -> dict:
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM contents WHERE (? IS NULL OR type=?) ORDER BY updated_at DESC LIMIT 500", (type, type)).fetchall()
    return {"items": [_decode(row, True) for row in rows]}


@router.post("")
def create(body: Draft) -> dict:
    return create_draft(body.model_dump())


@router.get("/{content_id}")
def detail(content_id: str) -> dict:
    return get_content(content_id)


@router.put("/{content_id}")
def update(content_id: str, body: UpdateDraft) -> dict:
    return update_draft(content_id, body.model_dump(exclude={"revision"}), body.revision)


@router.post("/{content_id}/publish")
def publish(content_id: str, body: Revision) -> dict:
    return publish_content(content_id, body.revision)


@router.post("/{content_id}/withdraw")
def withdraw(content_id: str, body: Revision) -> dict:
    return withdraw_content(content_id, body.revision)


@router.get("/{content_id}/versions")
def versions(content_id: str) -> dict:
    with closing(db.connect()) as conn:
        _row(conn, content_id)
        rows = conn.execute("SELECT * FROM content_versions WHERE content_id=? ORDER BY number DESC", (content_id,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        items.append(item)
    return {"items": items}


@router.post("/{content_id}/restore")
def restore(content_id: str, body: Restore) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT payload_json FROM content_versions WHERE id=? AND content_id=?", (body.version_id, content_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Content version not found")
    result = update_draft(content_id, json.loads(row["payload_json"]), body.revision)
    db.audit("content.restore-draft", content_id, {"version_id": body.version_id})
    return result


def _public_item(row, summary_only: bool) -> dict:
    item = json.loads(row["published_json"])
    item.update(id=row["id"], version=row["published_version"])
    if summary_only:
        item.pop("markdown", None)
        item.pop("media_ids", None)
    return item


@public_router.get("/contents")
def public_list(type: str | None = None, month: str | None = None, q: str = Query(default="", max_length=200),
                category: str | None = None, date: str | None = None,
                page: int = Query(default=1, ge=1, le=100000), batch: int = Query(default=1, ge=1, le=4)) -> dict:
    conditions, params = [PUBLIC_WHERE], []
    if type == "journal":
        conditions.append("json_extract(published_json,'$.type') IN ('manual','automatic')")
    elif type:
        conditions.append("json_extract(published_json,'$.type')=?")
        params.append(type)
    if month:
        conditions.append("substr(json_extract(published_json,'$.date'),1,7)=?")
        params.append(month)
    if date:
        conditions.append("json_extract(published_json,'$.date')=?")
        params.append(date)
    if category:
        conditions.append("json_extract(published_json,'$.category')=?")
        params.append(category)
    if q:
        conditions.append("(instr(lower(json_extract(published_json,'$.title')),lower(?))>0 OR instr(lower(json_extract(published_json,'$.summary')),lower(?))>0 OR instr(lower(json_extract(published_json,'$.markdown')),lower(?))>0)")
        params.extend([q, q, q])
    where = " AND ".join(conditions)
    offset = (page - 1) * 48 + (batch - 1) * 12
    with closing(db.connect()) as conn:
        total = conn.execute("SELECT count(*) FROM contents WHERE " + where, params).fetchone()[0]
        rows = conn.execute("SELECT * FROM contents WHERE " + where + " ORDER BY json_extract(published_json,'$.date') DESC,created_at DESC,id DESC LIMIT 12 OFFSET ?", [*params, offset]).fetchall()
        categories = [r[0] for r in conn.execute("SELECT DISTINCT json_extract(published_json,'$.category') FROM contents WHERE " + PUBLIC_WHERE + " AND json_extract(published_json,'$.type')='document' ORDER BY 1") if r[0]]
        months = [r[0] for r in conn.execute("SELECT DISTINCT substr(json_extract(published_json,'$.date'),1,7) FROM contents WHERE " + PUBLIC_WHERE + " ORDER BY 1 DESC")]
    return {"items": [_public_item(row, True) for row in rows], "total": total, "page": page, "batch": batch,
            "has_more": offset + len(rows) < min(page * 48, total), "has_next_page": page * 48 < total,
            "categories": categories, "months": months}


@public_router.get("/contents/{content_id}")
def public_detail(content_id: str) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM contents WHERE " + PUBLIC_WHERE + " AND id=?", (content_id,)).fetchone()
        if row is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", content_id):
            row = conn.execute("SELECT * FROM contents WHERE " + PUBLIC_WHERE + " AND json_extract(published_json,'$.date')=? ORDER BY created_at,id LIMIT 1", (content_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Published content not found")
    return _public_item(row, False)
