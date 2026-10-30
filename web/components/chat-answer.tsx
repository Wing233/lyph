"use client";

import { useEffect, useState } from "react";
import { ExternalLink, ImageOff } from "lucide-react";
import { api, ApiError, errorText, json } from "@/lib/api";
import { Markdown } from "./markdown";
import "./chat-answer.css";

type Entity = { kind: "project" | "content" | "company"; id: string };
type Control = {
  key: string;
  label: string;
  type: "text" | "select" | "range";
  value: string | number;
  options?: { value: string | number; label: string }[];
  min?: number;
  max?: number;
};
type Provenance = {
  source?: string;
  updated_at?: string;
  view_id?: string;
  revision?: number;
  query_state?: Record<string, unknown>;
  pagination?: { page: number; limit: number; total: number; has_more: boolean };
  controls?: Control[];
  actions?: { action: string; label: string }[];
};
export type AnswerBlock = Provenance &
  (
    | {
        type: "metrics";
        title?: string;
        items: { label: string; value: string | number; unit?: string }[];
      }
    | {
        type: "cards";
        title?: string;
        items: {
          title: string;
          description?: string;
          url?: string;
          image?: string;
          entity?: Entity;
        }[];
      }
    | {
        type: "table";
        title: string;
        columns: { key: string; label: string }[];
        rows: Record<string, unknown>[];
      }
    | {
        type: "chart";
        title: string;
        kind: "bar" | "line" | "pie";
        rows: { label: string; value: number }[];
        unit?: string;
      }
    | { type: "image"; url: string; alt: string; caption?: string }
    | { type: "content"; title: string; markdown: string }
  );

export function safeAnswerUrl(value?: string) {
  if (
    !value ||
    value.startsWith("//") ||
    value.includes("\\") ||
    Array.from(value).some((character) => character.charCodeAt(0) <= 32)
  )
    return undefined;
  if (value.startsWith("/")) return value;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:"
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

function AnswerImage({
  url,
  alt,
  caption,
}: {
  url: string;
  alt: string;
  caption?: string;
}) {
  const [failed, setFailed] = useState(false);
  const src = safeAnswerUrl(url);
  useEffect(() => setFailed(false), [url]);
  return (
    <figure className="answer-image">
      {src && !failed ? (
        <img
          src={src}
          alt={alt || "回答配图"}
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <div className="answer-image-fallback">
          <ImageOff size={22} />
          <span>{alt || "图片暂时无法显示"}</span>
        </div>
      )}
      {caption && <figcaption>{caption}</figcaption>}
    </figure>
  );
}

const palette = [
  "#a34232",
  "#355e64",
  "#718369",
  "#ae884b",
  "#74768d",
  "#b97870",
];

function Chart({ block }: { block: Extract<AnswerBlock, { type: "chart" }> }) {
  const [tabular, setTabular] = useState(false);
  const rows = block.rows
    .filter((r) => typeof r.label === "string" && Number.isFinite(r.value))
    .slice(0, 40);
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.value)));
  const min = Math.min(0, ...rows.map((r) => r.value));
  const top = Math.max(1, ...rows.map((r) => r.value));
  const y = (value: number) => 190 - ((value - min) / (top - min)) * 160;
  const x = (i: number) => 42 + (i + 0.5) * (520 / Math.max(1, rows.length));
  const total = rows.reduce((sum, r) => sum + Math.max(0, r.value), 0);
  let cursor = 0;
  return (
    <div className="answer-chart">
      <div
        className="answer-chart-toggle"
        role="group"
        aria-label="图表展示方式"
      >
        <button aria-pressed={!tabular} onClick={() => setTabular(false)}>
          图表
        </button>
        <button aria-pressed={tabular} onClick={() => setTabular(true)}>
          数据表
        </button>
      </div>
      {!rows.length ? (
        <p className="answer-empty">暂时没有可绘制的数据。</p>
      ) : (
        <>
          {!tabular && (
            <svg
              viewBox="0 0 600 235"
              role="img"
              aria-label={`${block.title}，${block.kind === "pie" ? "占比图" : block.kind === "line" ? "折线图" : "柱状图"}。下方可查看完整数据表。`}
            >
              {block.kind === "pie" ? (
                <>
                  {total > 0 ? (
                    rows.map((r, i) => {
                      const fraction = Math.max(0, r.value) / total;
                      const start = cursor;
                      cursor += fraction;
                      const p = (n: number) =>
                        `${180 + 78 * Math.cos(n * Math.PI * 2 - Math.PI / 2)} ${110 + 78 * Math.sin(n * Math.PI * 2 - Math.PI / 2)}`;
                      return fraction >= 0.999999 ? (
                        <circle
                          key={i}
                          cx="180"
                          cy="110"
                          r="78"
                          fill={palette[i % palette.length]}
                        />
                      ) : (
                        <path
                          key={i}
                          d={`M 180 110 L ${p(start)} A 78 78 0 ${fraction > 0.5 ? 1 : 0} 1 ${p(cursor)} Z`}
                          fill={palette[i % palette.length]}
                          stroke="white"
                          strokeWidth="2"
                        >
                          <title>
                            {r.label}：{r.value}
                            {block.unit || ""}
                          </title>
                        </path>
                      );
                    })
                  ) : (
                    <circle cx="180" cy="110" r="78" fill="#ebecea" />
                  )}
                  {rows.slice(0, 7).map((r, i) => (
                    <g key={i}>
                      <circle
                        cx="320"
                        cy={45 + i * 25}
                        r="4"
                        fill={palette[i % palette.length]}
                      />
                      <text x="334" y={49 + i * 25}>
                        {r.label.length > 12
                          ? `${r.label.slice(0, 12)}…`
                          : r.label}{" "}
                        · {r.value}
                        {block.unit}
                      </text>
                    </g>
                  ))}
                </>
              ) : (
                <>
                  {[0, 0.5, 1].map((step) => (
                    <g key={step}>
                      <line
                        x1="42"
                        x2="570"
                        y1={30 + step * 160}
                        y2={30 + step * 160}
                        stroke="#e7e8e6"
                      />
                      <text x="34" y={34 + step * 160} textAnchor="end">
                        {(top - step * (top - min)).toLocaleString("zh-CN", {
                          maximumFractionDigits: 1,
                        })}
                      </text>
                    </g>
                  ))}
                  {block.kind === "line" ? (
                    <>
                      <polyline
                        points={rows
                          .map((r, i) => `${x(i)},${y(r.value)}`)
                          .join(" ")}
                        fill="none"
                        stroke={palette[1]}
                        strokeWidth="3"
                        strokeLinejoin="round"
                      />
                      {rows.map((r, i) => (
                        <circle
                          key={i}
                          cx={x(i)}
                          cy={y(r.value)}
                          r="4"
                          fill={palette[1]}
                        >
                          <title>
                            {r.label}：{r.value}
                            {block.unit}
                          </title>
                        </circle>
                      ))}
                    </>
                  ) : (
                    rows.map((r, i) => (
                      <rect
                        key={i}
                        x={x(i) - Math.min(18, 180 / rows.length)}
                        y={Math.min(y(r.value), y(0))}
                        width={Math.min(36, 360 / rows.length)}
                        height={Math.max(1, Math.abs(y(r.value) - y(0)))}
                        rx="3"
                        fill={r.value < 0 ? palette[0] : palette[1]}
                      >
                        <title>
                          {r.label}：{r.value}
                          {block.unit}
                        </title>
                      </rect>
                    ))
                  )}
                  {rows.map(
                    (r, i) =>
                      (rows.length <= 8 ||
                        i % Math.ceil(rows.length / 6) === 0) && (
                        <text key={i} x={x(i)} y="217" textAnchor="middle">
                          {r.label.length > 8
                            ? `${r.label.slice(0, 8)}…`
                            : r.label}
                        </text>
                      ),
                  )}
                </>
              )}
            </svg>
          )}
          {tabular && (
            <div className="answer-table-scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">项目</th>
                    <th scope="col">
                      数值{block.unit ? `（${block.unit}）` : ""}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={i}>
                      <th scope="row">{r.label}</th>
                      <td>{r.value.toLocaleString("zh-CN")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {block.kind === "pie" && rows.some((r) => r.value < 0) && (
        <p className="answer-source">负值未参与占比计算，请以数据表为准。</p>
      )}
      {max < 0 && null}
    </div>
  );
}

function readable(value: unknown) {
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

type ActionResult = {
  blocks?: AnswerBlock[];
  revision?: number;
  download?: {
    filename: string;
    columns: { key: string; label: string }[];
    rows: Record<string, unknown>[];
    total?: number;
    truncated?: boolean;
    source?: string;
    updated_at?: string;
  };
};
function replaceView(blocks: AnswerBlock[], viewId: string, replacement: AnswerBlock[]) {
  const index = blocks.findIndex((block) => block.view_id === viewId);
  if (index < 0) return blocks;
  return [...blocks.slice(0, index), ...replacement, ...blocks.slice(index).filter((block) => block.view_id !== viewId)];
}
export async function restoreAnswerViews(blocks: AnswerBlock[]) {
  const ids = [...new Set(blocks.map((block) => block.view_id).filter((id): id is string => !!id))];
  const results = await Promise.all(ids.map(async (id) => {
    try { const value = await api<ActionResult>(`/chat/views/${encodeURIComponent(id)}`); return { id, blocks: value.blocks || [], expired: false, failed: false }; }
    catch (error) { return { id, blocks: [] as AnswerBlock[], expired: error instanceof ApiError && [403, 404, 410].includes(error.status), failed: true }; }
  }));
  let current = blocks;
  for (const result of results) current = replaceView(current, result.id, result.blocks);
  return { blocks: current, notice: results.some((result) => result.expired) ? "部分视图已过期或权限发生变化，请重新查询。" : results.some((result) => result.failed) ? "部分视图暂时无法恢复，请重新查询。" : "" };
}
function downloadCsv(download: NonNullable<ActionResult["download"]>) {
  const cell = (value: unknown) => {
    let text = readable(value);
    if (/^[\s]*[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
  };
  const csv =
    "\ufeff" +
    [
      download.columns.map((c) => cell(c.label)).join(","),
      ...download.rows.map((row) =>
        download.columns.map((c) => cell(row[c.key])).join(","),
      ),
    ].join("\r\n");
  const url = URL.createObjectURL(
    new Blob([csv], { type: "text/csv;charset=utf-8" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = download.filename.replace(/[\\/:*?"<>|]/g, "_");
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function ViewControls({
  block,
  busy,
  act,
}: {
  block: AnswerBlock;
  busy: boolean;
  act: (block: AnswerBlock, action: string, value?: unknown) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string | number>>({});
  useEffect(
    () =>
      setValues(
        Object.fromEntries((block.controls || []).map((c) => [c.key, c.value])),
      ),
    [block],
  );
  return (
    <>
      {block.controls?.length ? (
        <form
          className="answer-controls"
          onSubmit={(event) => {
            event.preventDefault();
            void act(block, "filter", values);
          }}
        >
          {block.controls.map((control) => (
            <label key={control.key}>
              <span>
                {control.label}
                {control.type === "range" &&
                  ` · ${values[control.key] ?? control.value}`}
              </span>
              {control.type === "select" ? (
                <select
                  disabled={busy}
                  value={values[control.key] ?? control.value}
                  onChange={(e) =>
                    setValues({ ...values, [control.key]: e.target.value })
                  }
                >
                  {control.options?.map((option) => (
                    <option value={option.value} key={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  disabled={busy}
                  type={control.type === "range" ? "range" : "text"}
                  value={values[control.key] ?? control.value}
                  min={control.min}
                  max={control.max}
                  onChange={(e) =>
                    setValues({
                      ...values,
                      [control.key]:
                        control.type === "range"
                          ? Number(e.target.value)
                          : e.target.value,
                    })
                  }
                />
              )}
            </label>
          ))}
          <button disabled={busy} type="submit">
            应用筛选
          </button>
        </form>
      ) : null}
      <div className="answer-view-actions">
        {block.actions
          ?.filter(
            (action) =>
              !["filter", "read", "select_entity"].includes(action.action) && !(["sort", "limit"].includes(action.action) && block.controls?.some((control) => control.key === action.action)),
          )
          .map((action) =>
            action.action === "sort" ? (
              <label key="sort">
                排序{" "}
                <select
                  disabled={busy}
                  value={String(block.query_state?.sort || "name")}
                  onChange={(e) => void act(block, "sort", e.target.value)}
                >
                  <option value="name">企业名称</option>
                  <option value="jobs_desc">岗位数从高到低</option>
                </select>
              </label>
            ) : action.action === "page" ? (
              <div key="page" className="answer-pagination">
                <button
                  disabled={busy || Number(block.pagination?.page || block.query_state?.page || 1) <= 1}
                  onClick={() =>
                    void act(
                      block,
                      "page",
                      Math.max(1, Number(block.pagination?.page || block.query_state?.page || 1) - 1),
                    )
                  }
                >
                  上一页
                </button>
                <span>第 {Number(block.pagination?.page || block.query_state?.page || 1)} 页{block.pagination && ` · ${block.pagination.total} 条`}</span>
                <button
                  disabled={busy || block.pagination?.has_more === false}
                  onClick={() =>
                    void act(
                      block,
                      "page",
                      Number(block.pagination?.page || block.query_state?.page || 1) + 1,
                    )
                  }
                >
                  下一页
                </button>
              </div>
            ) : action.action === "limit" ? (
              <label key="limit">
                每页{" "}
                <select
                  disabled={busy}
                  value={String(block.pagination?.limit || block.query_state?.limit || 8)}
                  onChange={(e) =>
                    void act(block, "limit", Number(e.target.value))
                  }
                >
                  {[5, 8, 10, 20].map((n) => (
                    <option key={n}>{n}</option>
                  ))}
                </select>
              </label>
            ) : (
              <button
                key={action.action}
                disabled={busy}
                onClick={() => void act(block, action.action)}
              >
                {busy ? "处理中…" : action.label}
              </button>
            ),
          )}
      </div>
    </>
  );
}
export function AnswerBlocks({
  blocks,
  onRead,
}: {
  blocks: AnswerBlock[];
  onRead?: (blocks: AnswerBlock[]) => void;
}) {
  const [current, setCurrent] = useState(blocks);
  const [working, setWorking] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { setCurrent(blocks); setError(""); }, [blocks]);
  async function act(block: AnswerBlock, action: string, value?: unknown) {
    if (!block.view_id || working) return;
    setWorking(block.view_id);
    setError("");
    try {
      const result = await api<ActionResult>(
        `/chat/views/${encodeURIComponent(block.view_id)}/actions`,
        {
          method: "POST",
          body: json({ action, revision: block.revision, value }),
        },
      );
      if (result.download) {
        downloadCsv(result.download);
        if (result.download.truncated) setError(`已导出前 ${result.download.rows.length} 条，完整结果共 ${result.download.total} 条。可缩小筛选范围后再次导出。`);
      }
      else if (result.blocks) {
        if ((action === "read" || action === "select_entity") && onRead)
          onRead(result.blocks);
        else
          setCurrent((previous) => replaceView(previous, block.view_id!, result.blocks!));
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try {
          const latest = await api<ActionResult>(`/chat/views/${encodeURIComponent(block.view_id)}`);
          setCurrent((previous) => replaceView(previous, block.view_id!, latest.blocks || []));
          setError("视图已更新，已加载最新结果，请重试操作。");
        } catch (refreshError) {
          setCurrent((previous) => previous.filter((item) => item.view_id !== block.view_id));
          setError(refreshError instanceof ApiError && [403, 404, 410].includes(refreshError.status) ? "视图已过期或权限发生变化，请重新查询。" : "最新视图暂时无法读取，请重新查询。");
        }
      } else {
        if (e instanceof ApiError && [403, 404, 410].includes(e.status)) setCurrent((previous) => previous.filter((item) => item.view_id !== block.view_id));
        setError(errorText(e));
      }
    } finally {
      setWorking(null);
    }
  }
  async function readEntity(block: AnswerBlock, entity: Entity) {
    if (block.view_id) return act(block, "read", entity);
    try {
      const result = await api<{ blocks: AnswerBlock[] }>(
        `/chat/entities?${new URLSearchParams(entity)}`,
      );
      if (onRead) onRead(result.blocks);
      else setCurrent((previous) => [...previous, ...result.blocks]);
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <div className="answer-blocks">
      {error && (
        <p role="alert" className="chat-error">
          {error}
        </p>
      )}
      {current.map((block, index) => (
        <section
          className={`answer-block answer-block-${block.type}`}
          key={`${block.view_id || "block"}-${index}`}
          aria-busy={working === block.view_id}
        >
          {"title" in block && block.title && <h3>{block.title}</h3>}
          {block.type === "metrics" && (
            <dl className="answer-metrics">
              {block.items.slice(0, 8).map((item, i) => (
                <div key={i}>
                  <dt>{item.label}</dt>
                  <dd>
                    {item.value}
                    <small>{item.unit}</small>
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {block.view_id &&
          (block.controls?.length || block.actions?.length) ? (
            <ViewControls
              block={block}
              busy={working === block.view_id}
              act={act}
            />
          ) : null}
          {block.type === "cards" && (
            <div className="answer-cards">
              {block.items.slice(0, 12).map((item, i) => (
                <article key={i}>
                  {item.image && (
                    <AnswerImage url={item.image} alt={item.title} />
                  )}
                  <h4>{item.title}</h4>
                  {item.description && <p>{item.description}</p>}
                  {item.entity ? (
                    <button
                      disabled={!!working}
                      className="answer-read"
                      onClick={() => void readEntity(block, item.entity!)}
                    >
                      打开详情 →
                    </button>
                  ) : (
                    safeAnswerUrl(item.url) && (
                      <a
                        href={safeAnswerUrl(item.url)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        查看来源 <ExternalLink size={12} />
                      </a>
                    )
                  )}
                </article>
              ))}
            </div>
          )}
          {block.type === "table" && (
            <div
              className="answer-table-scroll"
              role="region"
              aria-label={block.title}
              tabIndex={0}
            >
              <table>
                <thead>
                  <tr>
                    {block.columns.map((col) => (
                      <th key={col.key} scope="col">
                        {col.label}
                      </th>
                    ))}
                    {block.actions?.some(
                      (a) => a.action === "select_entity",
                    ) && <th scope="col">详情</th>}
                  </tr>
                </thead>
                <tbody>
                  {block.rows.slice(0, 100).map((row, i) => (
                    <tr key={i}>
                      {block.columns.map((col) => (
                        <td key={col.key}>{readable(row[col.key])}</td>
                      ))}
                      {block.actions?.some(
                        (a) => a.action === "select_entity",
                      ) && (
                        <td>
                          <button
                            disabled={!!working}
                            onClick={() =>
                              void act(block, "select_entity", {
                                kind: "company",
                                id: String(row.id),
                              })
                            }
                          >
                            查看企业
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
              {!block.rows.length && (
                <p className="answer-empty">没有符合条件的数据。</p>
              )}
            </div>
          )}
          {block.type === "chart" && <Chart block={block} />}
          {block.type === "image" && (
            <AnswerImage
              url={block.url}
              alt={block.alt}
              caption={block.caption}
            />
          )}
          {block.type === "content" && <Markdown>{block.markdown}</Markdown>}
          {(block.source || block.updated_at) && (
            <p className="answer-source">
              {block.source && <span>来源：{block.source}</span>}
              {block.updated_at && <span>更新于 {block.updated_at}</span>}
            </p>
          )}
        </section>
      ))}
    </div>
  );
}
