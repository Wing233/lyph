"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  captures,
  enqueue,
  pairing,
  removeCapture,
  savePairing,
  syncOutbox,
  validateToken,
  type Capture,
  type Pairing,
} from "@/lib/outbox";
import { errorText } from "@/lib/api";
export function Collector() {
  const [tab, setTab] = useState("today");
  const [rows, setRows] = useState<Capture[]>([]);
  const [paired, setPaired] = useState<Pairing>();
  const [note, setNote] = useState("");
  const [token, setToken] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [storage, setStorage] = useState(true);
  const [online, setOnline] = useState(true);
  const refresh = useCallback(async () => {
    try {
      setRows(await captures());
      setPaired(await pairing());
    } catch (e) {
      setStorage(false);
      setNotice(`无法打开本地缓存：${errorText(e)}。请开启浏览器存储后继续。`);
    }
  }, []);
  const sync = useCallback(async () => {
    setBusy(true);
    try {
      const count = await syncOutbox(() => void refresh());
      setNotice(
        count
          ? `已确认上传 ${count} 条记录。`
          : "队列已同步。尚未上传的旧设备记录需重新配对对应凭证。",
      );
    } catch (e) {
      setNotice(`${errorText(e)}；待上传记录仍保存在本机。`);
    } finally {
      setBusy(false);
      await refresh();
    }
  }, [refresh]);
  useEffect(() => {
    void refresh();
    if ("serviceWorker" in navigator)
      void navigator.serviceWorker
        .register("/collector-sw.js", { scope: "/collector" })
        .catch(() => {});
    const resume = () => {
      setOnline(navigator.onLine);
      if (navigator.onLine && document.visibilityState === "visible")
        void pairing()
          .then((value) => {
            if (value) void sync();
          })
          .catch(() => {});
    };
    resume();
    window.addEventListener("online", resume);
    window.addEventListener("offline", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      window.removeEventListener("online", resume);
      window.removeEventListener("offline", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [refresh, sync]);
  const pending = rows.filter((c) => c.status === "pending");
  const today = new Date().toLocaleDateString("en-CA", {
    timeZone: "Asia/Shanghai",
  });
  const todayRows = rows.filter(
    (c) =>
      new Date(c.captured_at).toLocaleDateString("en-CA", {
        timeZone: "Asia/Shanghai",
      }) === today,
  );
  const row = (c: Capture) => (
    <article className="capture-row" key={c.capture_id}>
      <p className="capture-time">
        {new Date(c.captured_at).toLocaleString("zh-CN", {
          timeZone: "Asia/Shanghai",
        })}{" "}
        · {c.source_kind === "note" ? "速记" : "照片"} ·{" "}
        {c.status === "synced" ? "已确认上传" : "待同步"}
      </p>
      <p>{c.text || c.filename}</p>
      {c.error && <p className="error-text">{c.error}</p>}
      {c.device_id !== paired?.device_id && c.status === "pending" && (
        <p className="muted">属于其他设备凭证，重新配对后可续传。</p>
      )}
      {c.status === "synced" && (
        <button
          className="text-link"
          onClick={() => void removeCapture(c.capture_id).then(refresh)}
        >
          移除本地已同步记录
        </button>
      )}
    </article>
  );
  return (
    <>
      <main className="collector-shell" id="main-content">
        <header className="collector-header">
          <Link className="brand" href="/">
            LYPH<span>.</span>
          </Link>
          <span className="pill">
            {online ? "在线" : "离线"} · {paired ? "已配对" : "未配对"}
          </span>
        </header>
        <p className="eyebrow">PERSONAL CAPTURE</p>
        <h1>
          {tab === "today"
            ? "留下一点日常。"
            : tab === "queue"
              ? "记录，在路上。"
              : "你的移动采集台。"}
        </h1>
        {notice && (
          <p className="inline-notice" role="status">
            {notice}
          </p>
        )}
        {tab === "today" ? (
          <>
            <p className="muted">
              主动写一段速记，或选一张照片。缓存到本机后再同步。
            </p>
            <section className="panel">
              <form
                onSubmit={async (e) => {
                  e.preventDefault();
                  try {
                    if (!paired) throw new Error("请先在设置中配对设备。");
                    await enqueue({
                      device_id: paired.device_id,
                      source_kind: "note",
                      text: note,
                    });
                    setNote("");
                    setNotice("速记已保存在本机队列。");
                    await refresh();
                    if (navigator.onLine) void sync();
                  } catch (err) {
                    setNotice(errorText(err));
                  }
                }}
              >
                <label className="field">
                  速记
                  <textarea
                    rows={5}
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    required
                    maxLength={16000}
                    placeholder="今天看到、想到，或完成了什么？"
                  />
                </label>
                <button className="button" disabled={!storage}>
                  保存速记
                </button>
              </form>
              <label className="field" style={{ marginTop: 24 }}>
                添加照片
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  onChange={async (e) => {
                    const file = e.target.files?.[0];
                    if (!file) return;
                    try {
                      if (!paired) throw new Error("请先在设置中配对设备。");
                      if (file.size > 20 * 1024 * 1024)
                        throw new Error("照片超过20MiB，请压缩后再选。");
                      await enqueue({
                        device_id: paired.device_id,
                        source_kind: "photo",
                        text: note,
                        file,
                        filename: file.name,
                      });
                      setNote("");
                      setNotice("照片已保存在本机队列。");
                      await refresh();
                      if (navigator.onLine) void sync();
                    } catch (err) {
                      setNotice(errorText(err));
                    } finally {
                      e.target.value = "";
                    }
                  }}
                />
                <small>PNG / JPEG / WebP · 最多20MiB。照片默认私密。</small>
              </label>
            </section>
            <h2>今天的 {todayRows.length} 条记录</h2>
            {todayRows.map(row)}
            {!todayRows.length && (
              <p className="empty-state">从一小段速记开始。</p>
            )}
          </>
        ) : tab === "queue" ? (
          <>
            <div className="toolbar">
              <p>
                待上传 {pending.length} 条 · 已确认{" "}
                {rows.length - pending.length} 条
              </p>
              <button
                className="button"
                disabled={busy || !storage}
                onClick={() => void sync()}
              >
                {busy ? "正在同步…" : "立即同步"}
              </button>
            </div>
            {rows.map(row)}
            {!rows.length && <p className="empty-state">队列是空的。</p>}
            <p className="muted">
              关闭页面不会继续上传。重新打开或回到前台后自动续传，服务端按
              capture_id 确认，确认成功才移除文件。
            </p>
          </>
        ) : (
          <>
            <section className="panel">
              <h2>配对 iOS 设备</h2>
              <p className="muted">
                在后台「桌面与日报」创建 iOS PWA 设备，再粘贴一次性凭证。
              </p>
              <form
                onSubmit={async (e) => {
                  e.preventDefault();
                  setBusy(true);
                  try {
                    const next = await validateToken(token);
                    await savePairing(next);
                    setToken("");
                    setNotice("设备已配对。只有采集权限。");
                    await refresh();
                  } catch (err) {
                    setNotice(errorText(err));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <label className="field">
                  设备凭证
                  <input
                    type="password"
                    autoComplete="off"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    required
                  />
                </label>
                <button className="button" disabled={busy}>
                  验证并保存
                </button>
              </form>
              {paired && (
                <>
                  <p className="muted">设备：{paired.device_id}</p>
                  <button
                    className="text-link"
                    onClick={() => void savePairing(undefined).then(refresh)}
                  >
                    解除本机配对
                  </button>
                </>
              )}
            </section>
            <section className="panel">
              <h2>关于缓存</h2>
              <p>
                本机上限1GiB，满额后停止新增。服务器确认前保留原文件；上传成功后只保留记录摘要。保留待上传记录期间，请勿清除浏览器数据。
              </p>
              <p>
                可从 Safari 分享菜单「添加到主屏幕」。上线需
                HTTPS，首次打开后可离线记录。
              </p>
              <Link className="text-link" href="/admin/desktop">
                在后台审核日报 ↗
              </Link>
            </section>
          </>
        )}
      </main>
      <nav className="collector-tabs" aria-label="采集台标签">
        {[
          ["today", "今日"],
          ["queue", "队列"],
          ["settings", "设置"],
        ].map(([id, title]) => (
          <button
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            key={id}
          >
            {title}
            {id === "queue" && pending.length ? ` · ${pending.length}` : ""}
          </button>
        ))}
      </nav>
    </>
  );
}
