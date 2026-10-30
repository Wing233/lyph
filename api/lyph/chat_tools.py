"""Bounded, read-only public data tools; model arguments never become SQL or URLs."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import settings
from .content import PUBLIC_WHERE


QUERY_TIMEOUT_SECONDS = 2.0
SITE_SOURCE = "lyph 公开站点资料"
CONTENT_SOURCE = "lyph 已发布且已授权对话的内容"
ENTERPRISE_SOURCE = "发掘企业公开快照（非实时、非全量）"
CONTENT_WHERE = (
    PUBLIC_WHERE
    + " AND json_extract(published_json,'$.rag_enabled')=1"
    + " AND json_extract(draft_json,'$.rag_enabled')=1"
)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProfileArguments(Arguments):
    section: Literal["all", "profile", "projects", "stack", "interests"] = "all"
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=8, ge=1, le=20)


class ContentSearchArguments(Arguments):
    query: str = Field(default="", max_length=200)
    type: Literal["all", "blog", "document", "manual", "automatic"] = "all"
    limit: int = Field(default=8, ge=1, le=20)


class ContentArguments(Arguments):
    content_id: str = Field(min_length=1, max_length=100)


class CompanySearchArguments(Arguments):
    query: str = Field(default="", max_length=200)
    province: str = Field(default="", max_length=80)
    city: str = Field(default="", max_length=80)
    industry: str = Field(default="", max_length=100)
    has_jobs: bool = False
    limit: int = Field(default=8, ge=1, le=20)
    page: int = Field(default=1, ge=1, le=100000)
    sort: Literal["name", "jobs_desc"] = "name"


class CompanyArguments(Arguments):
    company_id: str = Field(min_length=1, max_length=100)


class CompanyAggregateArguments(CompanySearchArguments):
    group_by: Literal["province", "city", "industry", "latest_round", "company_type"] = "province"
    metric: Literal["companies", "active_jobs"] = "companies"
    limit: int = Field(default=10, ge=1, le=20)


class ToolFailure(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@contextmanager
def _readonly(path: Path):
    """Never create a missing DB, alter journal mode, or keep a transaction over IO."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ToolFailure("unavailable", "数据源尚未配置或暂时不可用。") from exc
    if not resolved.is_file():
        raise ToolFailure("unavailable", "数据源尚未配置或暂时不可用。")
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=QUERY_TIMEOUT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.enable_load_extension(False)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
        yield conn
    finally:
        conn.close()


def _result(data, blocks: list, source: str, updated_at: str | None):
    return {"data": data, "blocks": [dict(block, source=source, updated_at=updated_at) for block in blocks],
            "source": source, "updated_at": updated_at}


def _text(value, maximum: int = 2000) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def _public_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2000 or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        if ".." in parsed.path.split("/") or parsed.path.startswith(("/admin", "/collector", "/api/v1/admin")):
            return None
        return value
    if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not parsed.password:
        return value
    return None


def _profile(arguments: ProfileArguments):
    with _readonly(settings.database_path) as conn:
        row = conn.execute("SELECT payload_json,updated_at FROM site_settings WHERE id=1").fetchone()
    if not row:
        raise ToolFailure("unavailable", "公开站点资料暂时不可用。")
    site = json.loads(row["payload_json"])
    result, blocks = {}, []
    sections = ["profile", "projects", "stack", "interests"] if arguments.section == "all" else [arguments.section]
    for section in sections:
        raw = site.get(section)
        if section == "profile":
            result[section] = {key: _text(raw.get(key), 8000) for key in ("name", "bio", "introduction", "hero")
                               if isinstance(raw, dict) and isinstance(raw.get(key), str)}
            continue
        entries = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            clean = {key: _text(item[key], 12000 if key == "markdown" else 2000)
                     for key in ("id", "title", "name", "description", "summary", "status", "category", "stack", "markdown")
                     if isinstance(item.get(key), str)}
            for key in ("url", "image"):
                if url := _public_url(item.get(key)):
                    clean[key] = url
            if arguments.query.casefold() not in " ".join(clean.values()).casefold():
                continue
            entries.append(clean)
        result[section] = {"items": entries[:arguments.limit], "total": len(entries)}
        cards = []
        for item in entries[:arguments.limit]:
            title = item.get("title") or item.get("name") or "公开资料"
            card = {"title": title, "description": item.get("description") or item.get("summary") or item.get("stack") or ""}
            url = ("/project?id=" + quote(item["id"], safe="")) if section == "projects" and item.get("id") else item.get("url")
            if url:
                card["url"] = url
            if section == "projects" and item.get("id"):
                card["entity"] = {"kind": "project", "id": item["id"]}
            if item.get("image"):
                card["image"] = item["image"]
            cards.append(card)
        if cards:
            blocks.append({"type": "cards", "title": {"projects": "公开项目", "stack": "技术栈", "interests": "生活与兴趣"}[section], "items": cards})
    return _result(result, blocks, SITE_SOURCE, row["updated_at"])


def _content_item(row, *, body: bool = False):
    payload = json.loads(row["published_json"])
    item = {key: _text(payload.get(key), 2000) for key in ("title", "summary", "date", "category", "type")}
    item.update(id=row["id"], url="/entry?id=" + quote(row["id"], safe=""), updated_at=row["updated_at"])
    if body:
        markdown = _text(payload.get("markdown"), 500000)
        item.update(markdown=markdown[:30000], truncated=len(markdown) > 30000)
    return item


def _content_search(arguments: ContentSearchArguments):
    conditions, values = [CONTENT_WHERE], []
    if arguments.type != "all":
        conditions.append("json_extract(published_json,'$.type')=?")
        values.append(arguments.type)
    if arguments.query:
        conditions.append("(instr(lower(json_extract(published_json,'$.title')),lower(?))>0 OR "
                          "instr(lower(json_extract(published_json,'$.summary')),lower(?))>0 OR "
                          "instr(lower(json_extract(published_json,'$.markdown')),lower(?))>0)")
        values.extend([arguments.query] * 3)
    where = " AND ".join(conditions)
    with _readonly(settings.database_path) as conn:
        total = conn.execute("SELECT count(*) FROM contents WHERE " + where, values).fetchone()[0]
        rows = conn.execute("SELECT id,published_json,updated_at FROM contents WHERE " + where
                            + " ORDER BY json_extract(published_json,'$.date') DESC,id LIMIT ?", [*values, arguments.limit]).fetchall()
    items = [_content_item(row) for row in rows]
    blocks = [{"type": "cards", "title": "可用于对话的公开内容", "items": [
        {"title": item["title"], "description": item["summary"], "url": item["url"], "entity": {"kind": "content", "id": item["id"]}} for item in items]}] if items else []
    return _result({"items": items, "total": total}, blocks, CONTENT_SOURCE,
                   max((item["updated_at"] for item in items), default=None))


def _content_detail(arguments: ContentArguments):
    with _readonly(settings.database_path) as conn:
        row = conn.execute("SELECT id,published_json,updated_at FROM contents WHERE " + CONTENT_WHERE + " AND id=?",
                           (arguments.content_id,)).fetchone()
    if not row:
        raise ToolFailure("not_found", "内容不存在或尚未授权用于访客对话。")
    item = _content_item(row, body=True)
    return _result(item, [{"type": "content", "title": item["title"], "markdown": item["markdown"]}], CONTENT_SOURCE, item["updated_at"])


def _enterprise_path() -> Path:
    configured = os.getenv("LYPH_ENTERPRISE_DATABASE_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "public-apps" / "companies" / "data" / "web.sqlite"


@contextmanager
def _enterprise():
    with _readonly(_enterprise_path()) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"company", "meta"}.issubset(tables):
            raise ToolFailure("unavailable", "企业公开快照格式不兼容，请重新发布导出快照。")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(company)")}
        required = {"id", "name", "province", "city", "industry", "search_text", "active_jobs", "latest_round", "company_type", "updated_at"}
        if not required.issubset(columns):
            raise ToolFailure("unavailable", "企业公开快照字段不兼容，请重新发布导出快照。")
        stamp = conn.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
        if not stamp:
            raise ToolFailure("unavailable", "企业数据缺少公开快照构建时间。")
        yield conn, columns, _text(stamp[0], 100)


def _company_where(arguments: CompanySearchArguments):
    conditions, values = [], []
    if arguments.query:
        conditions.append("instr(lower(coalesce(search_text,'') || ' ' || coalesce(name,'')),lower(?))>0")
        values.append(arguments.query)
    for key in ("province", "city", "industry"):
        value = getattr(arguments, key)
        if value:
            # Names in the export use e.g. 北京, not 北京市. Match literally, never as a LIKE pattern.
            if key in {"province", "city"}:
                value = value.removesuffix("省").removesuffix("市")
            conditions.append(key + "=?")
            values.append(value)
    if arguments.has_jobs:
        conditions.append("coalesce(active_jobs,0)>0")
    return " AND ".join(conditions) or "1=1", values


def _company_search(arguments: CompanySearchArguments):
    where, values = _company_where(arguments)
    with _enterprise() as (conn, columns, stamp):
        selected = [key for key in ("id", "name", "name_en", "status", "province", "city", "industry", "website", "description",
                                   "latest_round", "investor_names", "list_names", "tags", "active_jobs", "primary_source", "primary_source_url", "updated_at")
                    if key in columns]
        total = conn.execute("SELECT count(*) FROM company WHERE " + where, values).fetchone()[0]
        order = "coalesce(active_jobs,0) DESC,name,id" if arguments.sort == "jobs_desc" else "name,id"
        rows = conn.execute("SELECT " + ",".join(selected) + " FROM company WHERE " + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
                            [*values, arguments.limit, (arguments.page - 1) * arguments.limit]).fetchall()
    items = []
    for row in rows:
        item = {key: (_text(row[key], 2000) if isinstance(row[key], str) else row[key]) for key in selected}
        for key in ("website", "primary_source_url"):
            if key in item:
                item[key] = _public_url(item[key])
        items.append(item)
    blocks = [{"type": "metrics", "title": "企业快照检索", "items": [{"label": "匹配企业", "value": total, "unit": "家"}]}]
    if items:
        if arguments.sort == "jobs_desc":
            blocks.append({"type": "chart", "title": "当前页企业记录岗位数比较", "kind": "bar", "unit": "个",
                           "rows": [{"label": item["name"], "value": max(item.get("active_jobs") or 0, 0)} for item in items]})
        blocks.append({"type": "table", "title": "企业检索结果", "columns": [
            {"key": "name", "label": "企业"}, {"key": "province", "label": "省份"}, {"key": "city", "label": "城市"},
            {"key": "industry", "label": "行业"}, {"key": "active_jobs", "label": "企业记录岗位数"}], "rows": items})
    return _result({"items": items, "total": total, "page": arguments.page, "limit": arguments.limit,
                    "has_more": arguments.page * arguments.limit < total, "snapshot": True}, blocks, ENTERPRISE_SOURCE, stamp)


def _company_detail(arguments: CompanyArguments):
    with _enterprise() as (conn, columns, stamp):
        selected = [key for key in ("id", "name", "name_en", "status", "province", "city", "industry", "website", "description", "latest_round", "active_jobs", "primary_source", "primary_source_url", "updated_at") if key in columns]
        row = conn.execute("SELECT " + ",".join(selected) + " FROM company WHERE id=?", (arguments.company_id,)).fetchone()
    if not row:
        raise ToolFailure("not_found", "企业不存在于当前公开快照。")
    item = {key: (_text(row[key], 12000 if key == "description" else 2000) if isinstance(row[key], str) else row[key]) for key in selected}
    for key in ("website", "primary_source_url"):
        if key in item:
            item[key] = _public_url(item[key])
    labels = {"province": "省份", "city": "城市", "industry": "行业", "active_jobs": "企业记录岗位数", "latest_round": "融资轮次", "status": "状态"}
    blocks = [{"type": "content", "title": item.get("name", ""), "markdown": item.get("description") or "公开快照暂无企业介绍。", "entity": {"kind": "company", "id": str(item["id"])}},
              {"type": "table", "title": "企业公开资料", "columns": [{"key": "label", "label": "字段"}, {"key": "value", "label": "内容"}],
               "rows": [{"label": label, "value": item.get(key)} for key, label in labels.items() if key in item]}]
    return _result({"item": item, "snapshot": True, "note": "岗位数使用企业 active_jobs 字段，不代表实时岗位明细或实时在招总量。"}, blocks, ENTERPRISE_SOURCE, stamp)


def _company_aggregate(arguments: CompanyAggregateArguments):
    where, values = _company_where(arguments)
    # Both identifiers come from validated Literal enums, never arbitrary model text.
    label = "coalesce(nullif(trim(" + arguments.group_by + "),''),'未知')"
    metric = "count(*)" if arguments.metric == "companies" else "sum(max(coalesce(active_jobs,0),0))"
    with _enterprise() as (conn, _, stamp):
        total = conn.execute("SELECT " + metric + " FROM company WHERE " + where, values).fetchone()[0] or 0
        rows = [dict(row) for row in conn.execute("SELECT " + label + " AS label," + metric + " AS value FROM company WHERE "
                                                + where + " GROUP BY " + label + " ORDER BY value DESC,label LIMIT ?",
                                                [*values, arguments.limit])]
        group_count = conn.execute("SELECT count(DISTINCT " + label + ") FROM company WHERE " + where, values).fetchone()[0]
    displayed_total = sum(row["value"] for row in rows)
    unit = "家" if arguments.metric == "companies" else "个"
    title = {"province": "省份", "city": "城市", "industry": "行业", "latest_round": "融资轮次", "company_type": "企业类型"}[arguments.group_by]
    title += "企业分布" if arguments.metric == "companies" else "企业记录岗位数分布"
    blocks = [{"type": "metrics", "title": "快照统计", "items": [
        {"label": "匹配企业数" if arguments.metric == "companies" else "企业记录岗位数合计", "value": total, "unit": unit}]}]
    if rows:
        blocks.extend([{"type": "chart", "title": title, "kind": "bar", "rows": rows, "unit": unit},
                       {"type": "table", "title": title, "columns": [{"key": "label", "label": "分类"},
                                                                                 {"key": "value", "label": "企业数" if arguments.metric == "companies" else "岗位数"}], "rows": rows}])
    note = "仅统计已发布快照；未知字段独立分组，Top 分组未覆盖部分计入 other_total。"
    if arguments.metric == "active_jobs":
        note += "岗位数按企业 active_jobs 字段求和，可能不同于岗位明细条数，不代表当前实时在招岗位总数。"
    return _result({"group_by": arguments.group_by, "metric": arguments.metric, "rows": rows, "total": total,
                    "group_count": group_count, "displayed_total": displayed_total, "other_total": total - displayed_total,
                    "snapshot": True, "note": note},
                   blocks, ENTERPRISE_SOURCE, stamp)


_TOOLS = {
    "get_public_profile": (ProfileArguments, _profile, "查询站主公开介绍、项目、技术栈或兴趣；只能读取已公开站点资料。"),
    "search_public_content": (ContentSearchArguments, _content_search, "搜索已发布、公开且明确授权访客对话的博客、文档和日报；返回摘要及内容ID。"),
    "get_public_content": (ContentArguments, _content_detail, "按内容ID读取授权访客对话的已发布正文；长正文会截断并标记。"),
    "search_companies": (CompanySearchArguments, _company_search, "从已发布的企业只读快照查询企业；query按字面关键词搜索；省份/城市/行业为准确筛选。仅为快照，非实时全量。"),
    "get_company": (CompanyArguments, _company_detail, "使用search_companies返回或当前选中的明确企业ID，读取该企业的公开快照资料。不能猜测企业ID；不返回实时岗位或私密字段。"),
    "aggregate_companies": (CompanyAggregateArguments, _company_aggregate, "对企业公开快照做数据库分组统计并返回图表；可按省份/城市/行业/轮次/类型分组，统计企业数或企业active_jobs字段合计（不是实时岗位总数）。"),
}
TOOL_DEFINITIONS = [{"type": "function", "function": {"name": name, "description": description,
                                                       "parameters": model.model_json_schema()}}
                    for name, (model, _, description) in _TOOLS.items()]


def execute_tool(name: str, arguments: dict) -> dict:
    """Return bounded JSON data/blocks or a safe error; no writes or external network."""
    failure = None
    if not isinstance(name, str) or name not in _TOOLS:
        failure = ToolFailure("unknown_tool", "不支持此数据工具。")
    elif not isinstance(arguments, dict):
        failure = ToolFailure("invalid_arguments", "工具参数必须是对象。")
    else:
        model, handler, _ = _TOOLS[name]
        try:
            validated = model.model_validate(arguments)
            result = handler(validated)
            for block in result.get("blocks", []):
                block["_query"] = {"name": name, "arguments": validated.model_dump()}
            return result
        except ValidationError:
            failure = ToolFailure("invalid_arguments", "工具参数无效：请检查字段、类型、长度、枚举和条数限制。")
        except ToolFailure as exc:
            failure = exc
        except sqlite3.OperationalError as exc:
            failure = ToolFailure("timeout" if "interrupt" in str(exc).lower() else "unavailable",
                                  "查询超时，请缩小查询范围。" if "interrupt" in str(exc).lower() else "数据源暂时不可用。")
        except (sqlite3.DatabaseError, OSError, ValueError, TypeError, KeyError):
            failure = ToolFailure("unavailable", "数据源暂时不可用或格式不兼容。")
    return {"data": None, "blocks": [], "source": None, "updated_at": None,
            "error": {"code": failure.code, "message": failure.message}}
