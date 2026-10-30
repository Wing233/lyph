from contextlib import closing
import json
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from . import db, navigation
from .auth import require_owner

router = APIRouter(prefix="/api/v1/admin", tags=["settings"], dependencies=[Depends(require_owner)])
public_router = APIRouter(prefix="/api/v1/public", tags=["site"])
DEFAULT_SITE = {
    "profile": {"name": "lyph", "bio": "技术与生活的记录", "introduction": ""},
    "projects": [], "stack": [], "interests": [],
    "seo": {"title": "lyph · 技术与生活", "description": "记录技术、生活与思考。"},
    "contact": {"wechat": "example-wechat"},
    "navigation": [],  # 兼容字段；实际导航以 navigation_items 为准。
    "acknowledgements": [{"title": "Agar.io", "url": "https://agar.io"}],
}


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS site_settings(
          id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL, revision INTEGER NOT NULL,
          updated_at TEXT NOT NULL)""")
        conn.execute("INSERT OR IGNORE INTO site_settings VALUES(1,?,1,?)", (json.dumps(DEFAULT_SITE, ensure_ascii=False), db.now()))


def get_settings(*, public: bool = False) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM site_settings WHERE id=1").fetchone()
    site = json.loads(row["payload_json"])
    # 导航以 navigation_items 为唯一数据源；旧 site.navigation 仅保留为兼容字段。
    site["navigation"] = navigation.public_items() if public else navigation.list_items()
    return {"revision": row["revision"], "site": site, "updated_at": row["updated_at"]}


class SettingsUpdate(BaseModel):
    revision: int = Field(ge=1)
    site: dict[str, Any]


@router.get("/settings")
def read_settings() -> dict:
    return get_settings()


@router.put("/settings")
def save_settings(body: SettingsUpdate) -> dict:
    allowed = set(DEFAULT_SITE)
    if set(body.site) - allowed:
        raise HTTPException(422, "Unknown site configuration fields")
    site = dict(DEFAULT_SITE) | body.site
    if not isinstance(site["profile"], dict) or not isinstance(site["seo"], dict) or not isinstance(site["contact"], dict):
        raise HTTPException(422, "Profile, SEO and contact must be objects")
    for key in ("projects", "stack", "interests", "navigation", "acknowledgements"):
        if not isinstance(site[key], list) or len(site[key]) > 100:
            raise HTTPException(422, "Site lists must contain at most 100 entries")
        if any(not isinstance(item, dict) for item in site[key]):
            raise HTTPException(422, "Site entries must be objects")
    normalized_navigation = None
    if "navigation" in body.site:
        # 兼容旧后台 JSON：显式提交 navigation 时同步到唯一数据源。
        try:
            normalized_navigation = navigation.normalize_items(site["navigation"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        site["navigation"] = normalized_navigation
    encoded = json.dumps(site, ensure_ascii=False)
    if len(encoded.encode()) > 65536:
        raise HTTPException(413, "Site configuration exceeds 64 KiB")
    with db.transaction() as conn:
        current = conn.execute("SELECT revision FROM site_settings WHERE id=1").fetchone()[0]
        if current != body.revision:
            raise HTTPException(409, {"message": "Site settings changed", "revision": current})
        conn.execute("UPDATE site_settings SET payload_json=?,revision=revision+1,updated_at=? WHERE id=1", (encoded, db.now()))
        if normalized_navigation is not None:
            navigation.write_items(conn, normalized_navigation)
    db.audit("site.settings.update", details={"revision": current + 1})
    return get_settings()


@public_router.get("/site")
def public_site() -> dict:
    return get_settings(public=True)["site"]


@router.get("/audit")
def audit_log() -> dict:
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 200").fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        items.append(item)
    return {"items": items}
