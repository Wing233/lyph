from contextlib import closing
from datetime import datetime, timedelta, timezone
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from . import db, security
from .config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["owner authentication"])
COOKIE = "lyph_owner"


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS owner_sessions(
          id TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(ip_digest TEXT NOT NULL, attempted_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(ip_digest,attempted_at);
        """)


def check_origin(request: Request) -> None:
    if request.headers.get("origin", "").rstrip("/") != settings.frontend_origin:
        raise HTTPException(403, "Untrusted or missing Origin")


def _session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    ts = db.now()
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=settings.owner_idle_minutes)).isoformat()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM owner_sessions WHERE id=?", (security.digest(raw),)).fetchone()
        if not row:
            return None
        if row["expires_at"] <= ts or row["last_seen_at"] < threshold:
            conn.execute("DELETE FROM owner_sessions WHERE id=?", (row["id"],))
            return None
        conn.execute("UPDATE owner_sessions SET last_seen_at=? WHERE id=?", (ts, row["id"]))
        return dict(row)


def require_owner(request: Request) -> dict:
    session = _session(request)
    if session is None:
        raise HTTPException(401, "Owner authentication required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        supplied = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(supplied, session["csrf_token"]):
            raise HTTPException(403, "Invalid CSRF token")
    return session


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    check_origin(request)
    ip = security.digest(request.client.host if request.client else "unknown")
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    with db.transaction() as conn:
        conn.execute("DELETE FROM login_attempts WHERE attempted_at<?", (threshold,))
        count = conn.execute("SELECT count(*) FROM login_attempts WHERE ip_digest=?", (ip,)).fetchone()[0]
        if count >= 10:
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "900"})
        conn.execute("INSERT INTO login_attempts VALUES(?,?)", (ip, db.now()))
    if not security.verify_password(settings.owner_password_hash, body.password):
        raise HTTPException(401, "Invalid owner password")
    raw, csrf = security.token(), security.token()
    ts = db.now()
    expires = (datetime.now(timezone.utc) + timedelta(hours=settings.owner_session_hours)).isoformat()
    with db.transaction() as conn:
        conn.execute("DELETE FROM owner_sessions WHERE expires_at<?", (ts,))
        previous = request.cookies.get(COOKIE)
        if previous:
            conn.execute("DELETE FROM owner_sessions WHERE id=?", (security.digest(previous),))
        conn.execute("INSERT INTO owner_sessions VALUES(?,?,?,?,?)", (security.digest(raw), csrf, ts, expires, ts))
    response.set_cookie(COOKIE, raw, max_age=settings.owner_session_hours * 3600, httponly=True,
                        secure=settings.secure_cookies, samesite="strict", path="/")
    db.audit("owner.login", details={})
    return {"authenticated": True, "csrf_token": csrf, "expires_at": expires}


@router.get("/session")
def session(request: Request) -> dict:
    current = _session(request)
    return {"authenticated": bool(current), "csrf_token": current["csrf_token"] if current else None}


@router.post("/logout")
def logout(response: Response, current: dict = Depends(require_owner)) -> dict:
    with db.transaction() as conn:
        conn.execute("DELETE FROM owner_sessions WHERE id=?", (current["id"],))
    response.delete_cookie(COOKIE, path="/")
    db.audit("owner.logout")
    return {"authenticated": False}
