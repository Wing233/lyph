import type { Metadata } from "next";
import { notFound } from "next/navigation";
import Link from "next/link";
import {
  serverApi,
  type Content,
  type WorklogTL,
  isWorklogTL,
  typeNames,
  ApiError,
} from "@/lib/api";
import { PublicShell } from "@/components/public-shell";
import { Markdown } from "@/components/markdown";
import { SaveButton } from "@/components/collection";
type Props = {
  searchParams: Promise<{ id?: string; date?: string; from?: string }>;
};
async function load(p: Props["searchParams"]) {
  const query = await p;
  const id = query.id || query.date;
  if (!id) notFound();
  try {
    return await serverApi<Content>(
      `/public/contents/${encodeURIComponent(id)}`,
    );
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }
}
// 哨兵 WORKLOG_TL_V1：HTML 注释内嵌单个 compact JSON，以 --> 收尾，允许跨行。
const SENTINEL_RE = /<!--\s*WORKLOG_TL_V1\s+(\{[\s\S]*?\})\s*-->/;
function extractWorklog(
  markdown: string,
): { data: WorklogTL; cleaned: string } | null {
  if (typeof markdown !== "string") return null;
  const match = markdown.match(SENTINEL_RE);
  if (!match) return null;
  try {
    const parsed: unknown = JSON.parse(match[1]);
    if (!isWorklogTL(parsed)) return null;
    const cleaned = markdown.replace(SENTINEL_RE, "").trimEnd();
    return { data: parsed, cleaned };
  } catch {
    return null;
  }
}
const asText = (v: unknown) =>
  typeof v === "string" || typeof v === "number" ? String(v) : "";
const asNumber = (v: unknown) =>
  typeof v === "number" && Number.isFinite(v) ? v : 0;
function WorklogCards({ data }: { data: WorklogTL }) {
  const pts = Array.isArray(data.pts) ? data.pts : [];
  const stats = (data.stats ?? {}) as WorklogTL["stats"];
  const appTop = Array.isArray(stats.app_top) ? stats.app_top : [];
  const categories = Array.isArray(stats.browser_categories)
    ? stats.browser_categories
    : [];
  return (
    <div className="entry-extras">
      <aside className="entry-highlight" aria-label="今日小记">
        <h2>今日小记</h2>
        <p>{asText(data.hl)}</p>
      </aside>
      <details className="entry-timeline">
        <summary>
          这一天 · 时间线 <span>{pts.length} 个片段</span>
        </summary>
        <ol>
          {pts.map((p, i) => (
            <li key={i}>
              <span className="entry-timeline-time">{asText(p?.t)}</span>
              <span className="entry-timeline-app">{asText(p?.app)}</span>
              <span className="entry-timeline-line">{asText(p?.l)}</span>
            </li>
          ))}
        </ol>
      </details>
      <section className="entry-stats" aria-label="统计">
        <h2>统计</h2>
        <ul className="entry-stats-list">
          <li>
            活跃时长 <b>{asText(stats.active_human)}</b>
          </li>
          <li>
            Git 提交 <b>{asNumber(stats.git_commits)}</b>
          </li>
          <li>
            AI 会话 <b>{asNumber(stats.ai_sessions)}</b>
          </li>
          <li>
            AI 消息 <b>{asNumber(stats.ai_messages)}</b>
          </li>
          <li>
            文件改动 <b>{asNumber(stats.files_changed)}</b>
          </li>
        </ul>
        {appTop.length > 0 && (
          <div className="entry-stats-group">
            <h3>应用时长</h3>
            <ol>
              {appTop.map((a, i) => (
                <li key={i}>
                  <span>{asText(a?.app)}</span>
                  <span>{asNumber(a?.seconds)}s</span>
                </li>
              ))}
            </ol>
          </div>
        )}
        {categories.length > 0 && (
          <div className="entry-stats-group">
            <h3>浏览分类</h3>
            <ol>
              {categories.map((c, i) => (
                <li key={i}>
                  <span>{asText(c?.category)}</span>
                  <span>{asNumber(c?.visits)} 次</span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </section>
    </div>
  );
}
export async function generateMetadata({
  searchParams,
}: Props): Promise<Metadata> {
  const e = await load(searchParams);
  return { title: e.title, description: e.summary };
}
export default async function Page({ searchParams }: Props) {
  const e = await load(searchParams);
  const q = await searchParams;
  const from =
    q.from && /^\/(journal|blog|documents)(\?|$)/.test(q.from)
      ? q.from
      : e.type === "blog"
        ? "/blog"
        : e.type === "document"
          ? "/documents"
          : "/journal";
  const extracted = extractWorklog(e.markdown);
  const markdown = extracted ? extracted.cleaned : e.markdown;
  return (
    <PublicShell contentId={e.id}>
      <p className="kicker">
        {typeNames[e.type]} · {e.date}
      </p>
      <h1>{e.title}</h1>
      <p className="page-description">{e.summary}</p>
      <div className="reading-actions">
        <Link href={from}>返回目录 ↗</Link>
        <SaveButton
          item={{
            kind: "entry",
            id: e.id,
            title: e.title,
            url: `/entry?id=${e.id}`,
          }}
        />
      </div>
      <Markdown>{markdown}</Markdown>
      {extracted ? <WorklogCards data={extracted.data} /> : null}
    </PublicShell>
  );
}
