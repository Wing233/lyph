import Link from "next/link";
import type { ReactNode } from "react";
import { PageEvent } from "./tracking";
export function PublicShell({
  children,
  contentId,
}: {
  children: ReactNode;
  contentId?: string;
}) {
  return (
    <>
      <header className="public-header">
        <Link className="brand" href="/">
          LYPH<span>.</span>
        </Link>
        <nav aria-label="网站目录">
          <Link data-event="nav.blog.open" href="/blog">
            博客
          </Link>
          <Link data-event="nav.documents.open" href="/documents">
            文档
          </Link>
          <Link data-event="nav.journal.open" href="/journal">
            日记
          </Link>
          <Link href="/projects">项目</Link>
          <Link href="/collection">收藏</Link>
        </nav>
      </header>
      <main className="reading-shell" id="main-content">
        {children}
      </main>
      <footer className="public-footer">
        <Link href="/">回到主页 ↗</Link>
        <span>技术与生活，在这里相遇。</span>
      </footer>
      <PageEvent contentId={contentId} />
    </>
  );
}
