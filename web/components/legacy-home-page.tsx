import Link from "next/link";
import { serverApi, items, type Content, type Site } from "@/lib/api";
import { HomeExperience } from "@/components/home-experience";
import { Chat } from "@/components/legacy-chat";
import { PageEvent } from "@/components/tracking";
export const dynamic = "force-dynamic";
export async function generateMetadata() {
  const site = await serverApi<Site>("/public/site").catch(() => ({}) as Site);
  return {
    title: { absolute: site.seo?.title || "lyph · 技术与生活" },
    description: site.seo?.description || "记录技术、生活与思考。",
  };
}
export default async function Home() {
  const [siteResult, entriesResult] = await Promise.allSettled([
    serverApi<Site>("/public/site"),
    serverApi("/public/contents?type=journal"),
  ]);
  const site = siteResult.status === "fulfilled" ? siteResult.value : {};
  const entries =
    entriesResult.status === "fulfilled"
      ? items<Content>(entriesResult.value).slice(0, 3)
      : [];
  const navigation = Array.isArray(site.navigation) ? site.navigation : [];
  return (
    <div className="home">
      <HomeExperience navigation={navigation} />
      <main id="main-content">
        <section className="hero">
          <p className="kicker">PORTFOLIO · {new Date().getFullYear()}</p>
          <h1>
            <span>Building at the</span>
            <span>
              <em>edge</em> of code
            </span>
            <span>and life.</span>
          </h1>
          <p className="lead">
            {site.profile?.bio ||
              "我是 LYPH。这份站点，是关于技术与生活的一切。"}
          </p>
        </section>
        <div className="marquee" aria-hidden="true">
          <span>
            Screen Journal — RAG Agent — UI Live Edit — Next.js — FastAPI —
            Tauri —
          </span>
        </div>
        <section id="intro">
          <p className="kicker">01 — INTRODUCTION</p>
          <div className="intro">
            <div className="num">01</div>
            <div>
              <p>
                {site.profile?.introduction || (
                  <>
                    我相信<b>代码是个人表达</b>。我想把技术项目、生活记录和 AI
                    对话放进同一个主页，让写下的代码与经历过的日常，在这里相遇。
                  </>
                )}
              </p>
              <div className="reading-links">
                <Link href="/blog" data-event="nav.blog.open">
                  读一些想法 ↗
                </Link>
                <Link href="/documents" data-event="nav.documents.open">
                  翻翻文档 ↗
                </Link>
              </div>
            </div>
          </div>
        </section>
        <section id="works">
          <p className="kicker">02 — SELECTED WORKS</p>
          <div className="works-track">
            {site.projects?.length ? (
              site.projects.slice(0, 4).map((p, i) => (
                <article className="work-card" key={p.id || i}>
                  <span className="idx">No.{i + 1}</span>
                  <h2>{p.title || p.name}</h2>
                  <p className="eyebrow">
                    {p.status} · {p.stack}
                  </p>
                  <p>{p.description}</p>
                  <Link href={`/project?id=${p.id}`}>查看项目 ↗</Link>
                </article>
              ))
            ) : (
              <div className="empty-plain">
                <h2>作品，慢慢写在这里。</h2>
                <p>站主尚未发布项目。</p>
              </div>
            )}
          </div>
        </section>
        <section id="diary">
          <p className="kicker">03 — DAILY DIARY</p>
          <div className="diary">
            {entries.map((e) => (
              <Link
                className="day"
                key={e.id}
                href={`/entry?id=${e.id}`}
                data-event="content.open"
              >
                <time>{e.date?.slice(5).replace("-", ".")}</time>
                <h3>{e.title}</h3>
                <p>{e.summary}</p>
              </Link>
            ))}
            {!entries.length && (
              <div className="empty-plain">
                <h2>日常留白。</h2>
                <p>
                  {entriesResult.status === "rejected"
                    ? "暂时没有连接到内容服务，稍后可以重新访问。"
                    : "还没有公开的生活记录。"}
                </p>
              </div>
            )}
          </div>
        </section>
        <section id="chat">
          <p className="kicker">04 — ASK THE AGENT</p>
          <Chat />
        </section>
        <aside id="thanks" className="acknowledgements">
          <p className="kicker">05 — 灵感与致谢</p>
          <h2>致十年前的那颗小球。</h2>
          <p>
            十年前，我曾沉浸于 Agar.io
            的小球世界。如今，把这段回忆放进自己的主页，也把那份简单的快乐留在这里。
          </p>
          <p className="credit-sources">
            感谢原作{" "}
            <a href="https://agar.io/" target="_blank" rel="noreferrer">
              Agar.io ↗
            </a>
            ，以及开源参考项目{" "}
            <a
              href="https://github.com/owenashurst/agar.io-clone"
              target="_blank"
              rel="noreferrer"
            >
              agar.io-clone ↗
            </a>
            。
          </p>
        </aside>
      </main>
      <footer className="public-footer">
        <span>© {new Date().getFullYear()} LYPH</span>
        <span>EDITION 02 · EDITORIAL</span>
      </footer>
      <PageEvent />
    </div>
  );
}
