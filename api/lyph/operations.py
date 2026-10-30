"""Minimal first-party event analytics and owner-only execution observation."""
from __future__ import annotations

import ipaddress
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, Field, field_validator

from . import auth, db, security
from .chat import COOKIE, check_origin, public_request, rate_limit, visitor_for, redact_grant_secrets

router = APIRouter(tags=["observation"])
owner = [Depends(auth.require_owner)]


def init_schema():
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS visit_events (
          occurrence_id TEXT PRIMARY KEY, visitor_id TEXT NOT NULL, session_id TEXT NOT NULL,
          event_name TEXT NOT NULL, path TEXT NOT NULL, content_id TEXT, device TEXT NOT NULL,
          source TEXT NOT NULL, created_at TEXT NOT NULL, ip TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS visit_date ON visit_events(created_at,event_name);
        CREATE TABLE IF NOT EXISTS chat_feedback (
          id TEXT PRIMARY KEY, request_id TEXT NOT NULL, visitor_id TEXT NOT NULL,
          rating TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(request_id,visitor_id)
        );
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(visit_events)")}
        if "ip" not in columns:
            conn.execute("ALTER TABLE visit_events ADD COLUMN ip TEXT NOT NULL DEFAULT ''")


class EventInput(BaseModel):
    occurrence_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    event_name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    path: str = Field(max_length=1000)
    content_id: str | None = Field(default=None, max_length=100)
    device: str = Field(default="unknown", max_length=30)
    source: str = Field(default="direct", max_length=100)


def client_ip(request: Request) -> str:
    # nginx always overwrites X-Real-IP with the peer address; the API port is not public.
    raw = (request.headers.get("x-real-ip") or "").strip()
    if not raw and request.client:
        raw = request.client.host
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return ""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


@router.post("/api/v1/public/events")
def record_event(payload: EventInput, request: Request):
    check_origin(request)
    # Authentication/environment are authoritative. Client cannot opt back into owner tracking.
    if auth._session(request):
        return {"accepted": False, "reason": "owner"}
    if request.headers.get("x-lyph-preview") or request.headers.get("x-lyph-test"):
        return {"accepted": False, "reason": "preview-or-test"}
    path = urlsplit(payload.path).path
    if not path.startswith("/") or path.startswith(("/admin", "/api", "/preview", "/collector", "/_next")) or "\\" in path:
        return {"accepted": False, "reason": "excluded-path"}
    # Discard all query values, referrer URLs and unknown event semantics.
    if payload.event_name not in {"page_view", "content_view", "nav.blog.open", "nav.documents.open", "nav.journal.open", "nav.chat.open", "content.open", "collection.toggle", "chat.submit", "contact.open", "menu.unlock"}:
        raise HTTPException(422, "事件名称未注册")
    visitor = visitor_for(request)
    rate_limit("analytics", visitor["id"], 120)
    ip = client_ip(request)
    with db.transaction() as conn:
        if payload.content_id:
            row = conn.execute("SELECT 1 FROM contents WHERE id=? AND published_json IS NOT NULL AND json_extract(published_json,'$.visibility')='public' AND json_extract(draft_json,'$.visibility')='public'", (payload.content_id,)).fetchone()
            if not row:
                return {"accepted": False, "reason": "unpublished-content"}
        previous = conn.execute("SELECT session_id,created_at FROM visit_events WHERE visitor_id=? ORDER BY created_at DESC LIMIT 1", (visitor["id"],)).fetchone()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        session_id = previous["session_id"] if previous and previous["created_at"] >= cutoff else db.uid()
        created = conn.execute("""INSERT OR IGNORE INTO visit_events
                     (occurrence_id,visitor_id,session_id,event_name,path,content_id,device,source,created_at,ip)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""", (payload.occurrence_id, visitor["id"], session_id, payload.event_name, path[:500], payload.content_id,
                     payload.device if payload.device in ("desktop", "tablet", "mobile", "unknown") else "unknown",
                     payload.source if payload.source in ("direct", "search", "social", "other") else "other", db.now(), ip)).rowcount
    return {"accepted": True, "duplicate": not bool(created)}


def interval(start: str | None, end: str | None):
    zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    try:
        first = datetime.fromisoformat(start).date() if start else today - timedelta(days=6)
        last = datetime.fromisoformat(end).date() if end else today
    except ValueError:
        raise HTTPException(422, "日期区间无效") from None
    if first > last or (last - first).days > 366:
        raise HTTPException(422, "日期区间应在一年内且起点不晚于终点")
    left = datetime.combine(first, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
    right = datetime.combine(last + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
    return first.isoformat(), last.isoformat(), left, right


@router.get("/api/v1/admin/analytics", dependencies=owner)
def analytics(start: str | None = None, end: str | None = None, from_date: str | None = Query(None, alias="from"), to_date: str | None = Query(None, alias="to")):
    first, last, left, right = interval(start or from_date, end or to_date)
    with closing(db.connect()) as conn:
        totals = dict(conn.execute("""SELECT count(*) AS pv,count(DISTINCT visitor_id) AS uv,
            count(DISTINCT session_id) AS sessions FROM visit_events WHERE created_at>=? AND created_at<? AND event_name='page_view'""", (left, right)).fetchone())
        pages = [dict(row) | {"ctr": None} for row in conn.execute("""SELECT path,
            sum(event_name='page_view') AS pv,count(DISTINCT CASE WHEN event_name='page_view' THEN visitor_id END) AS uv,
            sum(event_name='content_view') AS content_views,sum(event_name NOT IN ('page_view','content_view')) AS clicks,
            count(DISTINCT CASE WHEN event_name NOT IN ('page_view','content_view') THEN visitor_id END) AS click_users
            FROM visit_events WHERE created_at>=? AND created_at<? GROUP BY path ORDER BY pv DESC LIMIT 500""", (left, right))]
        daily = [dict(row) for row in conn.execute("""SELECT date(created_at,'+8 hours') AS date,
            count(*) AS pv,count(DISTINCT visitor_id) AS uv FROM visit_events
            WHERE created_at>=? AND created_at<? AND event_name='page_view' GROUP BY date ORDER BY date""", (left, right))]
        clicks = conn.execute("SELECT count(*) FROM visit_events WHERE created_at>=? AND created_at<? AND event_name NOT IN ('page_view','content_view')", (left, right)).fetchone()[0]
        events = [dict(row) for row in conn.execute("SELECT * FROM visit_events WHERE created_at>=? AND created_at<? ORDER BY created_at DESC LIMIT 200", (left, right))]
    return {"start": first, "end": last, "timezone": "Asia/Shanghai", **totals,
            "clicks": clicks, "ctr": None, "pages": pages, "daily": daily, "events": events}


@router.get("/api/v1/admin/conversations", dependencies=owner)
def conversations():
    with closing(db.connect()) as conn:
        rows = conn.execute("""SELECT conversation_id,visitor_id,min(created_at) AS created_at,max(created_at) AS last_active,
            count(*) AS requests,sum(status='completed') AS completed,sum(status='failed') AS failed
            FROM chat_requests GROUP BY conversation_id,visitor_id ORDER BY last_active DESC LIMIT 100""").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/api/v1/admin/conversations/{conversation_id}", dependencies=owner)
def conversation_detail(conversation_id: str):
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM chat_requests WHERE conversation_id=? ORDER BY created_at", (conversation_id,)).fetchall()
    if not rows:
        raise HTTPException(404, "会话不存在")
    return {"conversation_id": conversation_id, "items": [public_request(row) for row in rows]}


@router.get("/api/v1/admin/requests", dependencies=owner)
def requests(status: str | None = None):
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM chat_requests WHERE (? IS NULL OR status=?) ORDER BY created_at DESC LIMIT 200", (status, status)).fetchall()
    return {"items": [public_request(row) for row in rows]}


@router.get("/api/v1/admin/requests/{request_id}", dependencies=owner)
def request_detail(request_id: str):
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM chat_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(404, "请求不存在")
        attempts = [dict(item) for item in conn.execute("SELECT * FROM chat_attempts WHERE request_id=? ORDER BY number", (request_id,))]
        calls = [dict(item) for item in conn.execute("SELECT * FROM model_calls WHERE request_id=? ORDER BY created_at", (request_id,))]
        feedback = [dict(item) for item in conn.execute("SELECT * FROM chat_feedback WHERE request_id=?", (request_id,))]
    for call in calls:
        call["usage"] = json.loads(call.pop("usage_json")) if call["usage_json"] else None
        call.pop("usage_json", None)
    return public_request(row) | {"attempts": attempts, "calls": calls, "feedback": feedback}


class FeedbackInput(BaseModel):
    rating: str
    note: str = Field(default="", max_length=1000)

    @field_validator("note")
    @classmethod
    def redact_note(cls, value):
        return redact_grant_secrets(value)


@router.post("/api/v1/chat/requests/{request_id}/feedback")
def feedback(request_id: str, payload: FeedbackInput, request: Request):
    check_origin(request)
    visitor = visitor_for(request)
    if payload.rating not in ("positive", "negative"):
        raise HTTPException(422, "反馈类型无效")
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM chat_requests WHERE id=? AND visitor_id=?", (request_id, visitor["id"])).fetchone():
            raise HTTPException(404, "请求不存在")
        conn.execute("""INSERT INTO chat_feedback VALUES (?,?,?,?,?,?) ON CONFLICT(request_id,visitor_id)
            DO UPDATE SET rating=excluded.rating,note=excluded.note,created_at=excluded.created_at""",
            (db.uid(), request_id, visitor["id"], payload.rating, payload.note, db.now()))
    return {"saved": True}


@router.get("/api/v1/admin/audit", dependencies=owner)
def audit():
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 200").fetchall()
    return {"items": [dict(row) | {"details": json.loads(row["details_json"])} for row in rows]}


@router.get("/api/v1/admin/overview", dependencies=owner)
def overview():
    with closing(db.connect()) as conn:
        counts = {"contents": conn.execute("SELECT count(*) FROM contents").fetchone()[0],
                  "published": conn.execute("SELECT count(*) FROM contents WHERE published_json IS NOT NULL").fetchone()[0],
                  "knowledge": conn.execute("SELECT count(*) FROM knowledge_sources").fetchone()[0],
                  "failed_tasks": conn.execute("SELECT count(*) FROM tasks WHERE status='failed'").fetchone()[0],
                  "pending_daily": conn.execute("SELECT count(*) FROM daily_runs WHERE status='ready' AND content_id IN (SELECT id FROM contents WHERE published_json IS NULL)").fetchone()[0],
                  "storage_bytes": conn.execute("SELECT coalesce(sum(size),0) FROM files").fetchone()[0]}
    return counts
