"""Versioned, encrypted OpenAI-compatible model configuration."""
from __future__ import annotations

import base64
import hashlib
import json
import ipaddress
import socket
import struct
import zlib
from contextlib import closing
from typing import Literal
from urllib.parse import urlparse
import httpx

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from . import db
from .auth import require_owner
from .config import settings

router = APIRouter(prefix="/api/v1/admin/models", tags=["models"], dependencies=[Depends(require_owner)])
_test_http_transport = None
CAPABILITY_TOKEN_BUDGET = 1024


def capability_vision_png() -> bytes:
    """A deterministic 256px red RGB PNG; no external files or image dependency."""
    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff)
    pixels = (b"\x00" + b"\xff\x00\x00" * 256) * 256
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class _CapabilityFailure(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def _capability_text_details(response) -> dict:
    usage = getattr(response, "usage", None)
    def count(record, key):
        value = getattr(record, key, None)
        return value if type(value) is int and value >= 0 else None
    details = {"token_budget": CAPABILITY_TOKEN_BUDGET, "response_received": False,
               "finish_reason": None, "usage": None if usage is None else {
                   key: count(usage, key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}}
    if usage is not None:
        details["usage"]["reasoning_tokens"] = count(getattr(usage, "completion_tokens_details", None), "reasoning_tokens")
    if not response.choices:
        raise _CapabilityFailure("no_choices", "供应商未返回回答候选；请检查模型用途和 OpenAI 兼容接口。", details)
    choice = response.choices[0]
    reason = getattr(choice, "finish_reason", None)
    details["finish_reason"] = reason if reason in {None, "stop", "length", "content_filter", "tool_calls", "function_call"} else "unknown"
    text = choice.message.content
    details["response_received"] = isinstance(text, str) and bool(text.strip())
    if reason == "length":
        raise _CapabilityFailure("output_budget_exhausted", "能力测试已耗尽输出预算，未确认完整回答；推理模型可能先消耗推理 token，请检查模型的输出/推理预算设置后重试。", details)
    if reason == "content_filter":
        raise _CapabilityFailure("content_filtered", "供应商过滤了能力测试回答；请检查模型用途与内容过滤设置。", details)
    if not details["response_received"]:
        raise _CapabilityFailure("no_final_text", "模型未返回最终文本；请检查模型用途、输出格式和推理设置后重试。", details)
    return details


def _capability_error(exc: Exception) -> dict:
    if isinstance(exc, _CapabilityFailure):
        return {"error_code": exc.code, "error": exc.message, **exc.details}
    status = getattr(exc, "status_code", None)
    if status == 404:
        return {"error_code": "model_not_found", "http_status": 404,
                "error": "模型或调用端点未找到（HTTP 404）；请核对模型 ID、Base URL 和账号访问权限，不能用占位模型名代替已开通的模型。"}
    return {"error_code": "provider_request_failed", "error": safe_error(exc)}


def set_test_http_transport(transport) -> None:
    """An explicit HTTP fixture, never enabled by environment/production settings."""
    global _test_http_transport
    _test_http_transport = transport


def validate_endpoint(url: str):
    parsed = urlparse(url)
    if _test_http_transport is not None:
        return
    if parsed.scheme != "https":
        raise HTTPException(422, "模型地址必须使用 HTTPS 公网端点")
    host = parsed.hostname or ""
    if host in {"metadata.google.internal", "metadata", "localhost"} or host.endswith((".localhost", ".local", ".internal")):
        raise HTTPException(422, "模型端点不可使用本机、内网或元数据地址")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise HTTPException(422, "模型端点域名无法解析") from None
    if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
        raise HTTPException(422, "模型端点不可指向本机、内网或元数据地址")


def init_schema():
    with closing(db.connect()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS model_configs (
          id TEXT PRIMARY KEY, purpose TEXT NOT NULL, provider TEXT NOT NULL,
          base_url TEXT NOT NULL, model TEXT NOT NULL, secret BLOB NOT NULL,
          enabled INTEGER NOT NULL, timeout REAL NOT NULL, prompt TEXT NOT NULL,
          version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          test_status TEXT, tested_at TEXT
        );
        CREATE TABLE IF NOT EXISTS model_config_versions (
          id TEXT PRIMARY KEY, config_id TEXT NOT NULL, version INTEGER NOT NULL,
          metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, secret BLOB
        );
        """)
        if "secret" not in [row[1] for row in conn.execute("PRAGMA table_info(model_config_versions)")]:
            conn.execute("ALTER TABLE model_config_versions ADD COLUMN secret BLOB")
        conn.commit()


def cipher() -> Fernet:
    secret = str(settings.secret_key)
    if len(secret) < 32:
        raise HTTPException(503, "服务端加密密钥未正确配置")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


class ModelInput(BaseModel):
    purpose: Literal["chat", "embedding", "vision", "code"]
    provider: str = Field(default="openai-compatible", max_length=80)
    base_url: str = Field(max_length=512)
    model: str = Field(min_length=1, max_length=160)
    api_key: str | None = Field(default=None, max_length=4096)
    enabled: bool = True
    timeout: float = Field(default=60, ge=1, le=300)
    prompt: str = Field(default="", max_length=20000)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("base_url 必须是无内嵌凭证的 HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url 不接受查询参数或片段")
        return value.rstrip("/")


def public_config(row):
    result = dict(row)
    result.pop("secret", None)
    result["has_api_key"] = True
    result["enabled"] = bool(result["enabled"])
    return result


def config_for(purpose: str) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM model_configs WHERE purpose=? AND enabled=1 ORDER BY updated_at DESC LIMIT 1", (purpose,)).fetchone()
    if not row:
        raise HTTPException(503, f"尚未配置可用的 {purpose} 模型")
    return dict(row)


def client_for(config: dict) -> AsyncOpenAI:
    validate_endpoint(config["base_url"])
    try:
        key = cipher().decrypt(config["secret"]).decode()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "模型密钥无法解密，请重新保存配置") from None
    headers = None
    if config.get("provider") == "aihorde-public-json":
        if config["base_url"].rstrip("/") != "https://oai.aihorde.net/v1" or key != "0000000000":
            raise HTTPException(503, "免费测试模式仅允许官方匿名端点与公开匿名标识")
        origin = (settings.frontend_origin or "https://example.com").rstrip("/")
        headers = {"Client-Agent": f"lyph-public-test:1.0:{origin}", "User-Agent": "lyph/1.0 public free-model test"}
    http_client = httpx.AsyncClient(transport=_test_http_transport, follow_redirects=False) if _test_http_transport is not None else httpx.AsyncClient(follow_redirects=False)
    return AsyncOpenAI(api_key=key, base_url=config["base_url"], timeout=config["timeout"], max_retries=0, http_client=http_client, default_headers=headers)


def config_at(config_id: str, version: int) -> dict:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM model_config_versions WHERE config_id=? AND version=?", (config_id, version)).fetchone()
        current = conn.execute("SELECT * FROM model_configs WHERE id=? AND version=?", (config_id, version)).fetchone()
    if row and row["secret"]:
        return json.loads(row["metadata_json"]) | {"secret": row["secret"]}
    if current:
        return dict(current)
    raise HTTPException(503, "模型配置版本已不可用，请重新生成或重建")


def safe_error(exc: BaseException) -> str:
    # Never persist provider response bodies, URLs, headers, or SDK exception text.
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")


def save_model(payload: ModelInput, model_id: str | None = None):
    validate_endpoint(payload.base_url)
    values = payload.model_dump(exclude={"api_key"})
    with db.transaction() as conn:
        old = conn.execute("SELECT * FROM model_configs WHERE id=?", (model_id,)).fetchone() if model_id else None
        if model_id and old is None:
            raise HTTPException(404, "模型配置不存在")
        if not payload.api_key and not old:
            raise HTTPException(422, "新配置必须提供 api_key")
        encrypted = cipher().encrypt(payload.api_key.encode()) if payload.api_key else old["secret"]
        model_id = model_id or db.uid()
        version = old["version"] + 1 if old else 1
        timestamp = db.now()
        if payload.enabled:
            conn.execute("UPDATE model_configs SET enabled=0 WHERE purpose=?", (payload.purpose,))
        conn.execute("""INSERT INTO model_configs
            (id,purpose,provider,base_url,model,secret,enabled,timeout,prompt,version,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            purpose=excluded.purpose,provider=excluded.provider,base_url=excluded.base_url,
            model=excluded.model,secret=excluded.secret,enabled=excluded.enabled,timeout=excluded.timeout,
            prompt=excluded.prompt,version=excluded.version,updated_at=excluded.updated_at,test_status=NULL,tested_at=NULL""",
            (model_id, payload.purpose, payload.provider, payload.base_url, payload.model, encrypted,
             int(payload.enabled), payload.timeout, payload.prompt, version, old["created_at"] if old else timestamp, timestamp))
        values.update(id=model_id, version=version)
        conn.execute("INSERT INTO model_config_versions(id,config_id,version,metadata_json,created_at,secret) VALUES (?,?,?,?,?,?)", (db.uid(), model_id, version, json.dumps(values, ensure_ascii=False), timestamp, encrypted))
        result = public_config(conn.execute("SELECT * FROM model_configs WHERE id=?", (model_id,)).fetchone())
    db.audit("model.save", model_id, {"purpose": payload.purpose, "version": version})
    result["rebuild_required"] = payload.purpose == "embedding"
    return result


@router.get("")
def list_models():
    with closing(db.connect()) as conn:
        return {"items": [public_config(row) for row in conn.execute("SELECT * FROM model_configs ORDER BY purpose,updated_at DESC")]}


@router.post("", status_code=201)
def create_model(payload: ModelInput):
    return save_model(payload)


@router.put("/{model_id}")
def update_model(model_id: str, payload: ModelInput):
    return save_model(payload, model_id)


@router.get("/{model_id}/versions")
def versions(model_id: str):
    with closing(db.connect()) as conn:
        return {"items": [dict(row) | {"metadata": json.loads(row["metadata_json"])} for row in conn.execute("SELECT id,config_id,version,metadata_json,created_at FROM model_config_versions WHERE config_id=? ORDER BY version DESC", (model_id,))]}


@router.post("/{model_id}/test")
async def test_model(model_id: str):
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM model_configs WHERE id=?", (model_id,)).fetchone()
    if not row:
        raise HTTPException(404, "模型配置不存在")
    config = dict(row)
    try:
        async with client_for(config) as client:
            if config["purpose"] == "embedding":
                response = await client.embeddings.create(model=config["model"], input=["lyph 能力测试"])
                if not response.data or not response.data[0].embedding:
                    raise _CapabilityFailure("empty_embedding", "模型未返回向量；请核对 embedding 用途和模型 ID。")
                detail = {"dimensions": len(response.data[0].embedding)}
            else:
                message: object = "请只回答 OK。"
                if config["purpose"] == "code":
                    message = "请只输出一行 JavaScript：const ok = true; 不要解释。"
                if config["purpose"] == "vision":
                    png = base64.b64encode(capability_vision_png()).decode("ascii")
                    message = [{"type": "text", "text": "图片主要是什么颜色？只用一个词回答。"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}}]
                budget = 256 if config.get("provider") == "aihorde-public-json" else CAPABILITY_TOKEN_BUDGET
                response = await client.chat.completions.create(model=config["model"], messages=[{"role": "user", "content": message}], max_tokens=budget)
                detail = _capability_text_details(response)
                detail["token_budget"] = budget
        result = {"status": "passed", "purpose": config["purpose"], "config_version": config["version"], **detail}
    except Exception as exc:
        result = {"status": "failed", "purpose": config["purpose"], "config_version": config["version"], **_capability_error(exc)}
    with db.transaction() as conn:
        conn.execute("UPDATE model_configs SET test_status=?,tested_at=? WHERE id=? AND version=?", (result["status"], db.now(), model_id, config["version"]))
    db.audit("model.test", model_id, {"status": result["status"], "version": config["version"]})
    return result
