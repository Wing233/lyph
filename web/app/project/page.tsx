import { notFound } from "next/navigation";
import { PublicShell } from "@/components/public-shell";
import { serverApi, type Site } from "@/lib/api";
import { SaveButton } from "@/components/collection";
import { Markdown } from "@/components/markdown";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ id?: string }>;
}) {
  const { id } = await searchParams;
  const site = await serverApi<Site>("/public/site");
  const p = site.projects?.find((p) => p.id === id);
  if (!p) notFound();
  return (
    <PublicShell>
      <p className="kicker">
        {p.status} · {p.stack}
      </p>
      <h1>{p.title || p.name}</h1>
      <p className="page-description">{p.description}</p>
      <SaveButton
        item={{
          kind: "project",
          id: p.id,
          title: p.title || p.name,
          url: `/project?id=${p.id}`,
        }}
      />
      <Markdown>{p.markdown || p.description}</Markdown>
      {p.url && /^https?:\/\//.test(p.url) && (
        <a href={p.url} target="_blank" rel="noreferrer">
          查看项目 ↗
        </a>
      )}
    </PublicShell>
  );
}
