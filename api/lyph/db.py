"""Short SQLite transactions; no transaction survives network/model awaits."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import sqlite3
import uuid
from .config import settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def transaction():
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def init_schema() -> None:
    with closing(connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log(
          id TEXT PRIMARY KEY, action TEXT NOT NULL, object_id TEXT, details_json TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT 'owner', created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
        """)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(?,?)", ("core-001", now()))


def audit(action: str, object_id: str | None = None, details: dict | None = None) -> None:
    with transaction() as conn:
        conn.execute("INSERT INTO audit_log(id,action,object_id,details_json,created_at) VALUES(?,?,?,?,?)",
                     (uid(), action, object_id, json.dumps(details or {}, ensure_ascii=False), now()))
