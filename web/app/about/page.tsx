import Link from "next/link";
import { PublicShell } from "@/components/public-shell";
import { serverApi, type Site } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function About() {
  const site = await serverApi<Site>("/public/site").catch(() => ({} as Site));
  return (
    <PublicShell>
      <p className="kicker">关于 · INTRODUCTION</p>
      <h1>{site.profile?.name || "LYPH"}</h1>
      <p className="lead">{site.profile?.bio || "记录技术、生活与思考。"}</p>
      <p>{site.profile?.introduction || "技术项目、生活记录和对话，在这里相遇。"}</p>
      <div className="reading-links">
        <Link href="/projects">查看项目 ↗</Link>
        <Link href="/">开始对话 ↗</Link>
      </div>
    </PublicShell>
  );
}
