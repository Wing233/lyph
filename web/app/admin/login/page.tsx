"use client";
import Link from "next/link";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, errorText, json, setCsrf } from "@/lib/api";
export default function Page() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  return (
    <main className="login-page" id="main-content">
      <section className="login-story">
        <Link href="/" className="brand">
          LYPH<span>.</span>
        </Link>
        <div>
          <p className="eyebrow">PRIVATE WORKSPACE</p>
          <h1>
            把想法写下，
            <br />
            让日常留下。
          </h1>
          <p>内容、资料与对话，在一个安静的工作台里整理。</p>
        </div>
        <small>技术与生活。</small>
      </section>
      <section className="login-form-wrap">
        <p className="eyebrow">站主登录</p>
        <h2>回来继续写。</h2>
        <p>使用本站的站主密码。</p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              const s = await api<{ csrf_token: string }>("/auth/login", {
                method: "POST",
                body: json({ password }),
              });
              setCsrf(s.csrf_token);
              setPassword("");
              router.replace("/admin/dashboard");
            } catch (err) {
              setError(errorText(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="field">
            站主密码
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
          <button className="button" disabled={busy}>
            {busy ? "正在验证…" : "进入工作台 ↗"}
          </button>
        </form>
        <Link className="text-link" href="/">
          回到官网 ↗
        </Link>
      </section>
    </main>
  );
}
