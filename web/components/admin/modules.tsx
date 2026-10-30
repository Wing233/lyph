"use client";
import Link from "next/link";
import { useState } from "react";
import {
  api,
  errorText,
  items,
  json,
  type Content,
  type NavigationItem,
  type Site,
} from "@/lib/api";
import { Dialog } from "@/components/ui/dialog";
import { ContentModule } from "./content";
import { AnalyticsReport } from "./analytics";
import { Files } from "./files";
import { AgentManagement } from "./agent-management";
import {
  DataView,
  Empty,
  formatTime,
  PageIntro,
  ResourceStatus,
  useResource,
} from "./common";
type RecordItem = Record<string, unknown>;
const text = (v: unknown) => (typeof v === "string" ? v : String(v ?? "—"));
const modelPayload = (m: RecordItem) =>
  Object.fromEntries(
    [
      "purpose",
      "provider",
      "base_url",
      "model",
      "enabled",
      "timeout",
      "prompt",
      "api_key",
    ]
      .filter((key) => key !== "api_key" || m[key])
      .map((key) => [key, m[key]]),
  );
export function AdminModule({ module }: { module: string }) {
  switch (module) {
    case "agent":
      return <AgentManagement />;
    case "content":
      return <ContentModule />;
    case "knowledge":
      return <Knowledge />;
    case "models":
      return <Models />;
    case "grants":
      return <Grants />;
    case "observability":
      return <Observability />;
    case "desktop":
      return <Desktop />;
    case "ui":
      return <UiChanges />;
    case "settings":
      return <Settings />;
    default:
      return <Dashboard />;
  }
}
function Dashboard() {
  const contents = useResource<{ items: Content[] }>("/admin/contents");
  const tasks = useResource<{ items: RecordItem[] }>("/admin/tasks");
  const audit = useResource<{ items: RecordItem[] }>("/admin/audit");
  const entries = items<Content>(contents.data);
  const taskItems = items<RecordItem>(tasks.data);
  return (
    <>
      <PageIntro
        eyebrow="OVERVIEW"
        title="今天，从这里开始。"
        description="照看内容，整理资料，留下一点新的想法。"
        action={
          <Link className="button" href="/admin/content">
            开始写作 ↗
          </Link>
        }
      />
      <div className="metric-grid">
        {[
          ["内容", entries.length, "content"],
          [
            "已发布",
            entries.filter((e) => e.published || e.published_version_id).length,
            "content",
          ],
          ["任务", taskItems.length, "settings"],
          [
            "失败待处理",
            taskItems.filter((t) => t.status === "failed").length,
            "settings",
          ],
        ].map(([name, value, path]) => (
          <Link className="metric" href={`/admin/${path}`} key={name}>
            <p className="metric-label">{name}</p>
            <p className="metric-value">{value}</p>
            <span className="metric-foot">服务端当前记录</span>
          </Link>
        ))}
      </div>
      <div className="content-flow">
        <div className="section-head">
          <h2>内容脉络</h2>
          <span className="flow-caption">从写下到被读到</span>
        </div>
        <div className="flow-nodes">
          {[
            ["写作", "content", "草稿与公开版本"],
            ["整理", "knowledge", "资料与索引"],
            ["连接", "models", "个人 Agent"],
            ["发布", "desktop", "审核今日记录"],
          ].map(([name, path, note]) => (
            <Link className="flow-node" href={`/admin/${path}`} key={path}>
              <strong>{name} ↗</strong>
              <small>{note}</small>
            </Link>
          ))}
        </div>
      </div>
      <div className="overview-grid">
        <section className="panel">
          <div className="section-head">
            <h2>最近的文字</h2>
            <Link className="text-link" href="/admin/content">
              全部 ↗
            </Link>
          </div>
          <ResourceStatus {...contents} retry={contents.reload} />
          {entries.slice(0, 5).map((c) => (
            <article className="recent-entry" key={c.id}>
              <p className="eyebrow">
                {c.date} · {c.visibility === "public" ? "公开" : "私密"}
              </p>
              <h3>{c.title}</h3>
              <p className="muted">{c.summary}</p>
            </article>
          ))}
          {!contents.loading && !entries.length && (
            <Empty title="留给第一篇文字。" />
          )}
        </section>
        <section className="note-panel">
          <span className="eyebrow">一点提醒</span>
          <blockquote>
            记录一次尝试，
            <br />
            留下一段日常。
          </blockquote>
          <p>
            新内容默认私密草稿，资料默认只供站主使用。发布与检索授权分别确认。
          </p>
        </section>
      </div>
      <section className="panel" style={{ marginTop: 24 }}>
        <div className="section-head">
          <h2>最近操作</h2>
          <Link className="text-link" href="/admin/settings">
            任务与审计 ↗
          </Link>
        </div>
        <ResourceStatus {...audit} retry={audit.reload} />
        {items<RecordItem>(audit.data)
          .slice(0, 6)
          .map((a) => (
            <p className="activity-item" key={text(a.id)}>
              <span>{text(a.action)}</span>
              <time>{formatTime(a.created_at)}</time>
            </p>
          ))}
      </section>
    </>
  );
}
function Knowledge() {
  const list = useResource<{ items: RecordItem[] }>("/admin/knowledge");
  const [detail, setDetail] = useState<RecordItem>();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("visitor");
  const [results, setResults] = useState<unknown>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <>
      <PageIntro
        eyebrow="PERSONAL KNOWLEDGE"
        title="让资料，有处可寻。"
        description="文件保存在私有存储。只有单独授权的来源进入访客检索。"
      />
      <div className="split-layout">
        <section className="panel">
          <h2>上传资料</h2>
          <p className="muted">
            Markdown、文本、PDF、Word，单文件最多 20 MiB。
          </p>
          <label className="field">
            选择文件
            <input
              type="file"
              accept=".md,.txt,.pdf,.docx"
              disabled={busy}
              onChange={async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                if (file.size > 20 * 1024 * 1024) {
                  setNotice("文件超过 20 MiB，请拆分后上传。");
                  return;
                }
                setBusy(true);
                try {
                  const form = new FormData();
                  form.set("file", file);
                  await api("/admin/knowledge", { method: "POST", body: form });
                  setNotice("文件已上传，处理进度请查看列表。");
                  void list.reload();
                } catch (err) {
                  setNotice(errorText(err));
                } finally {
                  setBusy(false);
                  e.target.value = "";
                }
              }}
            />
          </label>
        </section>
        <section className="panel">
          <h2>检索测试</h2>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                setResults(
                  await api("/admin/knowledge/search", {
                    method: "POST",
                    body: json({ query, scope }),
                  }),
                );
              } catch (err) {
                setNotice(errorText(err));
              }
            }}
          >
            <label className="field">
              检索问题
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                required
              />
            </label>
            <label className="field">
              身份
              <select value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="visitor">访客</option>
                <option value="owner">站主</option>
              </select>
            </label>
            <button className="button secondary">检索</button>
          </form>
          {results !== undefined && <DataView value={results} />}
        </section>
      </div>
      {notice && (
        <p className="inline-notice" role="status">
          {notice}
        </p>
      )}
      <div className="toolbar">
        <h2>资料与索引</h2>
        <button className="button secondary" onClick={list.reload}>
          刷新状态
        </button>
      </div>
      <ResourceStatus {...list} retry={list.reload} />
      {items<RecordItem>(list.data).length ? (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>资料</th>
                <th>处理</th>
                <th>访客检索</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items<RecordItem>(list.data).map((k) => (
                <tr key={text(k.id)}>
                  <td>
                    <button
                      className="table-link"
                      onClick={async () => {
                        try {
                          setDetail(await api(`/admin/knowledge/${k.id}`));
                        } catch (e) {
                          setNotice(errorText(e));
                        }
                      }}
                    >
                      {text(k.filename || k.title)}
                    </button>
                  </td>
                  <td>
                    {text(k.status)}
                    {!!k.error && <p className="error-text">{text(k.error)}</p>}
                  </td>
                  <td>{k.rag_enabled ? "已授权" : "仅站主"}</td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="text-link"
                        onClick={async () => {
                          try {
                            await api(`/admin/knowledge/${k.id}/permissions`, {
                              method: "PUT",
                              body: json({
                                visibility: k.rag_enabled
                                  ? "private"
                                  : "public",
                                rag_enabled: !k.rag_enabled,
                              }),
                            });
                            void list.reload();
                          } catch (e) {
                            setNotice(errorText(e));
                          }
                        }}
                      >
                        {k.rag_enabled ? "撤销授权" : "授权访客"}
                      </button>
                      <button
                        className="text-link"
                        onClick={async () => {
                          try {
                            await api(`/admin/knowledge/${k.id}/rebuild`, {
                              method: "POST",
                            });
                            setNotice(
                              "已提交重建任务，旧索引保留至新版本可用。",
                            );
                            void list.reload();
                          } catch (e) {
                            setNotice(errorText(e));
                          }
                        }}
                      >
                        重建索引
                      </button>
                      <button
                        className="text-link"
                        onClick={async () => {
                          try {
                            await api(`/admin/knowledge/${k.id}`, {
                              method: "DELETE",
                            });
                            void list.reload();
                          } catch (e) {
                            setNotice(errorText(e));
                          }
                        }}
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !list.loading &&
        !list.error && (
          <Empty
            title="尚未上传资料"
            description="加入自己的文件，让 Agent 的回答有据可循。"
          />
        )
      )}
      <Files />
      <Dialog
        open={!!detail}
        onOpenChange={() => setDetail(undefined)}
        title="资料与切片"
      >
        <DataView value={detail} />
        <button
          className="button secondary"
          onClick={async () => {
            try {
              setDetail({
                ...detail,
                chunks: await api(`/admin/knowledge/${detail?.id}/chunks`),
              });
            } catch (e) {
              setNotice(errorText(e));
            }
          }}
        >
          查看切片
        </button>
      </Dialog>
    </>
  );
}
function Models() {
  const list = useResource<{ items: RecordItem[] }>("/admin/models");
  const [model, setModel] = useState<RecordItem>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const initial = {
    purpose: "chat",
    provider: "openai-compatible",
    base_url: "",
    model: "",
    api_key: "",
    enabled: true,
    timeout: 60,
    prompt: "",
  };
  return (
    <>
      <PageIntro
        eyebrow="MODELS & AGENT"
        title="给对话，接上能力。"
        description="四种用途分别配置和测试。密钥只保存在服务端，不回显。"
        action={
          <button className="button" onClick={() => setModel(initial)}>
            添加模型 +
          </button>
        }
      />
      {notice && (
        <p className="inline-notice" role="status">
          {notice}
        </p>
      )}
      <ResourceStatus {...list} retry={list.reload} />
      <div className="model-grid">
        {items<RecordItem>(list.data).map((m) => (
          <section className="panel" key={text(m.id)}>
            <p className="eyebrow">{text(m.purpose).toUpperCase()}</p>
            <h2>{text(m.model)}</h2>
            <p className="muted">{text(m.base_url)}</p>
            <p className="pill">
              {m.enabled ? "已启用" : "已停用"} · 版本 {text(m.version)} ·{" "}
              {m.has_api_key ? "密钥已保存" : "无密钥"}
            </p>
            {!!m.rebuild_required && (
              <p className="inline-notice">
                embedding 变化后，需要重建受影响索引。
              </p>
            )}
            <div className="row-actions">
              <button
                className="button secondary"
                onClick={() => setModel({ ...m, api_key: "" })}
              >
                编辑
              </button>
              <button
                className="button secondary"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const result = await api<RecordItem>(
                      `/admin/models/${m.id}/test`,
                      { method: "POST" },
                    );
                    setNotice(
                      result.status === "passed"
                        ? "用途能力测试通过。"
                        : `测试失败：${text(result.error)}`,
                    );
                  } catch (e) {
                    setNotice(errorText(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                测试能力
              </button>
              <button
                className="text-link"
                onClick={async () => {
                  try {
                    await api(`/admin/models/${m.id}`, {
                      method: "PUT",
                      body: json(
                        modelPayload({ ...m, enabled: false, api_key: null }),
                      ),
                    });
                    setNotice("模型已停用，历史配置继续保留。");
                    void list.reload();
                  } catch (e) {
                    setNotice(errorText(e));
                  }
                }}
              >
                停用
              </button>
            </div>
          </section>
        ))}
      </div>
      {!list.loading && !list.error && !items(list.data).length && (
        <Empty
          title="还没有配置模型。"
          description="配置后，访客对话、资料索引、日报和 UI 候选才会调用真实模型。"
        />
      )}
      <Dialog
        open={!!model}
        onOpenChange={() => setModel(undefined)}
        title="模型配置"
      >
        {model && (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                const body = modelPayload(model);
                await api(`/admin/models${model.id ? `/${model.id}` : ""}`, {
                  method: model.id ? "PUT" : "POST",
                  body: json(body),
                });
                setModel(undefined);
                setNotice("配置已保存。");
                void list.reload();
              } catch (err) {
                setNotice(errorText(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label className="field">
              用途
              <select
                value={text(model.purpose)}
                onChange={(e) =>
                  setModel({ ...model, purpose: e.target.value })
                }
              >
                {["chat", "embedding", "vision", "code"].map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </label>
            {[
              ["provider", "供应商"],
              ["base_url", "API 地址"],
              ["model", "模型名"],
            ].map(([field, label]) => (
              <label className="field" key={field}>
                {label}
                <input
                  value={text(model[field]) === "—" ? "" : text(model[field])}
                  onChange={(e) =>
                    setModel({ ...model, [field]: e.target.value })
                  }
                  required
                />
              </label>
            ))}
            <label className="field">
              API Key{model.id ? "（留空保留现有密钥）" : ""}
              <input
                type="password"
                value={text(model.api_key) === "—" ? "" : text(model.api_key)}
                onChange={(e) =>
                  setModel({ ...model, api_key: e.target.value })
                }
                autoComplete="off"
                required={!model.id}
              />
            </label>
            <label className="field">
              提示词
              <textarea
                value={text(model.prompt) === "—" ? "" : text(model.prompt)}
                onChange={(e) => setModel({ ...model, prompt: e.target.value })}
              />
            </label>
            <label className="field">
              超时（秒）
              <input
                type="number"
                min={5}
                max={300}
                value={Number(model.timeout)}
                onChange={(e) =>
                  setModel({ ...model, timeout: Number(e.target.value) })
                }
              />
            </label>
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={!!model.enabled}
                onChange={(e) =>
                  setModel({ ...model, enabled: e.target.checked })
                }
              />
              启用
            </label>
            <button className="button" disabled={busy}>
              保存配置
            </button>
          </form>
        )}
      </Dialog>
    </>
  );
}
function Grants() {
  const list = useResource<{ items: RecordItem[] }>("/admin/grants");
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState("count");
  const [limit, setLimit] = useState(5);
  const [expires, setExpires] = useState("");
  const [note, setNote] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <>
      <PageIntro
        eyebrow="CONVERSATION ACCESS"
        title="让好奇，继续下去。"
        description="选择绝对到期时间或共享次数。密码明文只在签发时显示。"
        action={
          <button
            className="button"
            onClick={() => {
              setOpen(true);
              setPassword("");
              setError("");
            }}
          >
            签发密码 +
          </button>
        }
      />
      <ResourceStatus {...list} retry={list.reload} />
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>备注</th>
              <th>模式</th>
              <th>状态</th>
              <th>额度 / 到期</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items<RecordItem>(list.data).map((g) => (
              <tr key={text(g.id)}>
                <td>{text(g.note || g.id)}</td>
                <td>{g.mode === "count" ? "次数" : "时间"}</td>
                <td>{text(g.status || (g.revoked ? "吊销" : "有效"))}</td>
                <td>
                  {g.mode === "count"
                    ? `总计 ${g.quota_limit} / 已用 ${g.used ?? 0} / 预占 ${g.reserved ?? 0} / 剩余 ${g.remaining ?? "—"}`
                    : formatTime(g.expires_at)}
                </td>
                <td>
                  <button
                    className="text-link"
                    disabled={!!g.revoked}
                    onClick={async () => {
                      try {
                        await api(`/admin/grants/${g.id}`, {
                          method: "DELETE",
                        });
                        void list.reload();
                      } catch (e) {
                        setError(errorText(e));
                      }
                    }}
                  >
                    吊销
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {!list.loading && !list.error && !items(list.data).length && (
        <Empty
          title="尚未签发密码"
          description="已经成功回答的首次免费机会由服务端记账，密码提供额外授权。"
        />
      )}
      <Dialog
        open={open}
        onOpenChange={(value) => {
          setOpen(value);
          if (!value) setPassword("");
        }}
        title="签发对话密码"
      >
        {password ? (
          <div className="password-result">
            <p>请现在复制。关闭后无法再次查看。</p>
            <code>{password}</code>
            <button
              className="button"
              onClick={() =>
                void navigator.clipboard
                  .writeText(password)
                  .catch(() => setError("无法复制，请手动选择密码。"))
              }
            >
              复制密码
            </button>
          </div>
        ) : (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                const result = await api<{ password: string }>(
                  "/admin/grants",
                  {
                    method: "POST",
                    body: json({
                      mode,
                      note,
                      ...(mode === "count"
                        ? { limit }
                        : {
                            expires_at: new Date(
                              `${expires}+08:00`,
                            ).toISOString(),
                          }),
                    }),
                  },
                );
                setPassword(result.password);
                void list.reload();
              } catch (err) {
                setError(errorText(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label className="field">
              限制模式
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="count">共享对话次数</option>
                <option value="time">绝对到期时间</option>
              </select>
            </label>
            {mode === "count" ? (
              <label className="field">
                允许次数
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={limit}
                  onChange={(e) => setLimit(Number(e.target.value))}
                  required
                />
              </label>
            ) : (
              <label className="field">
                到期时间（上海）
                <input
                  type="datetime-local"
                  value={expires}
                  onChange={(e) => setExpires(e.target.value)}
                  required
                />
              </label>
            )}
            <label className="field">
              备注
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                maxLength={200}
              />
            </label>
            <button className="button" disabled={busy}>
              生成密码
            </button>
          </form>
        )}
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
      </Dialog>
    </>
  );
}
function Observability() {
  const [tab, setTab] = useState("analytics");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [range, setRange] = useState("");
  const resource = useResource<unknown>(`/admin/${tab}${range}`);
  const [detail, setDetail] = useState<unknown>();
  const [notice, setNotice] = useState("");
  const rows = items<RecordItem>(resource.data);
  return (
    <>
      <PageIntro
        eyebrow="OBSERVABILITY"
        title="看见真实的回应。"
        description="访问与对话独立统计；未知的 usage 保持未知。"
      />
      <div className="toolbar">
        <div className="segmented">
          <button
            aria-pressed={tab === "analytics"}
            onClick={() => setTab("analytics")}
          >
            访问分析
          </button>
          <button
            aria-pressed={tab === "conversations"}
            onClick={() => setTab("conversations")}
          >
            对话监控
          </button>
        </div>
        <form
          className="row-actions"
          onSubmit={(e) => {
            e.preventDefault();
            setRange(`?from=${from}&to=${to}`);
          }}
        >
          <label>
            起日
            <input
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </label>
          <label>
            止日
            <input
              type="date"
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
          </label>
          <button className="button secondary">筛选</button>
        </form>
      </div>
      <ResourceStatus {...resource} retry={resource.reload} />
      {notice && <p className="error-text">{notice}</p>}
      {tab === "analytics" ? (
        resource.data !== undefined && <AnalyticsReport value={resource.data} />
      ) : rows.length ? (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>会话</th>
                <th>状态</th>
                <th>创建</th>
                <th>查看</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={text(c.id || c.conversation_id)}>
                  <td>{text(c.id || c.conversation_id)}</td>
                  <td>
                    成功 {text(c.completed)} / 失败 {text(c.failed)} / 请求{" "}
                    {text(c.requests)}
                  </td>
                  <td>{formatTime(c.created_at)}</td>
                  <td>
                    <button
                      className="text-link"
                      onClick={async () => {
                        try {
                          setDetail(
                            await api(
                              `/admin/conversations/${c.id || c.conversation_id}`,
                            ),
                          );
                        } catch (e) {
                          setNotice(errorText(e));
                        }
                      }}
                    >
                      请求与调用 ↗
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !resource.loading &&
        !resource.error && (
          <Empty
            title="还没有对话记录。"
            description="访客真实发起请求后，这里显示请求、调用、检索与错误。"
          />
        )
      )}
      <Dialog
        open={detail !== undefined}
        onOpenChange={() => setDetail(undefined)}
        title="对话详情"
      >
        {items<RecordItem>(detail).map((r) => (
          <article className="recent-entry" key={text(r.id || r.request_id)}>
            <p className="eyebrow">
              {text(r.status)} · {formatTime(r.created_at)}
            </p>
            <h3>{text(r.message)}</h3>
            <p>{text(r.answer)}</p>
            <p className="muted">
              Usage：{r.usage ? JSON.stringify(r.usage) : "未知"}
            </p>
            <button
              className="text-link"
              onClick={async () => {
                try {
                  setDetail(
                    await api(`/admin/requests/${r.id || r.request_id}`),
                  );
                } catch (e) {
                  setNotice(errorText(e));
                }
              }}
            >
              调用、检索与反馈 ↗
            </button>
          </article>
        ))}
        <details>
          <summary>完整请求记录</summary>
          <DataView value={detail} />
        </details>
      </Dialog>
    </>
  );
}
function Desktop() {
  const devices = useResource<{ items: RecordItem[] }>("/admin/devices");
  const daily = useResource<{ items: RecordItem[] }>("/admin/daily");
  const [date, setDate] = useState(
    new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Shanghai" }),
  );
  const [name, setName] = useState("");
  const [platform, setPlatform] = useState("windows");
  const [credential, setCredential] = useState<RecordItem>();
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState<RecordItem>();
  return (
    <>
      <PageIntro
        eyebrow="SCREEN JOURNAL"
        title="把一天，留成文字。"
        description="截图始终私密。日报生成草稿，审核后在内容工作区发布。"
      />
      <div className="split-layout">
        <section className="panel">
          <h2>配对设备</h2>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                setCredential(
                  await api("/admin/devices", {
                    method: "POST",
                    body: json({ name, platform }),
                  }),
                );
                setName("");
                void devices.reload();
              } catch (err) {
                setNotice(errorText(err));
              }
            }}
          >
            <label className="field">
              设备名称
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </label>
            <label className="field">
              平台
              <select
                value={platform}
                onChange={(e) => setPlatform(e.target.value)}
              >
                <option value="windows">Windows</option>
                <option value="linux">Linux</option>
                <option value="ios">iOS PWA</option>
              </select>
            </label>
            <button className="button secondary">生成设备凭证</button>
          </form>
        </section>
        <section className="panel">
          <h2>按日生成</h2>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await api("/admin/daily", {
                  method: "POST",
                  body: json({ date, regenerate: false }),
                });
                setNotice("日报任务已提交，完成后进入审核草稿。");
                void daily.reload();
              } catch (err) {
                setNotice(errorText(err));
              }
            }}
          >
            <label className="field">
              日期（上海）
              <input
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                required
              />
            </label>
            <button className="button">生成日报</button>
          </form>
          <p className="muted">
            每天 23:00 调度。重复触发复用同一任务，迟到数据可重新生成。
          </p>
          <Link className="text-link" href="/collector">
            打开移动采集页 ↗
          </Link>
        </section>
      </div>
      {notice && (
        <p className="inline-notice" role="status">
          {notice}
        </p>
      )}
      <div className="toolbar">
        <h2>设备</h2>
        <button
          className="button secondary"
          onClick={() => {
            void devices.reload();
            void daily.reload();
          }}
        >
          刷新
        </button>
      </div>
      <ResourceStatus {...devices} retry={devices.reload} />
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>设备</th>
              <th>状态</th>
              <th>最后心跳</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items<RecordItem>(devices.data).map((d) => (
              <tr key={text(d.id)}>
                <td>
                  {text(d.name)} · {text(d.platform)}
                </td>
                <td>
                  {d.revoked ? "已吊销" : d.paused ? "已暂停" : "允许采集"}
                </td>
                <td>{formatTime(d.last_seen || d.last_heartbeat)}</td>
                <td>
                  <div className="row-actions">
                    <button
                      className="text-link"
                      onClick={async () => {
                        try {
                          await api(`/admin/devices/${d.id}`, {
                            method: "PUT",
                            body: json({ paused: !d.paused }),
                          });
                          void devices.reload();
                        } catch (e) {
                          setNotice(errorText(e));
                        }
                      }}
                    >
                      {d.paused ? "恢复" : "暂停"}
                    </button>
                    <button
                      className="text-link"
                      onClick={async () => {
                        try {
                          await api(`/admin/devices/${d.id}`, {
                            method: "DELETE",
                          });
                          void devices.reload();
                        } catch (e) {
                          setNotice(errorText(e));
                        }
                      }}
                    >
                      吊销
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="toolbar">
        <h2>日报任务与版本</h2>
        <Link className="text-link" href="/admin/content">
          审核与编辑 ↗
        </Link>
      </div>
      <ResourceStatus {...daily} retry={daily.reload} />
      <div className="content-list">
        {items<RecordItem>(daily.data).map((d) => (
          <article key={text(d.id || d.date)}>
            <p className="eyebrow">
              {text(d.date)} · {text(d.status)}
            </p>
            <h2>{text(d.title || "日报")}</h2>
            <div className="row-actions">
              <button
                className="text-link"
                onClick={async () => {
                  try {
                    setDetail(await api(`/admin/daily/${d.id || d.date}`));
                  } catch (e) {
                    setNotice(errorText(e));
                  }
                }}
              >
                查看输入与版本
              </button>
              <button
                className="text-link"
                onClick={async () => {
                  try {
                    await api("/admin/daily", {
                      method: "POST",
                      body: json({ date: d.date, regenerate: true }),
                    });
                    setNotice("新版本已排队，已发布文字保留。");
                    void daily.reload();
                  } catch (e) {
                    setNotice(errorText(e));
                  }
                }}
              >
                重新生成
              </button>
            </div>
          </article>
        ))}
      </div>
      {!daily.loading && !daily.error && !items(daily.data).length && (
        <Empty
          title="尚无日报。"
          description="设备确认上传的截图与速记将成为当日输入。"
        />
      )}
      <Dialog
        open={!!credential}
        onOpenChange={() => setCredential(undefined)}
        title="设备凭证"
      >
        <p>此凭证仅有设备采集权限。请现在保存。</p>
        <DataView value={credential} />
      </Dialog>
      <Dialog
        open={!!detail}
        onOpenChange={() => setDetail(undefined)}
        title="日报详情"
      >
        <DataView value={detail} />
        <Link className="button" href="/admin/content">
          在写作台审核 ↗
        </Link>
      </Dialog>
    </>
  );
}
function UiChanges() {
  const list = useResource<{ items: RecordItem[]; capacity_verified: boolean }>(
    "/admin/ui/sessions",
  );
  const [instruction, setInstruction] = useState("");
  const [candidate, setCandidate] = useState<RecordItem>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  async function action(id: unknown, kind: string) {
    setBusy(true);
    try {
      const data = await api<RecordItem>(`/admin/ui/sessions/${id}/${kind}`, {
        method: kind === "candidate" ? "GET" : "POST",
      });
      setCandidate({ ...candidate, ...data, id });
      setNotice(
        kind === "publish"
          ? "已提交构建发布任务，请查看健康检查与版本状态。"
          : kind === "rollback"
            ? "回滚操作完成。"
            : "服务已返回本次操作结果。",
      );
      void list.reload();
    } catch (e) {
      setNotice(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageIntro
        eyebrow="UI WORKSHOP"
        title="一个想法，先看见。"
        description="候选源码独立生成。校验通过后预览，由你确认发布。"
      />
      <section className="panel">
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            try {
              const result = await api<RecordItem>("/admin/ui/sessions", {
                method: "POST",
                body: json({ instruction }),
              });
              setCandidate(result);
              setInstruction("");
              setNotice("已创建候选会话。构建与发布结果以任务状态为准。");
              void list.reload();
            } catch (err) {
              setNotice(errorText(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="field">
            你想如何调整官网？
            <textarea
              rows={3}
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              required
              placeholder="例如：让首页最近记录的日期更容易辨认。"
            />
          </label>
          <button className="button" disabled={busy}>
            生成候选
          </button>
        </form>
        <p className="muted">
          只允许官网 UI 文件。后台、鉴权、数据、配置与密钥不进入候选。
        </p>
      </section>
      {notice && (
        <p className="inline-notice" role="status">
          {notice}
        </p>
      )}
      <ResourceStatus {...list} retry={list.reload} />
      {list.data && !list.data.capacity_verified && (
        <p className="inline-notice">
          部署容量尚未通过验收，源码校验、预览与生产发布尚未启用。可以保存候选指令并查看结果。
        </p>
      )}
      <div className="content-list">
        {items<RecordItem>(list.data).map((s) => (
          <article key={text(s.id)}>
            <p className="eyebrow">
              {formatTime(s.created_at)} · {text(s.status)}
            </p>
            <h2>{text(s.instruction)}</h2>
            <button
              className="text-link"
              onClick={() => void action(s.id, "candidate")}
            >
              查看候选与操作 ↗
            </button>
          </article>
        ))}
      </div>
      {!list.loading && !list.error && !items(list.data).length && (
        <Empty
          title="还没有 UI 候选"
          description="改动先经过隔离校验，不直接写入当前线上版本。"
        />
      )}
      <Dialog
        open={!!candidate}
        onOpenChange={() => setCandidate(undefined)}
        title="UI 候选与发布"
      >
        {candidate && (
          <>
            <DataView value={candidate} />
            {typeof candidate.preview_url === "string" && (
              <iframe
                title="隔离候选预览"
                src={candidate.preview_url}
                sandbox="allow-scripts"
                className="ui-preview"
              />
            )}
            <div className="row-actions">
              {[
                ["check", "校验完整源码"],
                ["preview", "启动预览"],
                ["publish", "确认构建发布"],
                ["rollback", "回滚版本"],
                ["cancel", "取消会话"],
              ].map(([actionName, label]) => (
                <button
                  className="button secondary"
                  disabled={busy}
                  key={actionName}
                  onClick={() => void action(candidate.id, actionName)}
                >
                  {label}
                </button>
              ))}
            </div>
          </>
        )}
      </Dialog>
    </>
  );
}
type NavigationDraft = { item: NavigationItem; isNew: boolean };

function NavigationSettings() {
  const list = useResource<{ items: NavigationItem[] }>("/admin/navigation");
  const [editing, setEditing] = useState<NavigationDraft>();
  const [removing, setRemoving] = useState<string>();
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const initial: NavigationItem = {
    id: "",
    label: "",
    href: "",
    event_name: "",
    visible: true,
    show_as_bot: true,
    bot_size: 1,
    sort_order: 1000,
  };
  const edit = (item: NavigationItem, isNew: boolean) => {
    setEditing({
      isNew,
      item: {
        ...item,
        event_name: item.event_name || "",
        visible: item.visible !== false,
        show_as_bot: item.show_as_bot !== false,
        bot_size: item.bot_size ?? 1,
        sort_order: item.sort_order ?? 1000,
      },
    });
    setNotice("");
    setError("");
    setFormError("");
    setRemoving(undefined);
  };
  const updateItem = (patch: Partial<NavigationItem>) => {
    setFormError("");
    setEditing((current) =>
      current ? { ...current, item: { ...current.item, ...patch } } : current,
    );
  };
  const remove = async (item: NavigationItem) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/admin/navigation/${encodeURIComponent(item.id)}`, {
        method: "DELETE",
      });
      setRemoving(undefined);
      setNotice(`菜单「${item.label}」已删除。`);
      void list.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel" style={{ marginTop: 24 }}>
      <div className="section-head">
        <div>
          <h2>导航与小球菜单</h2>
          <p>
            {
              "首页导航和吞噬小球共用这里的名称与地址。保存后刷新首页生效，也可直接在数据库 navigation_items 表新增。"
            }
          </p>
        </div>
        <div className="row-actions">
          <button
            className="button secondary"
            disabled={busy || list.loading}
            onClick={() => void list.reload()}
          >
            刷新
          </button>
          <button
            className="button"
            disabled={busy}
            onClick={() => edit(initial, true)}
          >
            添加菜单 +
          </button>
        </div>
      </div>
      {notice && (
        <p className="inline-notice" role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      <ResourceStatus {...list} retry={list.reload} />
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>菜单</th>
              <th>访问地址</th>
              <th>公开</th>
              <th>入口方式</th>
              <th>排序</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items<NavigationItem>(list.data).map((item) => (
              <tr key={item.id}>
                <td>
                  <span className="item-title">{item.label}</span>
                  <span className="item-subtitle mono">{item.id}</span>
                </td>
                <td>
                  <span className="mono">{item.href}</span>
                </td>
                <td>{item.visible === false ? "隐藏" : "公开"}</td>
                <td>
                  {item.show_as_bot === false
                    ? "直接显示"
                    : `小球 · ${item.bot_size ?? 1}`}
                </td>
                <td>{item.sort_order ?? 1000}</td>
                <td>
                  <div className="row-actions">
                    <button
                      className="text-link"
                      disabled={busy}
                      onClick={() => edit(item, false)}
                    >
                      编辑
                    </button>
                    {removing === item.id ? (
                      <>
                        <button
                          className="text-link"
                          disabled={busy}
                          onClick={() => void remove(item)}
                        >
                          确认删除
                        </button>
                        <button
                          className="text-link"
                          onClick={() => setRemoving(undefined)}
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <button
                        className="text-link"
                        disabled={busy}
                        onClick={() => setRemoving(item.id)}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!list.loading && !list.error && !items(list.data).length && (
        <Empty
          title="还没有导航菜单"
          description="点击右上角添加菜单，或直接在数据库 navigation_items 表新增。"
        />
      )}
      <Dialog
        open={!!editing}
        onOpenChange={(open) => {
          if (!open) {
            setEditing(undefined);
            setFormError("");
          }
        }}
        title={editing?.isNew ? "添加菜单" : "编辑菜单"}
      >
        {editing && (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError("");
              setNotice("");
              setFormError("");
              try {
                await api(
                  `/admin/navigation${
                    editing.isNew
                      ? ""
                      : `/${encodeURIComponent(editing.item.id)}`
                  }`,
                  {
                    method: editing.isNew ? "POST" : "PUT",
                    body: json({
                      ...editing.item,
                      id: editing.item.id || undefined,
                    }),
                  },
                );
                setEditing(undefined);
                setNotice("菜单已保存，刷新首页即可看到。");
                void list.reload();
              } catch (err) {
                setFormError(errorText(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label className="field">
              菜单名称
              <input
                value={editing.item.label}
                onChange={(e) => updateItem({ label: e.target.value })}
                maxLength={40}
                required
              />
            </label>
            <label className="field">
              访问地址
              <input
                value={editing.item.href}
                onChange={(e) => updateItem({ href: e.target.value })}
                placeholder="/blog、#chat 或 https://..."
                maxLength={500}
                required
              />
              <small>
                站内路径以 / 开头，首页锚点以 # 开头，外链使用 http(s)。
              </small>
            </label>
            <label className="field">
              标识{editing.isNew ? "（可选）" : ""}
              <input
                value={editing.item.id}
                onChange={(e) => updateItem({ id: e.target.value })}
                pattern="[A-Za-z0-9_-]{1,64}"
                maxLength={64}
                disabled={!editing.isNew}
              />
              <small>
                {editing.isNew
                  ? "留空自动生成；只能使用字母、数字、下划线和连字符。"
                  : "标识创建后不可修改。"}
              </small>
            </label>
            <div className="form-grid">
              <label className="field">
                小球大小（0.4～2.5）
                <input
                  type="number"
                  min={0.4}
                  max={2.5}
                  step={0.01}
                  value={editing.item.bot_size ?? 1}
                  onChange={(e) =>
                    updateItem({ bot_size: Number(e.target.value) })
                  }
                  required
                />
              </label>
              <label className="field">
                排序（越小越靠前）
                <input
                  type="number"
                  min={-100000}
                  max={100000}
                  step={1}
                  value={editing.item.sort_order ?? 1000}
                  onChange={(e) =>
                    updateItem({ sort_order: Number(e.target.value) })
                  }
                  required
                />
              </label>
            </div>
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={editing.item.visible !== false}
                onChange={(e) => updateItem({ visible: e.target.checked })}
              />
              <span>
                公开显示
                <small>关闭后前台不展示，但仍保留在后台列表中。</small>
              </span>
            </label>
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={editing.item.show_as_bot !== false}
                onChange={(e) => updateItem({ show_as_bot: e.target.checked })}
              />
              <span>
                生成小球
                <small>关闭后该菜单直接显示，不参与吞噬解锁。</small>
              </span>
            </label>
            <label className="field">
              埋点名称（可选）
              <input
                value={editing.item.event_name || ""}
                onChange={(e) => updateItem({ event_name: e.target.value })}
                placeholder="nav.blog.open"
                maxLength={80}
                pattern="[A-Za-z0-9._-]{0,80}"
              />
              <small>留空不记录；已有埋点名才会进入点击统计。</small>
            </label>
            {formError && (
              <p className="error-text" role="alert">
                {formError}
              </p>
            )}
            <button className="button" disabled={busy}>
              保存菜单
            </button>
          </form>
        )}
      </Dialog>
    </section>
  );
}

function Settings() {
  const settings = useResource<{ revision: number; site: Site }>(
    "/admin/settings",
  );
  const tasks = useResource<{ items: RecordItem[] }>("/admin/tasks");
  const audit = useResource<{ items: RecordItem[] }>("/admin/audit");
  const [tab, setTab] = useState("site");
  const [source, setSource] = useState<string>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const siteConfig =
    settings.data &&
    Object.fromEntries(
      Object.entries(settings.data.site).filter(
        ([key]) => key !== "navigation",
      ),
    );
  const value =
    source ?? (siteConfig ? JSON.stringify(siteConfig, null, 2) : "");
  let siteFields: Record<string, Record<string, string>> = {};
  try {
    siteFields = JSON.parse(value || "{}");
  } catch {}
  function fieldValue(section: string, key: string) {
    return typeof siteFields[section]?.[key] === "string"
      ? siteFields[section][key]
      : "";
  }
  function updateField(section: string, key: string, content: string) {
    try {
      const current = JSON.parse(value);
      setSource(
        JSON.stringify(
          { ...current, [section]: { ...current[section], [key]: content } },
          null,
          2,
        ),
      );
    } catch {
      setNotice("请先修正高级配置中的 JSON 格式。");
    }
  }
  return (
    <>
      <PageIntro
        eyebrow="SYSTEM"
        title="照看这个小站。"
        description="网站资料、导航菜单、任务与操作记录，从这里维护。"
      />
      <div className="segmented">
        {[
          ["site", "站点资料"],
          ["navigation", "导航菜单"],
          ["tasks", "任务"],
          ["audit", "操作记录"],
        ].map(([id, label]) => (
          <button
            key={id}
            className={tab === id ? "active" : undefined}
            aria-pressed={tab === id}
            onClick={() => {
              setTab(id);
              setNotice("");
            }}
          >
            {label}
          </button>
        ))}
      </div>
      {notice && (
        <p className="inline-notice" role="status">
          {notice}
        </p>
      )}
      {tab === "site" ? (
        <section className="panel" style={{ marginTop: 24 }}>
          <ResourceStatus {...settings} retry={settings.reload} />
          <h2>官网配置</h2>
          <p className="muted">
            基本资料和页面摘要保存后联动官网。作品、技术与兴趣在下方高级配置中维护。
          </p>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                const site = JSON.parse(value);
                if (!site || typeof site !== "object" || Array.isArray(site))
                  throw new Error("配置应为 JSON 对象。");
                for (const key of ["projects", "stack", "interests"])
                  if (
                    site[key] !== undefined &&
                    (!Array.isArray(site[key]) ||
                      site[key].some(
                        (item: unknown) =>
                          !item ||
                          typeof item !== "object" ||
                          Array.isArray(item),
                      ))
                  )
                    throw new Error(`${key} 应为对象数组。`);
                for (const key of ["profile", "seo", "contact"])
                  if (
                    site[key] !== undefined &&
                    (!site[key] ||
                      typeof site[key] !== "object" ||
                      Array.isArray(site[key]))
                  )
                    throw new Error(`${key} 应为对象。`);
                const result = await api<{ revision: number; site: Site }>(
                  "/admin/settings",
                  {
                    method: "PUT",
                    body: json({ revision: settings.data?.revision, site }),
                  },
                );
                settings.setData(result);
                setSource(undefined);
                setNotice("站点资料已保存，官网已使用新配置。");
              } catch (err) {
                setNotice(errorText(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            <div className="settings-fields">
              {[
                ["profile", "name", "站点名称"],
                ["profile", "bio", "一句话介绍"],
                ["profile", "introduction", "个人介绍"],
                ["seo", "title", "页面标题"],
                ["seo", "description", "搜索摘要"],
                ["contact", "wechat", "联系微信"],
              ].map(([section, key, label]) => (
                <label className="field" key={`${section}.${key}`}>
                  {label}
                  <input
                    value={fieldValue(section, key)}
                    onChange={(e) => updateField(section, key, e.target.value)}
                    disabled={!settings.data}
                  />
                </label>
              ))}
            </div>
            <details>
              <summary>高级展示配置</summary>
              <p className="muted">
                {
                  "projects 使用 id、title、description、status、stack；stack 与 interests 使用 id、name、description。导航菜单请在「导航菜单」页签维护。只填写要在官网展示的资料。"
                }
              </p>
              <label className="field">
                资料与展示配置
                <textarea
                  className="config-editor"
                  spellCheck={false}
                  rows={22}
                  value={value}
                  onChange={(e) => setSource(e.target.value)}
                />
              </label>
            </details>
            <button className="button" disabled={busy || !settings.data}>
              保存配置
            </button>
          </form>
        </section>
      ) : tab === "navigation" ? (
        <NavigationSettings />
      ) : (
        <section className="panel" style={{ marginTop: 24 }}>
          <ResourceStatus
            {...(tab === "tasks" ? tasks : audit)}
            retry={tab === "tasks" ? tasks.reload : audit.reload}
          />
          {tab === "tasks" ? (
            <>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>任务</th>
                      <th>状态</th>
                      <th>执行次数</th>
                      <th>错误 / 操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items<RecordItem>(tasks.data).map((task) => (
                      <tr key={text(task.id)}>
                        <td>
                          {text(task.kind)}
                          <small>{text(task.id)}</small>
                        </td>
                        <td>{text(task.status)}</td>
                        <td>{text(task.attempts)}</td>
                        <td>
                          {task.error ? text(task.error) : "—"}
                          {task.status === "failed" && (
                            <button
                              className="text-link"
                              disabled={busy}
                              onClick={async () => {
                                setBusy(true);
                                try {
                                  await api(`/admin/tasks/${task.id}/retry`, {
                                    method: "POST",
                                  });
                                  setNotice("任务已重新排队。");
                                  void tasks.reload();
                                } catch (error) {
                                  setNotice(errorText(error));
                                } finally {
                                  setBusy(false);
                                }
                              }}
                            >
                              重试
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <details>
                <summary>查看任务详情与版本</summary>
                <DataView value={tasks.data} />
              </details>
            </>
          ) : (
            <DataView value={audit.data} />
          )}
          <button
            className="button secondary"
            onClick={tab === "tasks" ? tasks.reload : audit.reload}
          >
            刷新
          </button>
        </section>
      )}
    </>
  );
}
