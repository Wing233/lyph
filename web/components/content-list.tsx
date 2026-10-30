import Link from "next/link";
import { serverApi, items, type Content, typeNames } from "@/lib/api";
import { PublicShell } from "./public-shell";
type Params = Record<string, string | string[] | undefined>;
type ContentPage = {
  items: Content[];
  total: number;
  has_more: boolean;
  has_next_page: boolean;
  categories: string[];
};
export async function ContentList({
  kind,
  title,
  subtitle,
  params,
}: {
  kind: string;
  title: string;
  subtitle: string;
  params: Params;
}) {
  const query = new URLSearchParams();
  for (const key of ["month", "q", "page", "batch", "category"]) {
    const v = params[key];
    if (typeof v === "string" && v) query.set(key, v);
  }
  const selected =
    typeof params.type === "string" &&
    ["blog", "document", "journal", "manual", "automatic"].includes(params.type)
      ? params.type
      : kind;
  query.set("type", selected);
  const page = Math.max(
    1,
    Math.min(100000, Number(query.get("page") || 1) || 1),
  );
  const batch = Math.max(1, Math.min(4, Number(query.get("batch") || 1) || 1));
  query.set("page", String(page));
  query.set("batch", String(batch));
  const data = await Promise.all(
    Array.from({ length: batch }, (_, index) => {
      const q = new URLSearchParams(query);
      q.set("batch", String(index + 1));
      return serverApi<ContentPage>(`/public/contents?${q}`);
    }),
  );
  const entries = data.flatMap((part) => items<Content>(part));
  const latest = data[data.length - 1];
  const href = (patch: Record<string, string>) => {
    const q = new URLSearchParams(query);
    for (const [k, v] of Object.entries(patch)) q.set(k, v);
    return `?${q}`;
  };
  const path =
    kind === "blog" ? "blog" : kind === "document" ? "documents" : "journal";
  return (
    <PublicShell>
      <p className="kicker">文字与记录</p>
      <h1>{title}</h1>
      <p className="page-description">{subtitle}</p>
      <form className="content-filters">
        {kind !== "journal" && (
          <input type="hidden" name="type" value={selected} />
        )}
        <label>
          搜索
          <input
            type="search"
            name="q"
            placeholder="标题或摘要"
            defaultValue={query.get("q") || ""}
          />
        </label>
        <label>
          月份
          <input
            type="month"
            name="month"
            defaultValue={query.get("month") || ""}
          />
        </label>
        {kind === "document" && (
          <label>
            分类
            <select name="category" defaultValue={query.get("category") || ""}>
              <option value="">全部分类</option>
              {latest.categories?.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
        )}
        {kind === "journal" && (
          <label>
            类型
            <select name="type" defaultValue={selected}>
              <option value="journal">全部生活记录</option>
              <option value="manual">手写日记</option>
              <option value="automatic">自动日报</option>
            </select>
          </label>
        )}
        <button className="button">查找</button>
        <Link href={`/${path}`} className="button secondary">
          重置
        </Link>
      </form>
      <div className="content-list">
        {entries.map((e) => (
          <article key={e.id}>
            <p className="eyebrow">
              {e.date} · {typeNames[e.type]}
              {e.category ? ` · ${e.category}` : ""}
            </p>
            <h2>
              <Link
                data-event="content.open"
                href={`/entry?id=${e.id}&from=${encodeURIComponent(`/${path}?${query}`)}`}
              >
                {e.title} ↗
              </Link>
            </h2>
            <p>{e.summary}</p>
          </article>
        ))}
      </div>
      {!entries.length && (
        <div className="empty-state">
          <h2>这里暂时没有公开的文字。</h2>
          <p>可以调整筛选，或过些时候再来。</p>
        </div>
      )}
      <div className="pagination">
        <span>
          共 {latest.total ?? entries.length} 篇 · 第 {page} 组
        </span>
        <div>
          {page > 1 && (
            <Link
              className="button secondary"
              href={href({ page: String(page - 1), batch: "4" })}
            >
              上一组
            </Link>
          )}
          {latest.has_more && batch < 4 && (
            <Link
              className="button secondary"
              href={href({ batch: String(batch + 1) })}
            >
              加载更多 12 篇
            </Link>
          )}
          {latest.has_next_page && batch >= 4 && (
            <Link
              className="button secondary"
              href={href({ page: String(page + 1), batch: "1" })}
            >
              下一组
            </Link>
          )}
        </div>
      </div>
    </PublicShell>
  );
}
