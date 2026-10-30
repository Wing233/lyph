import Link from "next/link";
import { notFound } from "next/navigation";
import { PublicShell } from "@/components/public-shell";
import { serverApi, type Site } from "@/lib/api";
import { SaveButton } from "@/components/collection";
export default async function Page({
  params,
}: {
  params: Promise<{ directory: string }>;
}) {
  const { directory } = await params;
  if (!["projects", "stack", "interests"].includes(directory)) notFound();
  const site = await serverApi<Site>("/public/site");
  const titles = {
    projects: "项目与探索。",
    stack: "技术如何落地。",
    interests: "生活的另一面。",
  };
  const entries = (site[directory] || []) as Record<string, string>[];
  return (
    <PublicShell>
      <p className="kicker">技术与生活</p>
      <h1>{titles[directory as keyof typeof titles]}</h1>
      <div className="content-list">
        {entries.map((e, i) => (
          <article key={e.id || i}>
            <p className="eyebrow">{e.category || e.status || ""}</p>
            <h2>
              {directory === "projects" ? (
                <Link href={`/project?id=${e.id}`}>{e.title || e.name} ↗</Link>
              ) : (
                e.title || e.name
              )}
            </h2>
            <p>{e.description || e.use || ""}</p>
            {directory === "projects" && (
              <SaveButton
                item={{
                  kind: "project",
                  id: e.id,
                  title: e.title || e.name,
                  url: `/project?id=${e.id}`,
                }}
              />
            )}
          </article>
        ))}
      </div>
      {!entries.length && (
        <div className="empty-state">
          <h2>正在整理。</h2>
          <p>这里会逐渐放入站主确认的资料。</p>
        </div>
      )}
    </PublicShell>
  );
}
