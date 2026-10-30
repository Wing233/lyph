"""Versioned answer caching. Authorization and settlement remain the caller's job."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import unicodedata
from zoneinfo import ZoneInfo

import sqlite_vec
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db
from .auth import require_owner

MAX_ENTRIES = 5000
MAX_BYTES = 128 * 1024 * 1024
DEFAULTS = {"enabled": True, "semantic_enabled": False, "ttl_seconds": 86400,
            "max_entries": MAX_ENTRIES, "max_bytes": MAX_BYTES}
PUBLIC_TOOLS = {"get_public_profile", "search_public_content", "get_public_content", "search_companies", "aggregate_companies"}
SAFE_TOOLS = PUBLIC_TOOLS | {"get_company"}
SAFE_INTENTS = {"profile", "projects", "stack", "interests", "content", "companies", "company", "composite"}
GUARD_VERSION = "slots-v3"
# These cases run through the production router, including rejected/ambiguous grammar.
CALIBRATION_CASES = [
    ("有哪些项目", "看看项目", True),
    ("介绍一下站主", "个人资料", True),
    ("广东前3家企业", "查看广东前3家公司", True),
    ("广东企业", "广西企业", False),
    ("广东前3家企业", "广东前5家企业", False),
    ("广东企业", "今年广东企业", False),
    ("今年广东企业", "去年广东企业", False),
    ("广东企业", "广东不招聘企业", False),
    ("广东企业", "广东正在招聘企业", False),
    ("广东企业按省份分布", "广东企业按城市分布", False),
    ("广东企业", "广东企业只显示图片", False),
    ("广东企业", "适合我的广东企业", False),
    ("看看项目", "查看技术栈", False),
]
router = APIRouter(prefix="/api/v1/admin/chat-cache", tags=["chat cache"], dependencies=[Depends(require_owner)])
_file_hashes: dict[tuple, str] = {}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def init_schema():
    with closing(db.connect()) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_answer_cache (
            key TEXT PRIMARY KEY, payload TEXT NOT NULL, size INTEGER NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_used TEXT NOT NULL)""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_answer_cache)")}
        for name, sql_type in (("entry_id", "TEXT"), ("exact_key", "TEXT"), ("normalized_key", "TEXT"),
                               ("bucket", "TEXT"), ("vector", "BLOB"), ("embedding_tag", "TEXT"), ("hits", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                conn.execute(f"ALTER TABLE chat_answer_cache ADD COLUMN {name} {sql_type}")
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS chat_answer_cache_expiry ON chat_answer_cache(expires_at);
            CREATE INDEX IF NOT EXISTS chat_answer_cache_exact ON chat_answer_cache(exact_key);
            CREATE INDEX IF NOT EXISTS chat_answer_cache_normalized ON chat_answer_cache(normalized_key);
            CREATE INDEX IF NOT EXISTS chat_answer_cache_bucket ON chat_answer_cache(bucket,embedding_tag);
            CREATE TABLE IF NOT EXISTS chat_cache_settings(id INTEGER PRIMARY KEY CHECK(id=1),config_json TEXT NOT NULL,calibration_json TEXT);
            CREATE TABLE IF NOT EXISTS chat_cache_counters(kind TEXT PRIMARY KEY,count INTEGER NOT NULL DEFAULT 0);
        """)
        conn.execute("INSERT OR IGNORE INTO chat_cache_settings(id,config_json) VALUES(1,?)", (_json(DEFAULTS),))


def settings():
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT config_json FROM chat_cache_settings WHERE id=1").fetchone()
    return DEFAULTS | (json.loads(row[0]) if row else {})


def _count(kind):
    with db.transaction() as conn:
        conn.execute("INSERT INTO chat_cache_counters(kind,count) VALUES(?,1) ON CONFLICT(kind) DO UPDATE SET count=count+1", (kind,))


def source_fingerprint() -> str:
    """Source authorization/freshness only; completed reads need no enabled model."""
    from .chat_tools import _enterprise_path
    digest = hashlib.sha256()
    def feed(value):
        digest.update(_json(value).encode())
        digest.update(b"\0")
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        try:
            for table, columns in (
                ("site_settings", "id,revision,payload_json"),
                ("contents", "id,draft_json,published_json,published_version"),
                ("knowledge_sources", "id,content_id,visibility,rag_enabled,revision,active_version,status"),
            ):
                feed(table)
                for row in conn.execute(f"SELECT {columns} FROM {table} ORDER BY id"):
                    feed(list(row))
        finally:
            conn.rollback()
    path = _enterprise_path()
    if path.exists():
        path = path.resolve(strict=True)
        stat = path.stat()
        identity = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        value = _file_hashes.get(identity)
        if value is None:
            with path.open("rb") as stream:
                value = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            if (after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size):
                raise ValueError("企业快照读取时发生变化")
            _file_hashes.clear()
            _file_hashes[identity] = value
        feed([str(path), value])
    else:
        feed("enterprise-unavailable")
    return digest.hexdigest()


def fingerprint(config: dict) -> str:
    """An answer cache also depends on generation, tool and component protocols."""
    from .chat_agent import FOLLOWUPS, INSTRUCTIONS
    from .chat_tools import TOOL_DEFINITIONS
    from .free_json import FREE_JSON_PROTOCOL_VERSION
    from .chat_agent import PUBLIC_FREE_INSTRUCTIONS
    with closing(db.connect()) as conn:
        models = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_configs'").fetchone():
            models = [list(row) for row in conn.execute(
                "SELECT id,purpose,base_url,model,enabled,timeout,prompt,version FROM model_configs ORDER BY id")]
    return _hash([source_fingerprint(),
                  {key: config.get(key) for key in ("id", "version", "model", "prompt", "base_url", "enabled")},
                  models, INSTRUCTIONS, FOLLOWUPS, TOOL_DEFINITIONS, GUARD_VERSION, "rich-v2", FREE_JSON_PROTOCOL_VERSION, PUBLIC_FREE_INSTRUCTIONS])


def cache_key(visitor_id: str, question: str, history: list, fingerprint_value: str) -> str:
    return _hash([visitor_id, unicodedata.normalize("NFC", question.strip()), history, fingerprint_value,
                  datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()])


def build_key(visitor_id, message, history, config, task=None, context=None):
    stamp = fingerprint(config)
    question = unicodedata.normalize("NFC", message.strip())
    task = task or {}
    steps = task.get("steps") or []
    structured = (task.get("intent") in SAFE_INTENTS and bool(task.get("canonical")) and bool(steps)
                  and all(isinstance(step, dict) and step.get("name") in SAFE_TOOLS for step in steps))
    public = (structured and task.get("standalone") is True
              and all(step["name"] in PUBLIC_TOOLS for step in steps))
    scope = "public" if public else "visitor:" + visitor_id
    state = {} if public else {"history": history, "context": context or {}}
    slots = task.get("slots", {})
    signature = {"intent": task.get("intent"), "steps": steps, "slots": slots}
    date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    bucket = _hash([scope, state, stamp, date, signature])
    exact = _hash([bucket, question])
    normalized = _hash([bucket, task["canonical"]]) if structured else None
    return {"key": normalized or exact, "exact_key": exact, "normalized_key": normalized,
            "scope": scope, "fingerprint": stamp, "bucket": bucket, "slots": slots,
            "question": task["canonical"] if public else question, "public": public, "structured": structured}


def _payload(row, kind):
    payload = json.loads(row["payload"])
    return {"payload": payload, "type": kind, "entry_id": row["entry_id"] or row["key"],
            "created_at": row["created_at"], "expires_at": row["expires_at"],
            "data_updated_at": max((block.get("updated_at") or "" for block in payload.get("blocks", [])), default="") or None}


def _touch(row, kind):
    with db.transaction() as conn:
        conn.execute("UPDATE chat_answer_cache SET last_used=?,hits=hits+1 WHERE key=?", (db.now(), row["key"]))
    _count(kind)
    return _payload(row, kind)


def _embedding_config():
    from .models_routes import config_for
    cfg = config_for("embedding")
    return cfg, _hash([cfg["id"], cfg["version"], cfg["model"], cfg.get("base_url")])


def _calibrated(tag):
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT calibration_json FROM chat_cache_settings WHERE id=1").fetchone()
    value = json.loads(row[0] or "{}") if row else {}
    return value.get("passed") is True and value.get("embedding_tag") == tag and value.get("guard_version") == GUARD_VERSION


async def lookup(key_info, config, request_id=None, attempt_id=None):
    try:
        cfg = settings()
        if not cfg["enabled"] or fingerprint(config) != key_info["fingerprint"]:
            return None
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT * FROM chat_answer_cache WHERE exact_key=? AND expires_at>?", (key_info["exact_key"], db.now())).fetchone()
            kind = "exact"
            if not row and key_info.get("normalized_key"):
                row = conn.execute("SELECT * FROM chat_answer_cache WHERE normalized_key=? AND expires_at>?", (key_info["normalized_key"], db.now())).fetchone()
                kind = "normalized"
        if row:
            return _touch(row, kind) if fingerprint(config) == key_info["fingerprint"] else None
        if cfg["semantic_enabled"] and key_info.get("structured"):
            embedding_config, tag = _embedding_config()
            if _calibrated(tag):
                from .knowledge import embed_texts
                vector = (await embed_texts(embedding_config, [key_info["question"]], request_id, attempt_id))[0]
                blob = sqlite_vec.serialize_float32(vector)
                key_info.update(vector=blob, embedding_tag=tag)
                with closing(db.connect()) as conn:
                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                    conn.enable_load_extension(False)
                    rows = conn.execute("""SELECT *,vec_distance_cosine(vector,?) AS distance FROM chat_answer_cache
                        WHERE bucket=? AND embedding_tag=? AND vector IS NOT NULL AND length(vector)=? AND expires_at>?
                        ORDER BY distance LIMIT 2""", (blob, key_info["bucket"], tag, len(blob), db.now())).fetchall()
                if rows and math.isfinite(rows[0]["distance"]):
                    similarity = 1 - rows[0]["distance"]
                    margin = rows[1]["distance"] - rows[0]["distance"] if len(rows) > 1 else 1.0
                    if similarity >= .95 and margin >= .03 and fingerprint(config) == key_info["fingerprint"]:
                        return _touch(rows[0], "semantic")
        _count("miss")
    except Exception:
        return None
    return None


def _strip_runtime(value):
    if isinstance(value, list):
        return [_strip_runtime(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_runtime(item) for key, item in value.items()
                if key not in {"view_id", "visitor_id", "request_id", "conversation_id", "cache", "usage"}}
    return value


def store(key_info, payload, config):
    try:
        cfg = settings()
        if not cfg["enabled"] or fingerprint(config) != key_info["fingerprint"]:
            return None
        clean = _strip_runtime({key: payload.get(key, []) if key != "answer" else payload.get(key, "")
                               for key in ("answer", "citations", "blocks", "suggestions")})
        return _write(key_info["key"], clean, cfg, key_info)
    except Exception:
        return None


def _write(key, payload, cfg, info=None):
    info = info or {}
    value = _json(payload)
    size = len(value.encode()) + len(info.get("vector") or b"")
    if size > cfg["max_bytes"]:
        return None
    now, entry_id = db.now(), db.uid()
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=cfg["ttl_seconds"])).isoformat(timespec="milliseconds")
    with db.transaction() as conn:
        conn.execute("DELETE FROM chat_answer_cache WHERE expires_at<=?", (now,))
        conn.execute("""INSERT OR REPLACE INTO chat_answer_cache
            (key,payload,size,created_at,expires_at,last_used,entry_id,exact_key,normalized_key,bucket,vector,embedding_tag,hits)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)""", (key, value, size, now, expiry, now, entry_id,
                info.get("exact_key"), info.get("normalized_key"), info.get("bucket"), info.get("vector"), info.get("embedding_tag")))
        _evict(conn, cfg)
    return entry_id


def _evict(conn, cfg):
    count, total = conn.execute("SELECT count(*),coalesce(sum(size),0) FROM chat_answer_cache").fetchone()
    for row in conn.execute("SELECT key,size FROM chat_answer_cache ORDER BY last_used,created_at").fetchall():
        if count <= cfg["max_entries"] and total <= cfg["max_bytes"]:
            break
        conn.execute("DELETE FROM chat_answer_cache WHERE key=?", (row["key"],))
        count, total = count - 1, total - row["size"]


def cleanup():
    cfg = settings()
    with db.transaction() as conn:
        removed = conn.execute("DELETE FROM chat_answer_cache WHERE expires_at<=?", (db.now(),)).rowcount
        _evict(conn, cfg)
    return removed


def clear():
    with db.transaction() as conn:
        return conn.execute("DELETE FROM chat_answer_cache").rowcount


def get(key):
    if not settings()["enabled"]:
        return None
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM chat_answer_cache WHERE key=? AND expires_at>?", (key, db.now())).fetchone()
    if not row:
        return None
    result = _touch(row, "exact")
    return result["payload"] | {"cache": {"type": "exact", "generated_at": result["created_at"]}}


def put(key, payload):
    cfg = settings()
    return _write(key, payload, cfg) if cfg["enabled"] else None


class CacheSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    semantic_enabled: bool = False
    ttl_seconds: int = Field(default=86400, ge=1, le=86400)
    max_entries: int = Field(default=5000, ge=1, le=5000)
    max_bytes: int = Field(default=MAX_BYTES, ge=1024, le=MAX_BYTES)


@router.get("")
def stats():
    with closing(db.connect()) as conn:
        count, size = conn.execute("SELECT count(*),coalesce(sum(size),0) FROM chat_answer_cache").fetchone()
        counters = {row["kind"]: row["count"] for row in conn.execute("SELECT * FROM chat_cache_counters")}
        calibration = conn.execute("SELECT calibration_json FROM chat_cache_settings WHERE id=1").fetchone()
        available = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone())
        calls = conn.execute("SELECT status,usage_json FROM model_calls").fetchall() if available else []
    hits = sum(counters.get(kind, 0) for kind in ("exact", "normalized", "semantic"))
    known_tokens, unknown = 0, 0
    for call in calls:
        try:
            usage = json.loads(call["usage_json"] or "null")
            tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        except (TypeError, ValueError):
            tokens = None
        if type(tokens) is int and tokens >= 0:
            known_tokens += tokens
        else:
            unknown += 1
    return {"config": settings(), "entries": count, "bytes": size, "counters": counters,
            "hit_rate": hits / (hits + counters.get("miss", 0)) if hits + counters.get("miss", 0) else None,
            "calibration": json.loads(calibration[0] or "{}") if calibration else {},
            "actual_model_usage": {"available": available, "scope": "all_recorded_calls", "calls": len(calls),
                                   "completed_calls": sum(call["status"] == "completed" for call in calls),
                                   "known_tokens": known_tokens, "unknown_usage_calls": unknown},
            "estimates": {"estimated_saved_tokens": None, "estimated_avoided_generations": hits}}


@router.get("/entries")
def entries():
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT entry_id,size,created_at,expires_at,last_used,hits FROM chat_answer_cache ORDER BY last_used DESC LIMIT 100").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.put("/config")
def configure(body: CacheSettings):
    if body.semantic_enabled:
        try:
            _, tag = _embedding_config()
            allowed = _calibrated(tag)
        except Exception:
            allowed = False
        if not allowed:
            raise HTTPException(422, "请先对当前embedding模型完成语义缓存校准")
    with db.transaction() as conn:
        conn.execute("UPDATE chat_cache_settings SET config_json=? WHERE id=1", (_json(body.model_dump()),))
        _evict(conn, body.model_dump())
    db.audit("chat_cache.configure", details=body.model_dump())
    return stats()


@router.delete("/entries/{entry_id}")
def invalidate(entry_id: str):
    with db.transaction() as conn:
        removed = conn.execute("DELETE FROM chat_answer_cache WHERE entry_id=?", (entry_id,)).rowcount
    if not removed:
        raise HTTPException(404, "缓存条目不存在")
    db.audit("chat_cache.invalidate", entry_id)
    return {"deleted": True}


@router.delete("")
def clear_all():
    removed = clear()
    db.audit("chat_cache.clear", details={"removed": removed})
    return {"deleted": removed}


@router.post("/calibrate")
async def calibrate():
    from .knowledge import embed_texts
    from .chat_views import resolve_task
    cfg, tag = _embedding_config()
    questions = list(dict.fromkeys(text for case in CALIBRATION_CASES for text in case[:2]))
    vectors = await embed_texts(cfg, questions)
    by_question = dict(zip(questions, vectors))
    def similarity(a, b):
        denominator = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
        return sum(x*y for x, y in zip(a, b)) / denominator if denominator else 0
    failures = []
    for index, (left, right, equivalent) in enumerate(CALIBRATION_CASES):
        a, b = resolve_task(left), resolve_task(right)
        same_slots = bool(a and b and a["intent"] == b["intent"] and a["steps"] == b["steps"] and a["slots"] == b["slots"])
        matched = same_slots and similarity(by_question[left], by_question[right]) >= .95
        # A negative pair must also be distinct before embedding, to exercise normalized hits.
        if matched != equivalent or (not equivalent and same_slots):
            failures.append(index)
    result = {"passed": not failures, "embedding_tag": tag, "guard_version": GUARD_VERSION,
              "cases": len(CALIBRATION_CASES), "failed_cases": failures,
              "created_at": db.now(), "threshold": .95, "margin": .03}
    current = settings()
    with db.transaction() as conn:
        conn.execute("UPDATE chat_cache_settings SET calibration_json=? WHERE id=1", (_json(result),))
        if not result["passed"]:
            conn.execute("UPDATE chat_cache_settings SET config_json=? WHERE id=1", (_json(current | {"semantic_enabled": False}),))
    db.audit("chat_cache.calibrate", details=result)
    return result
