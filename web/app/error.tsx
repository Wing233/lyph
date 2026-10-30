"use client";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <main className="reading-shell" id="main-content">
      <h1>暂时没有读到内容。</h1>
      <p>服务连接未完成，可以稍后再试。</p>
      <button className="button" onClick={reset}>
        重新读取
      </button>
    </main>
  );
}
