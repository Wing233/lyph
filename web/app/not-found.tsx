import Link from "next/link";
export default function NotFound() {
  return (
    <main className="reading-shell" id="main-content">
      <p className="kicker">404</p>
      <h1>这一页暂时没有公开。</h1>
      <p>内容可能仍在编辑，或已被撤回。</p>
      <Link href="/">回到主页 ↗</Link>
    </main>
  );
}
