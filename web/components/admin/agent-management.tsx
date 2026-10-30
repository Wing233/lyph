"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, errorText, json } from "@/lib/api";
import { DataView, Empty, formatTime, PageIntro, ResourceStatus, useResource } from "./common";

type CacheConfig = { enabled: boolean; semantic_enabled: boolean; ttl_seconds: number; max_entries: number; max_bytes: number };
type CacheStats = { config: CacheConfig; entries: number; bytes: number; counters: Record<string, number>; hit_rate: number | null; calibration?: { passed?: boolean }; actual_model_usage?: { scope: string; calls: number; completed_calls: number; known_tokens: number; unknown_usage_calls: number }; estimates?: { estimated_saved_tokens: number | null; estimated_avoided_generations: number } };
type CacheEntry = { entry_id: string; size: number; created_at: string; expires_at: string; last_used: string; hits: number };
type Credential = { id: string; label: string; prefix: string; created_at: string; expires_at?: string; revoked: boolean; last_used_at?: string };
type McpCall = { id: string; credential_id: string; tool_name: string; status: string; created_at: string };
const mb = (value: number) => `${(value / 1048576).toFixed(2)} MiB`;

export function AgentManagement() {
  const stats = useResource<CacheStats>("/admin/chat-cache");
  const entries = useResource<{ items: CacheEntry[] }>("/admin/chat-cache/entries");
  const credentials = useResource<{ items: Credential[] }>("/admin/mcp/credentials");
  const calls = useResource<{ items: McpCall[] }>("/admin/mcp/calls");
  const [config, setConfig] = useState<CacheConfig>();
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [label, setLabel] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [token, setToken] = useState("");
  const [calibration, setCalibration] = useState<Record<string, unknown>>();
  useEffect(() => { if (stats.data) setConfig(stats.data.config); }, [stats.data]);
  async function run(action: () => Promise<unknown>, success: string) {
    if (busy) return;
    setBusy(true); setNotice("");
    try { await action(); setNotice(success); } catch (e) { setNotice(errorText(e)); } finally { setBusy(false); }
  }
  async function reloadCache() { await Promise.all([stats.reload(), entries.reload()]); }
  return <>
    <PageIntro eyebrow="AGENT OPERATIONS" title="答案缓存与 Agent 协议" description="管理答案复用、独立 MCP 凭证及只读工具调用。界面操作与 MCP 查询不调用站内模型。" />
    {notice && <p role="status" className="inline-notice">{notice}</p>}
    <ResourceStatus error={stats.error || entries.error || credentials.error || calls.error} loading={stats.loading || credentials.loading} retry={() => { void reloadCache(); void credentials.reload(); void calls.reload(); }} />
    <section className="panel">
      <div className="section-heading"><div><p className="eyebrow">ANSWER CACHE</p><h2>答案缓存</h2></div><button disabled={busy} className="button secondary" onClick={() => void run(reloadCache, "统计已更新。")}>刷新统计</button></div>
      {stats.data && <><div className="stats-grid"><div className="stat-card"><span>命中率</span><strong>{((stats.data.hit_rate ?? 0) * 100).toFixed(1)}%</strong></div><div className="stat-card"><span>缓存条目</span><strong>{stats.data.entries}</strong></div><div className="stat-card"><span>逻辑容量</span><strong>{mb(stats.data.bytes)}</strong></div><div className="stat-card"><span>未命中</span><strong>{stats.data.counters.miss || 0}</strong></div></div><p className="muted">精确命中 {stats.data.counters.exact || 0} · 任务归一化命中 {stats.data.counters.normalized || 0} · 语义命中 {stats.data.counters.semantic || 0}。缓存命中仍计入成功回答次数，命中不续期。</p><p><Link className="text-link" href="/admin/observability">查看实际模型调用与 token 用量 ↗</Link></p></>}
      {stats.data?.actual_model_usage && <div className="inline-notice"><strong>实际模型账本（全部已记录调用）</strong><p>调用 {stats.data.actual_model_usage.calls} 次，完成 {stats.data.actual_model_usage.completed_calls} 次；已知用量 {stats.data.actual_model_usage.known_tokens.toLocaleString("zh-CN")} token，另有 {stats.data.actual_model_usage.unknown_usage_calls} 次调用用量未知。</p><p className="muted">这是累计实际用量，不将命中答案的历史 token 计作本次消耗。</p></div>}
      {stats.data?.estimates && <p className="muted">推算节省量：避免完整生成约 {stats.data.estimates.estimated_avoided_generations} 次；节省 token {stats.data.estimates.estimated_saved_tokens === null ? "尚未估算" : `约 ${stats.data.estimates.estimated_saved_tokens.toLocaleString("zh-CN")}`}。</p>}
      {config && <form className="settings-fields" onSubmit={(event) => { event.preventDefault(); void run(async () => { await api("/admin/chat-cache/config", { method: "PUT", body: json(config) }); await reloadCache(); }, "缓存设置已保存。"); }}>
        <label className="field"><span>答案缓存</span><select value={String(config.enabled)} onChange={(event) => setConfig({ ...config, enabled: event.target.value === "true" })}><option value="true">启用</option><option value="false">关闭</option></select></label>
        <label className="field"><span>语义缓存</span><select value={String(config.semantic_enabled)} onChange={(event) => setConfig({ ...config, semantic_enabled: event.target.value === "true" })}><option value="false">关闭</option><option value="true">启用（需通过校准）</option></select></label>
        <label className="field"><span>有效期（秒）</span><input type="number" min="1" max="86400" value={config.ttl_seconds} onChange={(event) => setConfig({ ...config, ttl_seconds: Number(event.target.value) })} required /></label>
        <label className="field"><span>最多条目</span><input type="number" min="1" max="5000" value={config.max_entries} onChange={(event) => setConfig({ ...config, max_entries: Number(event.target.value) })} required /></label>
        <label className="field"><span>逻辑容量上限（MiB）</span><input type="number" min="1" max="128" value={config.max_bytes / 1048576} onChange={(event) => setConfig({ ...config, max_bytes: Number(event.target.value) * 1048576 })} required /></label>
        <div className="field"><span>保存配置</span><button className="button" disabled={busy} type="submit">保存缓存设置</button></div>
      </form>}
      <div className="page-actions"><button disabled={busy} className="button secondary" onClick={() => void run(async () => { const result = await api<Record<string, unknown>>("/admin/chat-cache/calibrate", { method: "POST" }); setCalibration(result); await stats.reload(); }, "校准已完成，请查看等价问题与反例结果。")}>{busy ? "处理中…" : "运行语义缓存校准"}</button><button disabled={busy} className="button secondary" onClick={() => void run(async () => { await api("/admin/chat-cache", { method: "DELETE" }); await reloadCache(); }, "全部缓存已清理。")}>清空全部答案缓存</button></div>
      <p className="muted">校准使用当前 embedding 配置，可能产生 embedding 用量。反例误命中时保持语义缓存关闭。</p>
      {calibration && <details open><summary>本次校准结果：{calibration.passed ? "通过" : "未通过"}</summary><DataView value={calibration} /></details>}
      <details><summary>缓存条目与失效</summary>{entries.data?.items.length ? <div className="table-wrap"><table className="data-table"><thead><tr><th>条目</th><th>容量 / 命中</th><th>到期时间</th><th>操作</th></tr></thead><tbody>{entries.data.items.map((entry) => <tr key={entry.entry_id}><td><code>{entry.entry_id.slice(0, 16)}…</code><small>创建 {formatTime(entry.created_at)}</small></td><td>{mb(entry.size)} / {entry.hits} 次</td><td>{formatTime(entry.expires_at)}</td><td><button className="text-link" disabled={busy} onClick={() => void run(async () => { await api(`/admin/chat-cache/entries/${encodeURIComponent(entry.entry_id)}`, { method: "DELETE" }); await reloadCache(); }, "该缓存已失效。")}>使其失效</button></td></tr>)}</tbody></table></div> : <Empty title="暂无答案缓存" description="成功完成且来源版本可确认的回答会保存到这里。" />}</details>
    </section>
    <section className="panel">
      <div className="section-heading"><div><p className="eyebrow">READ-ONLY MCP</p><h2>独立访问凭证</h2></div></div>
      <p className="muted">连接地址：<code>/api/v1/mcp</code>。客户端显式配置 <code>Authorization: Bearer &lt;token&gt;</code>，使用 Streamable HTTP。仅开放公开授权的只读工具，独立限流。</p>
      <form className="settings-fields" onSubmit={(event) => { event.preventDefault(); void run(async () => { const result = await api<Credential & { token: string }>("/admin/mcp/credentials", { method: "POST", body: json({ label: label.trim(), ...(expiresAt ? { expires_at: new Date(expiresAt).toISOString() } : {}) }) }); setToken(result.token); setLabel(""); setExpiresAt(""); await credentials.reload(); }, "凭证已签发，完整 token 仅在此显示一次。"); }}><label className="field"><span>客户端名称</span><input required maxLength={80} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="例如：我的桌面助手" /></label><label className="field"><span>有效期（可选，本地时间）</span><input type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} /></label><button type="submit" disabled={busy} className="button">签发 MCP 凭证</button></form>
      {token && <div className="inline-notice"><label className="field"><span>完整 token 仅此一次，请立即保存。</span><textarea readOnly rows={2} value={token} aria-label="新 MCP 凭证" /></label><div className="page-actions"><button className="button secondary" onClick={() => void navigator.clipboard.writeText(token).then(() => setNotice("凭证已复制。")).catch(() => setNotice("复制不可用，请选择凭证手动复制。"))}>复制凭证</button><button className="text-link" onClick={() => setToken("")}>已保存，隐藏</button></div></div>}
      {credentials.data?.items.length ? <div className="table-wrap"><table className="data-table"><thead><tr><th>名称 / 前缀</th><th>有效期</th><th>最近调用</th><th>状态 / 操作</th></tr></thead><tbody>{credentials.data.items.map((credential) => <tr key={credential.id}><td>{credential.label}<small><code>{credential.prefix}…</code></small></td><td>{credential.expires_at ? formatTime(credential.expires_at) : "长期有效"}</td><td>{formatTime(credential.last_used_at)}</td><td>{credential.revoked ? "已吊销" : <button className="text-link" disabled={busy} onClick={() => void run(async () => { await api(`/admin/mcp/credentials/${encodeURIComponent(credential.id)}`, { method: "DELETE" }); setToken(""); await credentials.reload(); }, "凭证已吊销，客户端将无法继续调用。")}>吊销凭证</button>}</td></tr>)}</tbody></table></div> : <Empty title="暂无 MCP 凭证" description="为每个外部客户端签发独立凭证，便于分别吊销。" />}
      <details><summary>最近的 MCP 工具调用</summary>{calls.data?.items.length ? <div className="table-wrap"><table className="data-table"><thead><tr><th>工具</th><th>客户端</th><th>结果</th><th>时间</th></tr></thead><tbody>{calls.data.items.map((call) => <tr key={call.id}><td>{call.tool_name}</td><td>{credentials.data?.items.find((credential) => credential.id === call.credential_id)?.label || call.credential_id.slice(0, 12)}</td><td>{call.status}</td><td>{formatTime(call.created_at)}</td></tr>)}</tbody></table></div> : <p className="muted">暂无工具调用记录。</p>}<button className="text-link" onClick={() => void calls.reload()}>刷新调用记录</button></details>
    </section>
  </>;
}
