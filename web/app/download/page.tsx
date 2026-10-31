import Link from "next/link";
import { PublicShell } from "@/components/public-shell";

export const metadata = {
  title: "下载桌面端",
  description: "下载微桥 Windows 桌面控制台最新版。",
};

const RELEASE_URL = "https://github.com/Wing233/wechat-agent-router/releases/latest";
const CHECKSUM_URL = `${RELEASE_URL}/download/WeChatAgentDesktop.exe.sha256`;

export default function DownloadPage() {
  return (
    <PublicShell>
      <p className="kicker">桌面端 · DESKTOP</p>
      <h1>微桥桌面控制台</h1>
      <p className="lead">
        Windows 桌面端：管理本机 Agent、引擎、工作区，以及远程 Router 的插件和定时任务。
      </p>

      <div className="reading-links">
        <a className="button accent" href="/download/desktop">
          下载最新版
        </a>
        <a className="button secondary" href={RELEASE_URL} target="_blank" rel="noreferrer">
          查看 Release
        </a>
      </div>

      <section
        style={{
          marginTop: 48,
          padding: 28,
          border: "1px solid var(--line)",
          background: "var(--surface)",
        }}
      >
        <h2 style={{ fontFamily: "var(--serif)", fontWeight: 400, fontSize: 28 }}>
          这个链接为什么一直是最新
        </h2>
        <p style={{ color: "var(--muted)", lineHeight: 1.9 }}>
          下载按钮指向 GitHub 的 <code>releases/latest/download</code> 固定地址。
          每次发布新 Release 时，构建流程都会把 Windows exe 以同一个文件名上传；
          GitHub 会自动把 <code>latest</code> 指向最新的非预发布版本，所以不需要改 LYPH 的代码。
        </p>
        <pre
          style={{
            marginTop: 18,
            padding: 16,
            overflowX: "auto",
            background: "rgba(22, 19, 15, 0.04)",
            fontSize: 12,
            lineHeight: 1.7,
          }}
        >
{`https://github.com/Wing233/wechat-agent-router/releases/latest/download/WeChatAgentDesktop.exe`}
        </pre>
        <p style={{ color: "var(--muted)", lineHeight: 1.9 }}>
          也可以直接访问本站的短链接：<code>/download/desktop</code>，它会 302 到同一个地址。
        </p>
      </section>

      <section style={{ marginTop: 32, color: "var(--muted)", lineHeight: 1.9 }}>
        <h2 style={{ fontFamily: "var(--serif)", fontWeight: 400, fontSize: 24 }}>
          安装说明
        </h2>
        <ol style={{ paddingLeft: 20 }}>
          <li>下载 <code>WeChatAgentDesktop.exe</code>，放到一个固定目录。</li>
          <li>首次运行会在 exe 同目录生成 <code>config.json</code>。</li>
          <li>填写 Router 地址、Agent ID、Agent Key，保存并启动 Agent。</li>
          <li>在 Router 的 <code>config.json</code> 里给同一个 Agent 配置 API Key。</li>
        </ol>
        <p>
          校验文件：<a href={CHECKSUM_URL} target="_blank" rel="noreferrer">SHA256</a>。
          exe 目前没有代码签名，Windows SmartScreen 可能提示“未知发布者”，请自行校验哈希。
        </p>
        <p>
          系统要求：Windows 10/11，WebView2 Runtime（Win11 通常已内置）。
        </p>
        <p>
          源码与文档：<a href="https://github.com/Wing233/wechat-agent-router" target="_blank" rel="noreferrer">Wing233/wechat-agent-router</a>。
        </p>
      </section>

      <div className="reading-links" style={{ marginTop: 40 }}>
        <Link href="/">回到主页 ↗</Link>
        <Link href="/blog">读一些想法 ↗</Link>
      </div>
    </PublicShell>
  );
}
