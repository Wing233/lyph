from contextlib import closing
import json
import re
import sqlite3
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from . import db
from .auth import require_owner

router = APIRouter(prefix="/api/v1/admin", tags=["navigation"], dependencies=[Depends(require_owner)])
public_router = APIRouter(prefix="/api/v1/public", tags=["navigation"])

DEFAULT_NAVIGATION = (
    {"id": "about", "label": "关于", "href": "#intro", "event_name": "", "sort_order": 10},
    {"id": "projects", "label": "项目", "href": "/projects", "event_name": "", "sort_order": 20},
    {"id": "stack", "label": "技术栈", "href": "/stack", "event_name": "", "sort_order": 30},
    {"id": "interests", "label": "爱好", "href": "/interests", "event_name": "", "sort_order": 40},
    {"id": "journal", "label": "日报", "href": "/journal", "event_name": "nav.journal.open", "sort_order": 50},
    {"id": "chat", "label": "对话", "href": "#chat", "event_name": "nav.chat.open", "sort_order": 60},
    {"id": "collection", "label": "收藏", "href": "/collection", "event_name": "", "sort_order": 70},
)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EVENT = re.compile(r"^[A-Za-z0-9._-]{0,80}$")
_EXTERNAL = re.compile(r"^https?://", re.I)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _safe_href(value: Any) -> str:
    href = str(value or "").strip()
    if len(href) > 500 or any(char in href for char in ("\\", "\n", "\r", "\t")):
        return ""
    if href.startswith("#") and len(href) > 1:
        return href
    if href.startswith("/") and not href.startswith("//"):
        return href
    if _EXTERNAL.match(href):
        return href
    return ""


def _normalize(item: dict, *, generated_id: bool = False) -> dict:
    if not isinstance(item, dict):
        raise ValueError("导航项必须是对象")
    label = str(item.get("label") or "").strip() or str(item.get("name") or "").strip()
    if not label or len(label) > 40 or any(char in label for char in ("\n", "\r", "\t")):
        raise ValueError("导航名称需为 1～40 个字符且不能包含换行")
    href = _safe_href(item.get("href") or item.get("path"))
    if not href:
        raise ValueError("导航地址必须是站内路径、# 锚点或 http(s) 链接")
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        if not generated_id:
            raise ValueError("导航 id 不能为空")
        item_id = f"nav-{db.uid()[:12]}"
    if not _ID.fullmatch(item_id):
        raise ValueError("导航 id 只能包含字母、数字、下划线和连字符")
    event_name = str(item.get("event_name") or "").strip()
    if not _EVENT.fullmatch(event_name):
        raise ValueError("event_name 只能包含字母、数字、点、下划线和连字符")
    try:
        bot_size = float(item.get("bot_size", 1))
        sort_order = int(item.get("sort_order", 1000))
    except (TypeError, ValueError) as exc:
        raise ValueError("bot_size 或 sort_order 格式不正确") from exc
    if not 0.4 <= bot_size <= 2.5:
        raise ValueError("bot_size 需在 0.4～2.5 之间")
    if not -100000 <= sort_order <= 100000:
        raise ValueError("sort_order 超出允许范围")
    return {
        "id": item_id,
        "label": label,
        "href": href,
        "event_name": event_name,
        "visible": _as_bool(item.get("visible", True)),
        "show_as_bot": _as_bool(item.get("show_as_bot", True)),
        "bot_size": round(bot_size, 2),
        "sort_order": sort_order,
    }


def normalize_items(items: Any) -> list[dict]:
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("导航项需为数组且不超过 100 项")
    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        normalized = _normalize(item, generated_id=True)
        if normalized["id"] in seen:
            raise ValueError("导航 id 不能重复")
        seen.add(normalized["id"])
        if not isinstance(item, dict) or "sort_order" not in item:
            normalized["sort_order"] = (index + 1) * 10
        result.append(normalized)
    return result


def _from_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "label": row["label"],
        "href": row["href"],
        "event_name": row["event_name"],
        "visible": bool(row["visible"]),
        "show_as_bot": bool(row["show_as_bot"]),
        "bot_size": min(2.5, max(0.4, float(row["bot_size"] or 1))),
        "sort_order": int(row["sort_order"] or 0),
    }


def _rows(*, public: bool = False) -> list[sqlite3.Row]:
    query = "SELECT * FROM navigation_items"
    if public:
        query += " WHERE visible=1"
    query += " ORDER BY sort_order, rowid"
    try:
        with closing(db.connect()) as conn:
            return conn.execute(query).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


def list_items() -> list[dict]:
    return [_from_row(row) for row in _rows()]


def public_items() -> list[dict]:
    result = []
    for row in _rows(public=True):
        item = _from_row(row)
        # 直接写库的行也要经过同一套校验，避免超长文本和 javascript: 等链接进入页面。
        if (
            not _ID.fullmatch(item["id"])
            or not item["label"]
            or len(item["label"]) > 40
            or any(char in item["label"] for char in ("\n", "\r", "\t"))
            or not _safe_href(item["href"])
        ):
            continue
        result.append(item)
    return result


def _legacy_items(conn: sqlite3.Connection) -> list[dict]:
    try:
        row = conn.execute("SELECT payload_json FROM site_settings WHERE id=1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise
    if not row:
        return []
    try:
        navigation = json.loads(row["payload_json"]).get("navigation")
    except (AttributeError, TypeError, ValueError):
        return []
    if not isinstance(navigation, list):
        return []
    result = []
    seen: set[str] = set()
    for index, entry in enumerate(navigation[:100]):
        if not isinstance(entry, dict):
            continue
        try:
            item = _normalize({
                **entry,
                "label": entry.get("label") or entry.get("name"),
                "href": entry.get("href") or entry.get("path"),
                "sort_order": entry.get("sort_order", (index + 1) * 10),
            }, generated_id=True)
        except ValueError:
            continue
        if item["id"] in seen:
            item["id"] = f"nav-{db.uid()[:12]}"
        seen.add(item["id"])
        result.append(item)
    return result


def _insert(conn: sqlite3.Connection, item: dict) -> None:
    now = db.now()
    conn.execute(
        """INSERT INTO navigation_items(
          id,label,href,event_name,visible,show_as_bot,bot_size,sort_order,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (item["id"], item["label"], item["href"], item["event_name"], int(item["visible"]),
         int(item["show_as_bot"]), item["bot_size"], item["sort_order"], now, now),
    )


def write_items(conn: sqlite3.Connection, items: list[dict]) -> None:
    conn.execute("DELETE FROM navigation_items")
    for item in items:
        _insert(conn, item)


def _invalidate_settings_snapshot(conn: sqlite3.Connection) -> None:
    # Legacy settings writes can replace navigation. Advance their shared revision
    # in the same transaction so a stale JSON snapshot cannot overwrite this edit.
    conn.execute("UPDATE site_settings SET revision=revision+1,updated_at=? WHERE id=1", (db.now(),))


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS navigation_items(
          id TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          href TEXT NOT NULL,
          event_name TEXT NOT NULL DEFAULT '',
          visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0,1)),
          show_as_bot INTEGER NOT NULL DEFAULT 1 CHECK(show_as_bot IN (0,1)),
          bot_size REAL NOT NULL DEFAULT 1.0,
          sort_order INTEGER NOT NULL DEFAULT 1000,
          created_at TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT '')""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_navigation_order ON navigation_items(sort_order, id)")
        applied = conn.execute("SELECT 1 FROM schema_migrations WHERE version='navigation-001'").fetchone()
        existing = conn.execute("SELECT 1 FROM navigation_items LIMIT 1").fetchone()
        if applied or existing:
            if not applied:
                conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES('navigation-001',?)", (db.now(),))
            return
        seed = _legacy_items(conn) or [_normalize(item) for item in DEFAULT_NAVIGATION]
    with db.transaction() as conn:
        for item in seed:
            _insert(conn, item)
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES('navigation-001',?)", (db.now(),))


class NavigationInput(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    label: str = Field(min_length=1, max_length=40)
    href: str = Field(min_length=1, max_length=500)
    event_name: str = Field(default="", max_length=80)
    visible: bool = True
    show_as_bot: bool = True
    bot_size: float = Field(default=1, ge=0.4, le=2.5)
    sort_order: int = Field(default=1000, ge=-100000, le=100000)


@router.get("/navigation")
def read_navigation() -> dict:
    return {"items": list_items()}


@router.post("/navigation")
def create_navigation(body: NavigationInput) -> dict:
    try:
        item = _normalize(body.model_dump(), generated_id=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        with db.transaction() as conn:
            _insert(conn, item)
            _invalidate_settings_snapshot(conn)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "导航 id 已存在") from exc
    db.audit("navigation.create", item["id"], {"label": item["label"], "href": item["href"]})
    return item


@router.put("/navigation/{item_id}")
def update_navigation(item_id: str, body: NavigationInput) -> dict:
    payload = body.model_dump()
    payload["id"] = item_id
    try:
        item = _normalize(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with db.transaction() as conn:
        result = conn.execute(
            """UPDATE navigation_items SET label=?,href=?,event_name=?,visible=?,show_as_bot=?,
              bot_size=?,sort_order=?,updated_at=? WHERE id=?""",
            (item["label"], item["href"], item["event_name"], int(item["visible"]),
             int(item["show_as_bot"]), item["bot_size"], item["sort_order"], db.now(), item_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "导航项不存在")
        _invalidate_settings_snapshot(conn)
    db.audit("navigation.update", item_id, {"label": item["label"], "href": item["href"]})
    return item


@router.delete("/navigation/{item_id}")
def delete_navigation(item_id: str) -> dict:
    with db.transaction() as conn:
        result = conn.execute("DELETE FROM navigation_items WHERE id=?", (item_id,))
        if result.rowcount == 0:
            raise HTTPException(404, "导航项不存在")
        _invalidate_settings_snapshot(conn)
    db.audit("navigation.delete", item_id)
    return {"deleted": True}


@public_router.get("/navigation")
def public_navigation() -> dict:
    return {"items": public_items()}
