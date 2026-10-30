"""Editable preset questions for the chat UI, stored in SQLite."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json

from . import db

DEFAULT = {
    "default": ["有哪些项目", "你的技术栈", "有哪些文章"],
    "content": ["博客列表", "文档列表", "生活日报列表"],
    "companies": ["只看深圳", "再比较岗位", "这些企业按行业分布"],
}
VALID = set(DEFAULT)


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_presets(
          intent TEXT PRIMARY KEY,
          questions_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")


def _clean(questions) -> list[str]:
    cleaned: list[str] = []
    for item in questions or []:
        text = str(item).strip()[:120]
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned or len(cleaned) > 5:
        raise ValueError("预设问题需要 1–5 条")
    return cleaned


def load(intent: str) -> list[str]:
    intent = intent if intent in VALID else "default"
    try:
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT questions_json FROM chat_presets WHERE intent=?", (intent,)).fetchone()
        if row:
            value = json.loads(row["questions_json"])
            if isinstance(value, list):
                cleaned = [str(item).strip()[:120] for item in value if str(item).strip()]
                if cleaned:
                    return cleaned[:5]
    except Exception:
        pass
    return list(DEFAULT[intent])


def save(intent: str, questions) -> list[str]:
    if intent not in VALID:
        raise ValueError("intent 必须是 default / content / companies")
    cleaned = _clean(questions)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(db.connect()) as conn:
        conn.execute(
            """INSERT INTO chat_presets(intent,questions_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(intent) DO UPDATE SET
                 questions_json=excluded.questions_json, updated_at=excluded.updated_at""",
            (intent, json.dumps(cleaned, ensure_ascii=False), stamp),
        )
    return cleaned
