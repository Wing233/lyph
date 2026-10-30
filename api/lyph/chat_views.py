"""Server-owned read-only views and conservative, deterministic task routing."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import date as calendar_date
import json
import re
import sqlite3
import time
import unicodedata
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import chat_tools, db, presets
from .content import PUBLIC_WHERE

router = APIRouter(prefix="/api/v1/chat", tags=["chat-views"])
VIEW_TTL = 86400
FILTERS = {"query", "province", "city", "industry", "has_jobs"}


def init_schema() -> None:
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_view_contexts(
          visitor_id TEXT NOT NULL,conversation_id TEXT NOT NULL,state_json TEXT NOT NULL,
          updated_at TEXT NOT NULL,PRIMARY KEY(visitor_id,conversation_id));
        CREATE TABLE IF NOT EXISTS chat_views(
          id TEXT PRIMARY KEY,visitor_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
          request_id TEXT NOT NULL,revision INTEGER NOT NULL,query_json TEXT NOT NULL,
          created_at REAL NOT NULL,expires_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_chat_views_owner ON chat_views(visitor_id,conversation_id);
        CREATE INDEX IF NOT EXISTS idx_chat_views_expiry ON chat_views(expires_at);
        CREATE INDEX IF NOT EXISTS idx_chat_view_contexts_updated ON chat_view_contexts(updated_at);
        """)
        presets.init_schema()


def get_context(visitor_id: str, conversation_id: str | None) -> dict:
    if not conversation_id:
        return {}
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT state_json FROM chat_view_contexts WHERE visitor_id=? AND conversation_id=?",
                           (visitor_id, conversation_id)).fetchone()
    return json.loads(row[0]) if row else {}


def record_tool_context(visitor_id: str, conversation_id: str | None, name: str, args: dict, result: dict) -> None:
    if conversation_id and name == "get_company" and not result.get("error"):
        item = (result.get("data") or {}).get("item", {})
        identifier = item.get("id", args.get("company_id"))
        if identifier is not None:
            _record_selection(visitor_id, conversation_id, {"kind": "company", "id": str(identifier)}, result)
        return
    if not conversation_id or result.get("error") or name not in {"search_companies", "aggregate_companies"}:
        return
    model = chat_tools._TOOLS[name][0]
    try:
        arguments = model.model_validate(args).model_dump()
    except ValidationError:
        return
    state = {"intent": "companies", "tool": name, "arguments": arguments,
             "source": result.get("source"), "data_time": result.get("updated_at")}
    with db.transaction() as conn:
        conn.execute("INSERT INTO chat_view_contexts VALUES(?,?,?,?) ON CONFLICT(visitor_id,conversation_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
                     (visitor_id, conversation_id, json.dumps(state, ensure_ascii=False), db.now()))


def _record_selection(visitor_id: str, conversation_id: str, selection: dict, result: dict) -> None:
    if not conversation_id:
        return
    with db.transaction() as conn:
        row = conn.execute("SELECT state_json FROM chat_view_contexts WHERE visitor_id=? AND conversation_id=?", (visitor_id, conversation_id)).fetchone()
        state = json.loads(row[0]) if row else {}
        state.update(selected_entity=selection, selected_source=result.get("source"), selected_data_time=result.get("updated_at"))
        conn.execute("INSERT INTO chat_view_contexts VALUES(?,?,?,?) ON CONFLICT(visitor_id,conversation_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
                     (visitor_id, conversation_id, json.dumps(state, ensure_ascii=False), db.now()))


def _task(intent: str, name: str, arguments: dict, standalone: bool) -> dict:
    validated = chat_tools._TOOLS[name][0].model_validate(arguments).model_dump()
    steps = [{"name": name, "arguments": validated}]
    return {"intent": intent, "steps": steps, "canonical": json.dumps(steps, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "standalone": standalone, "slots": {"tool": name, **validated}}


def resolve_task(message: str, context: dict | None = None) -> dict | None:
    """Only recognize complete whitelist grammar; never drop an unrecognized constraint."""
    text = re.sub(r"[\s，。！？、,.!?]", "", unicodedata.normalize("NFKC", message)).lower()
    if text in {"这家公司", "查看这家公司", "这家公司详情"}:
        selected = (context or {}).get("selected_entity", {})
        if selected.get("kind") == "company" and isinstance(selected.get("id"), str):
            return _task("company", "get_company", {"company_id": selected["id"]}, False)
        return None
    pieces = re.split(r"并且|并|以及|同时|然后", text)
    if len(pieces) > 1:
        parts = [resolve_task(piece, context) for piece in pieces]
        if any(part is None for part in parts):
            return None
        steps = [step for part in parts for step in part["steps"]]
        return {"intent": "composite", "steps": steps, "canonical": json.dumps(steps, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "standalone": all(part["standalone"] for part in parts), "slots": {"steps": steps}}
    aliases = {
        "profile": {"介绍一下你", "介绍一下站主", "站主是谁", "关于站主", "个人资料", "个人介绍", "关于你"},
        "projects": {"有哪些项目", "看看项目", "查看项目", "项目列表", "展示项目", "介绍项目", "你的项目", "公开项目"},
        "stack": {"技术栈", "你的技术栈", "介绍技术栈", "使用什么技术"},
        "interests": {"兴趣爱好", "你的兴趣", "有哪些爱好"},
    }
    for section, forms in aliases.items():
        if text in forms:
            return _task(section, "get_public_profile", {"section": section}, True)
    for kind, forms in {"all": {"有哪些文章", "文章列表", "查看内容", "公开内容"},
                        "blog": {"博客", "博客列表", "有哪些博客"}, "document": {"文档", "文档列表"},
                        "manual": {"手写日记", "手写日记列表"}, "automatic": {"生活日报", "生活日报列表"}}.items():
        if text in forms:
            return _task("content", "search_public_content", {"type": kind}, True)
    # Negations, dates, personal criteria and relative comparisons require model clarification.
    if re.search(r"不|别|除|非|未|没有|去年|今年|季度|月|日|周|昨天|今天|明天|以前|以后|20\d{2}|我|适合|推荐|至少|以上|以下|少于|大于", text):
        return None
    ctx = context or {}
    contextual = bool(re.search(r"这些|它们|只看|再|这批|上述", text))
    if contextual and ctx.get("intent") != "companies":
        return None
    if not contextual and not re.search(r"企业|公司", text):
        return None
    args = dict(ctx.get("arguments", {})) if contextual else {}
    args.pop("group_by", None)
    args.pop("metric", None)
    args["page"] = 1
    residue = text
    provinces = ["广东", "广西", "北京", "上海", "浙江", "江苏", "山东", "福建", "四川", "湖北", "湖南", "河南", "河北", "安徽", "江西", "山西", "陕西", "云南", "贵州", "海南", "辽宁", "吉林", "黑龙江", "天津", "重庆", "甘肃", "青海", "台湾", "宁夏", "新疆", "西藏", "内蒙古", "香港", "澳门"]
    cities = ["深圳", "广州", "杭州", "南京", "苏州", "成都", "武汉", "西安", "长沙", "合肥", "厦门", "福州", "郑州", "济南", "青岛", "宁波", "东莞", "佛山", "珠海", "无锡", "天津", "重庆", "北京", "上海"]
    # Distribution dimensions are not location filters.
    group_by = "province"
    for term, group in [("按省份", "province"), ("按城市", "city"), ("按行业", "industry"), ("按轮次", "latest_round"), ("按类型", "company_type")]:
        if term in residue:
            group_by = group
            residue = residue.replace(term, "")
    found = [(place, "city") for place in cities if place in residue]
    found += [(place, "province") for place in provinces if place in residue and place not in cities]
    if len(found) > 1:
        return None  # A single location filter cannot faithfully express alternatives.
    for place, field in found:
        args[field] = place
        if contextual and field == "city":
            args["province"] = ""
        residue = residue.replace(place + ("市" if field == "city" else "省"), "").replace(place, "")
    keyword = next((word for word in ["人工智能", "ai", "软件", "硬件"] if word in residue), None)
    if keyword:
        args["query"] = keyword.upper() if keyword == "ai" else keyword
        residue = residue.replace(keyword, "")
    if "有岗位" in residue or "正在招聘" in residue:
        args["has_jobs"] = True
        residue = residue.replace("有岗位", "").replace("正在招聘", "")
    compare_jobs = any(word in residue for word in ["比较岗位", "对比岗位", "岗位比较"])
    aggregate = any(word in residue for word in ["分布", "统计"])
    if compare_jobs:
        args["sort"] = "jobs_desc"
    metric = "active_jobs" if "岗位" in residue and aggregate else "companies"
    if "岗位最多" in residue:
        args["sort"] = "jobs_desc"
        residue = residue.replace("岗位最多", "")
    match = re.search(r"前([1-9]\d?)", residue)
    if match:
        count = int(match[1])
        if count > 20:
            return None
        args["limit"] = count
        residue = residue.replace(match[0], "")
    for word in sorted(["帮忙", "帮", "请", "筛选", "搜索", "查询", "找一下", "找", "看看", "查看", "展示", "列出", "有哪些", "多少", "这些", "它们", "只看", "再", "这批", "上述", "企业", "公司", "比较岗位", "对比岗位", "岗位比较", "分布", "统计", "情况", "数据", "的", "家", "个"], key=len, reverse=True):
        residue = residue.replace(word, "")
    if residue:
        return None
    name = "aggregate_companies" if aggregate else "search_companies"
    if aggregate:
        args.update(group_by=group_by, metric=metric)
    return _task("companies", name, args, not contextual)


def rule_answer(task: dict, results: list[dict]) -> dict:
    """Explain whitelist results without a model call or invented measurements."""
    paragraphs = []
    suggestions = []
    for step, result in zip(task.get("steps", []), results):
        if result.get("error"):
            paragraphs.append(result["error"]["message"])
            continue
        data = result.get("data") or {}
        name = step["name"]
        if name == "get_public_profile":
            profile = data.get("profile", {})
            if profile:
                paragraphs.append("\n\n".join(value for key, value in profile.items() if key in {"name", "bio", "introduction", "hero"} and value))
            for key, label in [("projects", "项目"), ("stack", "技术栈"), ("interests", "兴趣")]:
                if key in data:
                    paragraphs.append(f"已找到 {data[key].get('total', 0)} 项公开{label}，可在下方查看。")
            suggestions = presets.load("default")
        elif name == "search_public_content":
            paragraphs.append(f"找到 {data.get('total', 0)} 篇已公开且授权对话的内容。点击卡片可阅读正文。")
            suggestions = presets.load("content")
        elif name == "get_public_content":
            paragraphs.append(f"已打开《{data.get('title', '')}》。" + ("正文较长，当前展示前 30000 字。" if data.get("truncated") else ""))
            suggestions = presets.load("content")
        elif name == "search_companies":
            paragraphs.append(f"当前条件匹配 {data.get('total', 0)} 家企业，下方展示第 {data.get('page', 1)} 页。" + ("已按企业记录岗位数从高到低排列。" if step["arguments"].get("sort") == "jobs_desc" else ""))
            paragraphs.append("企业数据来自公开快照；岗位数使用 active_jobs 字段，不代表实时在招岗位总数。")
            suggestions = presets.load("companies")
        elif name == "aggregate_companies":
            label = "企业记录岗位数合计" if data.get("metric") == "active_jobs" else "匹配企业数"
            paragraphs.append(f"{label}为 {data.get('total', 0)}，共 {data.get('group_count', 0)} 个分组。")
            if data.get("note"):
                paragraphs.append(data["note"])
            suggestions = presets.load("companies")
        elif name == "get_company":
            item = data.get("item", {})
            paragraphs.append("已打开“" + str(item.get("name", "")) + "”的公开快照资料。")
            if data.get("note"):
                paragraphs.append(data["note"])
            suggestions = presets.load("companies")
        if result.get("updated_at"):
            paragraphs.append("数据截至 " + str(result["updated_at"]) + "。")
    return {"answer": "\n\n".join(paragraphs) or "当前没有可展示的公开数据。", "suggestions": suggestions[:3]}


def _validate_query(query: dict) -> dict:
    if query.get("name") == "__read":
        args = query.get("arguments", {})
        if args.get("kind") not in {"project", "content", "company"} or not isinstance(args.get("id"), str) or not 0 < len(args["id"]) <= 100:
            raise HTTPException(400, "详情参数无效")
        return {"name": "__read", "arguments": {"kind": args["kind"], "id": args["id"]}}
    name = query.get("name")
    if name not in chat_tools._TOOLS:
        raise HTTPException(400, "视图查询无效")
    try:
        args = chat_tools._TOOLS[name][0].model_validate(query.get("arguments", {})).model_dump()
    except ValidationError:
        raise HTTPException(400, "视图条件无效") from None
    return {"name": name, "arguments": args}


def _decorate(blocks: list[dict], query: dict, view_id: str, revision: int, result: dict | None = None) -> list[dict]:
    blocks = deepcopy(blocks)
    company = query["name"] in {"search_companies", "aggregate_companies"}
    for block in blocks:
        block.pop("_query", None)
        block.update(view_id=view_id, revision=revision, query_state=dict(query["arguments"]))
        block["actions"] = []
    target = next((b for b in reversed(blocks) if b.get("type") == "table"), blocks[-1] if blocks else None)
    if target is not None:
        target["actions"] = [{"action": "refresh", "label": "重新查询"}]
        if company:
            args = query["arguments"]
            target["controls"] = [{"key": key, "label": label, "type": "text", "value": args.get(key, "")} for key, label in [("query", "关键词"), ("province", "省份"), ("city", "城市"), ("industry", "行业")]]
            target["controls"].append({"key": "limit", "label": "显示条数", "type": "range", "value": args.get("limit", 8), "min": 1, "max": 20})
            target["actions"] += [{"action": "filter", "label": "应用筛选"}, {"action": "limit", "label": "调整条数"}, {"action": "export", "label": "导出 CSV"}]
            if query["name"] == "search_companies":
                target["controls"].append({"key": "sort", "label": "排序", "type": "select", "value": args.get("sort", "name"), "options": [{"value": "name", "label": "企业名称"}, {"value": "jobs_desc", "label": "岗位数从高到低"}]})
                target["actions"] += [{"action": "sort", "label": "排序"}, {"action": "page", "label": "翻页"}, {"action": "select_entity", "label": "查看企业"}]
                if result:
                    target["pagination"] = {key: result.get("data", {}).get(key) for key in ("page", "limit", "total", "has_more")}
                else:
                    count = next((b.get("items", [{}])[0].get("value", 0) for b in blocks if b.get("type") == "metrics" and b.get("items")), 0)
                    target["pagination"] = {"page": args.get("page", 1), "limit": args.get("limit", 8), "total": count, "has_more": args.get("page", 1) * args.get("limit", 8) < count}
        elif target.get("type") == "cards":
            target["actions"].append({"action": "read", "label": "阅读详情"})
    return blocks


def prepare_blocks(blocks: list[dict], visitor_id: str, conversation_id: str | None, request_id: str) -> list[dict]:
    """Rebind every cached view; client-supplied IDs and action state are never reused."""
    output = []
    grouped: dict[str, list[dict]] = {}
    queries: dict[str, dict] = {}
    for original in blocks:
        block = deepcopy(original)
        query = block.pop("_query", None)
        for key in ("view_id", "revision", "query_state", "actions", "controls", "pagination"):
            block.pop(key, None)
        if not query:
            output.append(block)
            continue
        query = _validate_query(query)
        key = json.dumps(query, ensure_ascii=False, sort_keys=True)
        grouped.setdefault(key, []).append(block)
        queries[key] = query
    with db.transaction() as conn:
        conn.execute("DELETE FROM chat_views WHERE expires_at<?", (time.time(),))
        for key, batch in grouped.items():
            identifier = db.uid()
            conn.execute("INSERT INTO chat_views VALUES(?,?,?,?,?,?,?,?)", (identifier, visitor_id, conversation_id or "", request_id, 1, key, time.time(), time.time() + VIEW_TTL))
            output.extend(_decorate(batch, queries[key], identifier, 1))
        conn.execute("DELETE FROM chat_views WHERE id IN (SELECT id FROM chat_views ORDER BY created_at DESC LIMIT -1 OFFSET 10000)")
    return output


def _read_entity(kind: str, identifier: str) -> dict:
    if kind == "content":
        with chat_tools._readonly(chat_tools.settings.database_path) as conn:
            row = conn.execute("SELECT id,published_json,updated_at FROM contents WHERE " + PUBLIC_WHERE + " AND id=?", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, "内容不存在或未公开")
        item = json.loads(row["published_json"])
        markdown = str(item.get("markdown", ""))
        truncated = len(markdown) > 100000
        body = markdown[:100000] + ("\n\n> 正文较长，当前展示前 100000 字。" if truncated else "")
        return chat_tools._result({}, [{"type": "content", "title": str(item.get("title", "")), "markdown": body, "truncated": truncated, "entity": {"kind": kind, "id": identifier}}], "lyph 已发布公开内容（直接阅读）", row["updated_at"])
    if kind == "project":
        result = chat_tools.execute_tool("get_public_profile", {"section": "projects", "query": identifier, "limit": 20})
        items = [item for item in (result.get("data") or {}).get("projects", {}).get("items", []) if str(item.get("id")) == identifier]
        if not items:
            raise HTTPException(404, "项目不存在或未公开")
        item = items[0]
        return chat_tools._result({}, [{"type": "content", "title": item.get("title", ""), "markdown": item.get("markdown") or item.get("description", ""), "entity": {"kind": kind, "id": identifier}}], result["source"], result["updated_at"])
    return _execute({"name": "get_company", "arguments": {"company_id": identifier}})


def _execute(query: dict) -> dict:
    if query["name"] == "__read":
        try:
            return _read_entity(query["arguments"]["kind"], query["arguments"]["id"])
        except (chat_tools.ToolFailure, sqlite3.DatabaseError, OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "公开数据源暂时不可用") from None
    result = chat_tools.execute_tool(query["name"], query["arguments"])
    if result.get("error"):
        raise HTTPException(404 if result["error"]["code"] == "not_found" else 503, result["error"]["message"])
    return result


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["filter", "sort", "page", "limit", "select_entity", "read", "export", "refresh"]
    revision: int = Field(ge=1)
    value: Any = None


def get_view(view_id: str, visitor_id: str) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM chat_views WHERE id=? AND visitor_id=? AND expires_at>?", (view_id, visitor_id, time.time())).fetchone()
    if not row:
        raise HTTPException(404, "视图不存在或已过期")
    query = _validate_query(json.loads(row["query_json"]))
    result = _execute(query)
    return {"blocks": _decorate(result["blocks"], query, view_id, row["revision"], result), "revision": row["revision"]}


def perform_action(view_id: str, visitor_id: str, body: ActionBody) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM chat_views WHERE id=? AND visitor_id=? AND expires_at>?", (view_id, visitor_id, time.time())).fetchone()
    if not row:
        raise HTTPException(404, "视图不存在或已过期")
    if body.revision != row["revision"]:
        raise HTTPException(409, "视图已更新，请使用最新结果")
    query = json.loads(row["query_json"])
    company = query["name"] in {"search_companies", "aggregate_companies"}
    if body.action in {"read", "select_entity"}:
        # First revalidate the source; a stale card never grants permission to read.
        _execute(query)
        value = body.value
        if not isinstance(value, dict) or set(value) != {"kind", "id"}:
            raise HTTPException(400, "详情参数无效")
        detail = _validate_query({"name": "__read", "arguments": value})
        result = _execute(detail)
        _record_selection(visitor_id, row["conversation_id"], detail["arguments"], result)
        blocks = result["blocks"]
        for block in blocks:
            block["_query"] = detail
        return {"blocks": prepare_blocks(blocks, visitor_id, row["conversation_id"], row["request_id"]), "revision": row["revision"]}
    args = dict(query["arguments"])
    if body.action in {"filter", "sort", "page", "limit", "export"} and not company:
        raise HTTPException(400, "此视图不支持该操作")
    if body.action == "filter":
        if not isinstance(body.value, dict) or not set(body.value) <= FILTERS:
            raise HTTPException(400, "筛选字段无效")
        args.update(body.value)
        args["page"] = 1
    elif body.action in {"sort", "page", "limit"}:
        if body.action in {"sort", "page"} and query["name"] != "search_companies":
            raise HTTPException(400, "统计视图不支持翻页或企业排序")
        args[body.action] = body.value
        if body.action != "page":
            args["page"] = 1
    query = _validate_query({"name": query["name"], "arguments": args})
    result = _execute(query)  # Every action observes current publication and permissions.
    if body.action == "export":
        table = next((block for block in result["blocks"] if block.get("type") == "table"), None)
        rows = table["rows"] if table else []
        total = len(rows)
        if query["name"] == "search_companies":
            arguments = chat_tools.CompanySearchArguments.model_validate(args)
            where, values = chat_tools._company_where(arguments)
            with chat_tools._enterprise() as (conn, columns, stamp):
                total = conn.execute("SELECT count(*) FROM company WHERE " + where, values).fetchone()[0]
                order = "coalesce(active_jobs,0) DESC,name,id" if arguments.sort == "jobs_desc" else "name,id"
                rows = [dict(item) for item in conn.execute("SELECT id,name,province,city,industry,active_jobs FROM company WHERE " + where + " ORDER BY " + order + " LIMIT 5000", values)]
                result["updated_at"] = stamp
        # CSV is a spreadsheet boundary: neutralize formulas without changing query results.
        safe_rows = [{key: ("'" + value if isinstance(value, str) and value.lstrip(" ")[:1] in {"=", "+", "-", "@", "\t", "\r", "\n"} else value) for key, value in item.items()} for item in rows]
        return {"revision": row["revision"], "download": {"filename": "companies.csv", "columns": table["columns"] if table else [], "rows": safe_rows,
                "total": total, "truncated": total > len(rows), "source": result["source"], "updated_at": result["updated_at"]}}
    revision = row["revision"] + 1
    with db.transaction() as conn:
        changed = conn.execute("UPDATE chat_views SET revision=?,query_json=? WHERE id=? AND revision=?", (revision, json.dumps(query, ensure_ascii=False, sort_keys=True), view_id, row["revision"])).rowcount
        if not changed:
            raise HTTPException(409, "视图已更新，请使用最新结果")
    record_tool_context(visitor_id, row["conversation_id"], query["name"], args, result)
    return {"blocks": _decorate(result["blocks"], query, view_id, revision, result), "revision": revision}


@router.post("/views/{view_id}/actions")
def view_action(view_id: str, body: ActionBody, request: Request):
    from . import chat
    chat.check_origin(request)
    visitor = chat.visitor_for(request)
    chat.rate_limit("chat-view", visitor["id"], 120)
    try:
        return perform_action(view_id, visitor["id"], body)
    except (chat_tools.ToolFailure, sqlite3.DatabaseError, OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "公开数据源暂时不可用，请稍后重试") from None


@router.get("/views/{view_id}")
def latest_view(view_id: str, request: Request):
    from . import chat
    visitor = chat.visitor_for(request)
    chat.rate_limit("chat-view", visitor["id"], 120)
    try:
        return get_view(view_id, visitor["id"])
    except (chat_tools.ToolFailure, sqlite3.DatabaseError, OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "公开数据源暂时不可用，请稍后重试") from None


@router.get("/entities")
def entity(kind: Literal["project", "content", "company"], request: Request, id: str = "", date: str | None = None):
    from . import chat
    visitor = chat.visitor_for(request)
    chat.rate_limit("chat-read", visitor["id"], 120)
    if date:
        # Resolve old daily share links; never interpret this as a historic enterprise snapshot.
        try:
            valid_date = calendar_date.fromisoformat(date).isoformat() == date
        except ValueError:
            valid_date = False
        if kind != "content" or not valid_date:
            raise HTTPException(400, "日期参数仅支持按 YYYY-MM-DD 阅读公开日报")
        with chat_tools._readonly(chat_tools.settings.database_path) as conn:
            row = conn.execute("SELECT id FROM contents WHERE " + PUBLIC_WHERE
                               + " AND json_extract(published_json,'$.date')=? AND json_extract(published_json,'$.type') IN ('manual','automatic')"
                               + (" AND id=?" if id else "") + " ORDER BY updated_at DESC,id LIMIT 1", [date, id] if id else [date]).fetchone()
        if not row:
            raise HTTPException(404, "该日期暂无公开日报")
        id = row["id"]
    query = _validate_query({"name": "__read", "arguments": {"kind": kind, "id": id}})
    result = _execute(query)
    for block in result["blocks"]:
        block["_query"] = query
    return {"blocks": prepare_blocks(result["blocks"], visitor["id"], None, "direct-read")}
