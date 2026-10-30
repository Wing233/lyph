import type { Metadata } from "next";
import { notFound } from "next/navigation";
import Link from "next/link";
import { serverApi, type Content, typeNames, ApiError } from "@/lib/api";
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
      <Markdown>{e.markdown}</Markdown>
    </PublicShell>
  );
}
